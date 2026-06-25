# V5 Testing Guide — Bastion Bypass Workflow

How V5 was tested **without** a full AgentCore deploy, using an EC2 bastion in
`us-west-2` (a MicroVMs-supported region). Two layers:

1. **Local layer** — Runtime A + Runtime B as Docker containers on the bastion,
   Runtime A pointed at a local Runtime B (`RUNTIME_B_URL`). Fast feedback,
   no MicroVM involved.
2. **Real layer** — build an actual MicroVM image, `run-microvm`, and have
   Runtime A start/terminate a real MicroVM end-to-end.

> Why a bastion and not a laptop: MicroVMs is region-limited (us-east-1/2,
> us-west-2, eu-west-1, ap-northeast-1) and the workflow needs Bedrock + S3 in
> that region. The bastion (`us-west-2`) sits inside AWS with the right creds.

---

## 0. Environment facts (bastion)

| Item | Value |
|------|-------|
| Host | `ec2-user@52.25.73.119` (alias `basion`), `us-west-2` |
| Account | `269562551342` |
| Docker | present; `sudo systemctl start docker` if daemon down |
| Python | 3.7 (system, too old) / 3.10 (lacks `_ctypes`) → **use Docker python:3.11** |
| Data bucket | `agentcore-zoom-demo-269562551342` (**us-east-1**) |
| Artifact bucket | `agentcore-zoom-demo-v5-usw2-269562551342` (**us-west-2**, created for builds) |

**boto3 caveat:** the bastion's system boto3 is 1.33 (no `lambda-microvms`
client). The MicroVMs API needs **boto3 ≥ 1.43.36** → driven from a Docker
python:3.11 container (`drv/run.sh` below).

---

## 1. Local layer — A + B as containers

### Build images (python:3.11-slim stand-in for MicroVM base; app code identical)

```bash
# Runtime B (uses stdlib HTTP server + pandas/numpy/matplotlib)
cd ~/v5test/runtime_b_v5
cat > Dockerfile.local <<'EOF'
FROM public.ecr.aws/docker/library/python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends awscli && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt . && pip install --no-cache-dir -r requirements.txt
COPY main.py .
ENV APP_PORT=8080 HOOK_PORT=9000 WORKSPACE_DIR=/tmp/workspace
CMD ["python3","main.py"]
EOF
docker build -t runtime-b-v5:local -f Dockerfile.local .

# Runtime A (strands + bedrock-agentcore + boto3>=1.43.36)
cd ~/v5test/runtime_a_v5
docker build -t runtime-a-v5:local -f Dockerfile.local .   # Dockerfile.local = same as Dockerfile
```

### Run on a shared network, A → local B

```bash
docker network create v5net
docker run -d --name rbv5 --network v5net -p 8080:8080 -p 9000:9000 \
  -e AWS_REGION=us-west-2 runtime-b-v5:local

# reset workspace via the /run hook (mimics MicroVM lifecycle)
curl -s -X POST http://localhost:9000/aws/lambda-microvms/runtime/v1/run

docker run -d --name rav5 --network v5net -p 8090:8080 \
  -e AWS_REGION=us-west-2 \
  -e DATA_BUCKET=agentcore-zoom-demo-269562551342 \
  -e MODEL_ID=us.anthropic.claude-opus-4-6-v1 \
  -e OPUS_MODEL_ID=us.anthropic.claude-opus-4-6-v1 \
  -e RUNTIME_B_URL=http://rbv5:8080/ \
  -v ~/.aws:/root/.aws:ro \
  runtime-a-v5:local
```

### Smoke-test Runtime B directly

```bash
curl -s http://localhost:8080/                                   # {"status":"ok"}
curl -s -X POST http://localhost:8080/ -H 'Content-Type: application/json' \
  -d '{"action":"shell","command":"echo hi && pwd"}'
# python action: pass JSON via --data @file to avoid shell escaping of \n
curl -s -X POST http://localhost:8080/ -H 'Content-Type: application/json' --data @p1.json
```

### Full A→B SSE run (detached — see "Pitfall: client timeout")

