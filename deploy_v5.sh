#!/bin/bash
# Deploy V5: build the Runtime B MicroVM image, then deploy Runtime A to AgentCore.
#
# Prerequisites:
#   - AWS CLI v2 with credentials
#   - boto3 >= 1.43.36 (for the lambda-microvms client, if you script API calls)
#   - A region where Lambda MicroVMs is available:
#       us-east-1, us-east-2, us-west-2, eu-west-1, ap-northeast-1
#
# Usage:
#   export REGION=us-west-2
#   export DATA_BUCKET=agentcore-zoom-demo-$(aws sts get-caller-identity --query Account --output text)
#   ./deploy_v5.sh
set -euo pipefail

REGION="${REGION:-us-west-2}"
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
DATA_BUCKET="${DATA_BUCKET:-agentcore-zoom-demo-$ACCOUNT}"
ARTIFACT_BUCKET="${ARTIFACT_BUCKET:-$DATA_BUCKET}"
IMAGE_NAME="${IMAGE_NAME:-data_workstation_v5}"
BASE_IMAGE_ARN="arn:aws:lambda:$REGION:aws:microvm-image:al2023-1"
OPUS_MODEL_ID="${OPUS_MODEL_ID:-us.anthropic.claude-opus-4-6-v1}"

echo "Region=$REGION Account=$ACCOUNT Bucket=$DATA_BUCKET Image=$IMAGE_NAME"

# ---------- 1. IAM: MicroVM build role ----------
BUILD_ROLE_NAME="${BUILD_ROLE_NAME:-MicrovmBuildRole-v5}"
cat > /tmp/mvm-build-trust.json <<EOF
{"Version":"2012-10-17","Statement":[{"Effect":"Allow",
  "Principal":{"Service":"lambda.amazonaws.com"},
  "Action":["sts:AssumeRole","sts:TagSession"]}]}
EOF
cat > /tmp/mvm-build-perms.json <<EOF
{"Version":"2012-10-17","Statement":[
  {"Effect":"Allow","Action":["s3:GetObject"],"Resource":"arn:aws:s3:::$ARTIFACT_BUCKET/*"},
  {"Effect":"Allow","Action":["logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents"],"Resource":"arn:aws:logs:*:*:*"}
]}
EOF
aws iam create-role --role-name "$BUILD_ROLE_NAME" \
  --assume-role-policy-document file:///tmp/mvm-build-trust.json 2>/dev/null || true
aws iam put-role-policy --role-name "$BUILD_ROLE_NAME" \
  --policy-name build-perms --policy-document file:///tmp/mvm-build-perms.json
BUILD_ROLE_ARN="arn:aws:iam::$ACCOUNT:role/$BUILD_ROLE_NAME"
echo "Build role: $BUILD_ROLE_ARN"

# ---------- 2. IAM: MicroVM execution role (runtime S3 access) ----------
EXEC_ROLE_NAME="${EXEC_ROLE_NAME:-MicrovmExecRole-v5}"
cat > /tmp/mvm-exec-trust.json <<EOF
{"Version":"2012-10-17","Statement":[{"Effect":"Allow",
  "Principal":{"Service":"lambda.amazonaws.com"},
  "Action":["sts:AssumeRole","sts:TagSession"]}]}
EOF
cat > /tmp/mvm-exec-perms.json <<EOF
{"Version":"2012-10-17","Statement":[
  {"Effect":"Allow","Action":["s3:GetObject","s3:PutObject","s3:ListBucket","s3:DeleteObject"],
   "Resource":["arn:aws:s3:::$DATA_BUCKET","arn:aws:s3:::$DATA_BUCKET/*"]}
]}
EOF
aws iam create-role --role-name "$EXEC_ROLE_NAME" \
  --assume-role-policy-document file:///tmp/mvm-exec-trust.json 2>/dev/null || true
aws iam put-role-policy --role-name "$EXEC_ROLE_NAME" \
  --policy-name exec-perms --policy-document file:///tmp/mvm-exec-perms.json
EXEC_ROLE_ARN="arn:aws:iam::$ACCOUNT:role/$EXEC_ROLE_NAME"
echo "Exec role: $EXEC_ROLE_ARN"

# ---------- 3. Package Runtime B and upload to S3 ----------
( cd runtime_b_v5 && zip -q -r /tmp/runtime_b_v5.zip main.py requirements.txt Dockerfile )
aws s3 cp /tmp/runtime_b_v5.zip "s3://$ARTIFACT_BUCKET/microvm/runtime_b_v5.zip" --region "$REGION"
echo "Artifact: s3://$ARTIFACT_BUCKET/microvm/runtime_b_v5.zip"

# ---------- 4. Create the MicroVM image (with /ready + /run hooks on port 9000) ----------
# Hook paths are fixed by the service; we only declare the port and timeouts.
aws lambda-microvms create-microvm-image \
  --region "$REGION" \
  --name "$IMAGE_NAME" \
  --code-artifact "uri=s3://$ARTIFACT_BUCKET/microvm/runtime_b_v5.zip" \
  --base-image-arn "$BASE_IMAGE_ARN" \
  --build-role-arn "$BUILD_ROLE_ARN" \
  --hooks '{"port":9000,
            "microvmImageHooks":{"ready":"/aws/lambda-microvms/runtime/v1/ready","readyTimeoutInSeconds":120},
            "microvmHooks":{"run":"/aws/lambda-microvms/runtime/v1/run","runTimeoutInSeconds":30}}' \
  || echo "create-microvm-image may already exist; use update-microvm-image to ship new code"

echo "Poll image state until CREATED:"
echo "  aws lambda-microvms get-microvm-image --region $REGION --image-identifier $IMAGE_NAME"

IMAGE_ARN="arn:aws:lambda:$REGION:$ACCOUNT:microvm-image:$IMAGE_NAME"
echo "Image ARN (once CREATED): $IMAGE_ARN"

# ---------- 5. Deploy Runtime A to AgentCore ----------
cat <<EOF

Next, deploy Runtime A (set MICROVM_IMAGE_ARN + MICROVM_EXEC_ROLE_ARN):

  cd runtime_a_v5
  agentcore configure --create --name data_router_v5 --entrypoint main.py --region $REGION --non-interactive
  # enable ecr_auto_create + s3_auto_create in .bedrock_agentcore.yaml
  agentcore deploy \\
    --env DATA_BUCKET=$DATA_BUCKET \\
    --env AWS_REGION=$REGION \\
    --env MODEL_ID=$OPUS_MODEL_ID \\
    --env OPUS_MODEL_ID=$OPUS_MODEL_ID \\
    --env MICROVM_IMAGE_ARN=$IMAGE_ARN \\
    --env MICROVM_EXEC_ROLE_ARN=$EXEC_ROLE_ARN

Runtime A's AgentCore execution role also needs:
  - lambda-microvms: RunMicrovm, GetMicrovm, CreateMicrovmAuthToken, TerminateMicrovm
  - iam:PassRole on $EXEC_ROLE_ARN
  - bedrock: InvokeModel, InvokeModelWithResponseStream
EOF
