"""Generate sample sales data for multiple tenants and upload to S3.

Usage:
    DATA_BUCKET=your-bucket-name python3 generate_sample_data.py
"""
import csv
import io
import os
import random
import sys

import boto3
from datetime import datetime, timedelta

BUCKET = os.environ.get("DATA_BUCKET")
REGION = os.environ.get("AWS_REGION", "us-east-1")

if not BUCKET:
    print("ERROR: Set DATA_BUCKET environment variable first.")
    print("  export DATA_BUCKET=your-bucket-name")
    sys.exit(1)

# Two tenants with different business profiles
TENANTS = {
    "acme-corp": {
        "regions": ["华东", "华南", "华北", "西南", "华中"],
        "products": ["云服务器ECS", "对象存储OSS", "数据库RDS", "CDN加速", "容器服务ACK"],
        "sales_reps": ["张伟", "李娜", "王强", "刘洋", "陈静", "赵明", "孙磊", "周婷"],
        "price_range": (100, 8000),
        "targets": {"华东": (5000000, 5500000), "华南": (4000000, 4200000),
                    "华北": (4500000, 4800000), "西南": (2000000, 2200000), "华中": (3000000, 3200000)},
        "num_rows": 500,
    },
    "globex-inc": {
        "regions": ["North America", "Europe", "Asia Pacific", "Latin America"],
        "products": ["SaaS Platform", "Data Analytics", "IoT Suite", "Security Pro"],
        "sales_reps": ["Alice", "Bob", "Carol", "Dave", "Eve", "Frank"],
        "price_range": (500, 15000),
        "targets": {"North America": (8000000, 8500000), "Europe": (6000000, 6500000),
                    "Asia Pacific": (4000000, 4500000), "Latin America": (2000000, 2500000)},
        "num_rows": 300,
    },
}


def generate_sales_csv(config):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["transaction_id", "date", "region", "product", "sales_rep",
                      "quantity", "unit_price", "discount_pct", "cost"])

    base_date = datetime(2026, 1, 1)
    lo, hi = config["price_range"]
    for i in range(1, config["num_rows"] + 1):
        date = base_date + timedelta(days=random.randint(0, 180))
        unit_price = random.randint(lo, hi)
        qty = random.randint(1, 20)
        discount = random.choice([0, 5, 10, 15, 20])
        cost = round(unit_price * qty * (1 - discount / 100), 2)

        writer.writerow([
            f"TXN-{i:05d}", date.strftime("%Y-%m-%d"),
            random.choice(config["regions"]), random.choice(config["products"]),
            random.choice(config["sales_reps"]), qty, unit_price, discount, cost,
        ])
    return buf.getvalue()


def generate_targets_csv(config):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["region", "q1_target", "q2_target"])
    for region, (q1, q2) in config["targets"].items():
        writer.writerow([region, q1, q2])
    return buf.getvalue()


if __name__ == "__main__":
    s3 = boto3.client("s3", region_name=REGION)

    for tenant_id, config in TENANTS.items():
        prefix = f"tenants/{tenant_id}/datasets/sales/2026-H1"

        sales = generate_sales_csv(config)
        s3.put_object(Bucket=BUCKET, Key=f"{prefix}/transactions.csv",
                      Body=sales.encode("utf-8"), ContentType="text/csv")
        print(f"[{tenant_id}] Uploaded transactions.csv ({config['num_rows']} rows)")

        targets = generate_targets_csv(config)
        s3.put_object(Bucket=BUCKET, Key=f"{prefix}/region_targets.csv",
                      Body=targets.encode("utf-8"), ContentType="text/csv")
        print(f"[{tenant_id}] Uploaded region_targets.csv")

    # Verify
    print(f"\nS3 layout (s3://{BUCKET}/tenants/):")
    resp = s3.list_objects_v2(Bucket=BUCKET, Prefix="tenants/")
    for obj in resp.get("Contents", []):
        print(f"  {obj['Key']}  ({obj['Size']} bytes)")