```bash
cat > runfull.sh <<'EOF'
#!/bin/bash
curl -sN -X POST http://localhost:8090/invocations -H 'Content-Type: application/json' \
  -d '{"tenant_id":"acme-corp","message":"分析各区域Q1销售达成率，给出排名和改进建议"}' \
  --max-time 590 > full.out 2>&1
echo "EXIT=$? ELAPSED done" > full.status
EOF
chmod +x runfull.sh
nohup ./runfull.sh >/dev/null 2>&1 &        # detach so a stuck SSH won't kill it

# poll (short calls)
cat full.status ; grep -c '^data:' full.out
grep '^data:' full.out | sed -E 's/.*"type": "([a-z]+)".*/\1/' | sort | uniq -c
```

**Verified local result:** 6 status + 923 chunk + 1 done, 175s, report.md uploaded.

---

## 2. Real layer — actual MicroVM

### boto3 driver (latest boto3 in a container)

```bash
mkdir -p ~/v5test/drv
cat > ~/v5test/drv/run.sh <<'EOF'
#!/bin/bash
docker run --rm -v /home/ec2-user/.aws:/root/.aws:ro -v /home/ec2-user/v5test:/w -w /w \
  -e AWS_REGION=us-west-2 \
  public.ecr.aws/docker/library/python:3.11-slim \
  bash -c "pip install -q boto3==1.43.36 2>/dev/null && python /w/$1"
EOF
chmod +x ~/v5test/drv/run.sh
```

### IAM roles (build role + execution role)

```bash
# Trust: lambda.amazonaws.com with sts:AssumeRole + sts:TagSession
aws iam create-role --role-name MicrovmBuildRole-v5 --assume-role-policy-document file://trust.json
# Build perms: s3:GetObject on the ARTIFACT bucket + logs:*  (MUST include the us-west-2 bucket)
aws iam put-role-policy --role-name MicrovmBuildRole-v5 --policy-name build-perms --policy-document file://build-perms.json
aws iam create-role --role-name MicrovmExecRole-v5 --assume-role-policy-document file://trust.json
# Exec perms: s3 Get/Put/List/Delete on the DATA bucket
aws iam put-role-policy --role-name MicrovmExecRole-v5 --policy-name exec-perms --policy-document file://exec-perms.json
```

### Package + upload (artifact bucket MUST be same region as the build)

```bash
cd runtime_b_v5
zip -q -r /tmp/runtime_b_v5.zip main.py requirements.txt Dockerfile
aws s3 cp /tmp/runtime_b_v5.zip \
  s3://agentcore-zoom-demo-v5-usw2-269562551342/microvm/runtime_b_v5.zip --region us-west-2
```

### Create / update the MicroVM image (hooks are ENABLED/DISABLED, see pitfall)

```python
# create_image.py — run via: ~/v5test/drv/run.sh create_image.py
c.create_microvm_image(
    name="data_workstation_v5",
    codeArtifact={"uri": "s3://agentcore-zoom-demo-v5-usw2-269562551342/microvm/runtime_b_v5.zip"},
    baseImageArn="arn:aws:lambda:us-west-2:aws:microvm-image:al2023-1",
    buildRoleArn="arn:aws:iam::269562551342:role/MicrovmBuildRole-v5",
    hooks={"port": 9000,
           "microvmImageHooks": {"ready": "ENABLED", "readyTimeoutInSeconds": 120},
           "microvmHooks":      {"run":   "ENABLED", "runTimeoutInSeconds": 30}},
)
# ship new code later with update_microvm_image (same args; base+build role required each time)
```

Poll until built:

```python
# get_image.py
r = c.get_microvm_image(imageIdentifier="arn:aws:lambda:us-west-2:269562551342:microvm-image:data_workstation_v5")
# imageState: CREATING → CREATED  (or UPDATING → UPDATED on update)
```

### Run a MicroVM, measure cold start, connect

```python
# run_mvm.py
r = c.run_microvm(imageIdentifier=ARN, executionRoleArn=EXEC,
    ingressNetworkConnectors=["arn:aws:lambda:us-west-2:aws:network-connector:aws-network-connector:ALL_INGRESS"],
    egressNetworkConnectors=["arn:aws:lambda:us-west-2:aws:network-connector:aws-network-connector:INTERNET_EGRESS"],
    idlePolicy={"autoResumeEnabled": True, "maxIdleDurationSeconds": 900, "suspendedDurationSeconds": 300},
    maximumDurationInSeconds=1800)
# poll get_microvm until state==RUNNING  → measured 2.25–2.51s (PENDING→RUNNING)
tok = c.create_microvm_auth_token(microvmIdentifier=mid, expirationInMinutes=30,
                                  allowedPorts=[{"allPorts": {}}])
token = tok["authToken"]["X-aws-proxy-auth"]
```

