# Sample: analyze data pulled from RDS MySQL (MicroVM, end-to-end)

A self-contained variant of V5 where the data source is **Amazon RDS MySQL**
instead of S3. The model (in the sandbox) connects to MySQL with `pymysql`,
pulls the `sales` / `region_targets` tables into pandas, computes Q1 achievement
rates, charts them, and the Orchestrator writes the report.

Same architecture as V5 (see [../DESIGN_V5.md](../DESIGN_V5.md)) — only the data
source and the sandbox image (adds `pymysql`) change.

```
Orchestrator Runtime ── run_microvm ──▶ Sandbox Runtime (MicroVM, pymysql baked in)
        │ HTTPS shell/python                     │ python: pymysql → pandas → matplotlib
        │ converse_stream (report)               ▼
        ▼                                   RDS MySQL (salesdb.sales)
   Bedrock (Opus)                           charts/CSV → /tmp/workspace/output
```

## Files

```
sample/
├── seed_mysql.py            # create tables + random Q1 sales data in RDS
├── runtime_b/               # Sandbox Runtime (shell/python executor) — image bakes pymysql + CJK fonts
│   ├── main.py
│   ├── Dockerfile
│   └── requirements.txt
├── runtime_a/               # Orchestrator: drives the sandbox, renders the report
│   ├── main.py
│   ├── requirements.txt
│   └── (no Dockerfile — built locally as sample-orch:local for the bastion run)
├── run_experiment.sh        # build + invoke the orchestrator
└── README.md
```

## Full flow (from scratch, on the bastion in us-west-2)

### 1. RDS MySQL (publicly accessible, port 3306)

```bash
SG=$(aws ec2 create-security-group --region us-west-2 --group-name v5-mysql-demo-sg \
  --description "v5 mysql demo" --vpc-id <default-vpc> --query GroupId --output text)
aws ec2 authorize-security-group-ingress --region us-west-2 --group-id $SG \
  --protocol tcp --port 3306 --cidr 0.0.0.0/0
aws rds create-db-instance --region us-west-2 \
  --db-instance-identifier v5-mysql-demo --db-instance-class db.t4g.micro \
  --engine mysql --master-username admin --master-user-password 'V5demoPass123!' \
  --allocated-storage 20 --vpc-security-group-ids $SG --publicly-accessible \
  --backup-retention-period 0 --no-multi-az --db-name salesdb
# wait until status=available, note Endpoint.Address
```

### 2. Seed data

```bash
pip install pymysql   # on the bastion (or run in a python:3.11 container)
MYSQL_HOST=<rds-endpoint> MYSQL_PASSWORD='V5demoPass123!' python3 seed_mysql.py
# -> seeded sales: 2000 rows, total amount=...
```

### 3. Build the Sandbox MicroVM image

```bash
cd runtime_b && zip -r /tmp/sandbox.zip main.py requirements.txt Dockerfile
aws s3 cp /tmp/sandbox.zip s3://<same-region-artifact-bucket>/microvm/sandbox.zip --region us-west-2
aws lambda-microvms create-microvm-image --region us-west-2 \
  --name sample_mysql_sandbox \
  --code-artifact uri=s3://<artifact-bucket>/microvm/sandbox.zip \
  --base-image-arn arn:aws:lambda:us-west-2:aws:microvm-image:al2023-1 \
  --build-role-arn <build-role-arn> \
  --hooks '{"port":9000,"microvmImageHooks":{"ready":"ENABLED","readyTimeoutInSeconds":120},"microvmHooks":{"run":"ENABLED","runTimeoutInSeconds":30}}'
# poll get-microvm-image until state=CREATED
```

The MicroVM execution role needs S3 (for the report bucket) and outbound to RDS
is over the default internet egress (RDS is publicly accessible here).

### 4. Run the experiment

```bash
cd .. && docker build -q -t sample-orch:local -f - runtime_a <<'DOCKER'
FROM public.ecr.aws/docker/library/python:3.11-slim
WORKDIR /app
COPY requirements.txt . && RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .
CMD ["python","main.py"]
DOCKER

MYSQL_HOST=<rds-endpoint> MYSQL_PASSWORD='V5demoPass123!' \
MICROVM_IMAGE_ARN=<image-arn> MICROVM_EXEC_ROLE_ARN=<exec-role-arn> \
DATA_BUCKET=<report-bucket> ./run_experiment.sh
```

You'll see SSE: `provision` → `analysis` (the model writing pymysql + pandas +
matplotlib, each step live) → `report` (streamed) → `done`.

### 5. Clean up (avoid charges)

```bash
aws rds delete-db-instance --region us-west-2 --db-instance-identifier v5-mysql-demo \
  --skip-final-snapshot --delete-automated-backups
aws lambda-microvms terminate-microvm --region us-west-2 --microvm-identifier <id>   # if any left
```
