#!/bin/bash
# End-to-end MySQL→analysis experiment driver (run on the bastion).
#
# Prereqs already done by the setup: RDS MySQL up, data seeded, MicroVM image
# built. This script builds the local Orchestrator container, points it at the
# MicroVM image + RDS, and invokes the SSE entrypoint.
#
# Env you must set:
#   MYSQL_HOST, MYSQL_PASSWORD, MICROVM_IMAGE_ARN, MICROVM_EXEC_ROLE_ARN,
#   DATA_BUCKET (optional, for report upload), REGION (default us-west-2)
set -euo pipefail
REGION="${REGION:-us-west-2}"

docker rm -f sample-orch 2>/dev/null || true
docker run -d --name sample-orch -p 8095:8080 \
  -e AWS_REGION="$REGION" \
  -e DATA_BUCKET="${DATA_BUCKET:-}" \
  -e MODEL_ID=us.anthropic.claude-opus-4-6-v1 \
  -e OPUS_MODEL_ID=us.anthropic.claude-opus-4-6-v1 \
  -e MICROVM_IMAGE_ARN="$MICROVM_IMAGE_ARN" \
  -e MICROVM_EXEC_ROLE_ARN="$MICROVM_EXEC_ROLE_ARN" \
  -e MYSQL_HOST="$MYSQL_HOST" \
  -e MYSQL_PORT=3306 \
  -e MYSQL_USER=admin \
  -e MYSQL_PASSWORD="$MYSQL_PASSWORD" \
  -e MYSQL_DB=salesdb \
  -v "$HOME/.aws:/root/.aws:ro" \
  sample-orch:local
sleep 4
echo "=== orchestrator up; invoking ==="
curl -sN -X POST http://localhost:8095/invocations -H 'Content-Type: application/json' \
  -d '{"tenant_id":"demo","message":"从 MySQL 分析各区域Q1销售达成率，给出排名和改进建议"}' \
  --max-time 590