```bash
# call the app over the real HTTPS endpoint (port 8080 default; 9000 = hooks)
curl -s "https://$EP/" -H "X-aws-proxy-auth: $TOK"                      # health
curl -s -X POST "https://$EP/" -H "X-aws-proxy-auth: $TOK" -H 'Content-Type: application/json' \
  -d '{"action":"shell","command":"aws s3 cp s3://BUCKET/.../datasets/ /tmp/workspace/ --recursive --region us-east-1"}'
```

### Full real E2E: Runtime A drives a real MicroVM

Run Runtime A in **deployed mode** (set `MICROVM_IMAGE_ARN` + `MICROVM_EXEC_ROLE_ARN`,
drop `RUNTIME_B_URL`). It will `run_microvm` → drive → `terminate_microvm`.

```bash
docker run -d --name rav5 --network v5net -p 8090:8080 \
  -e AWS_REGION=us-west-2 -e DATA_BUCKET=agentcore-zoom-demo-269562551342 \
  -e MODEL_ID=us.anthropic.claude-opus-4-6-v1 -e OPUS_MODEL_ID=us.anthropic.claude-opus-4-6-v1 \
  -e MICROVM_IMAGE_ARN=arn:aws:lambda:us-west-2:269562551342:microvm-image:data_workstation_v5 \
  -e MICROVM_EXEC_ROLE_ARN=arn:aws:iam::269562551342:role/MicrovmExecRole-v5 \
  -v ~/.aws:/root/.aws:ro runtime-a-v5:local
# then invoke /invocations as in the local layer (detached). Verified: 756 events, 184s, exit=0.
```

### Cleanup (avoid charges)

```python
# terminate any running MicroVM; verify none left
c.terminate_microvm(microvmIdentifier=mid)
[m for m in c.list_microvms()["items"] if m["state"] not in ("TERMINATED","TERMINATING")]  # → []
```
```bash
docker rm -f rav5 rbv5
```

---

## Pitfalls hit (and fixes)

| # | Symptom | Root cause | Fix |
|---|---------|-----------|-----|
| 1 | `ValidationException ... hooks.microvmImageHooks.ready ... enum [ENABLED,DISABLED]` | **Docs vs API mismatch**: hook fields are on/off toggles, not paths. Paths are fixed by the service. | Pass `"ENABLED"`, not the path string. |
| 2 | image build `CREATE_FAILED`: "artifact is inaccessible because it is stored in a bucket in another region" | Data bucket is us-east-1; build runs in us-west-2 | Upload the zip to a **same-region** artifact bucket; grant build role `s3:GetObject` on it. |
| 3 | `ModuleNotFoundError: No module named 'dateutil'` when running `aws` in the MicroVM | `dnf install awscli` puts a 2nd copy in `/usr/bin` that can't see pip deps | Install awscli via **pip**, not dnf. |
| 4 | matplotlib Chinese labels render as boxes | al2023-minimal has no CJK fonts | `dnf install google-noto-sans-cjk-ttc-fonts` in the Dockerfile. |
| 5 | `boto3` has no `lambda-microvms` client | bastion boto3 is 1.33 / py3.7 caps it | Drive the API from a Docker python:3.11 container with `boto3==1.43.36`. |
| 6 | SSE run appears to "hang"/0 events; report.md not updated | My `--max-time`/2-min SSH tool timeout killed the client mid-run (full loop ≈ 3 min); curl output is block-buffered | Run **detached** with `nohup`, poll a status file. Not an app bug. |
| 7 | `find: command not found` in shell action | al2023-minimal is minimal | Cosmetic (test command only); use `ls`/glob, or `dnf install findutils` if needed. |
| 8 | py3.10 venv `No module named '_ctypes'` | bastion python3.10 built without libffi | Use Docker python:3.11 for anything needing pandas/boto3. |
