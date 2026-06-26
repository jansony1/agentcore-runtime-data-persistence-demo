"""
Seed the demo RDS MySQL with random Q1 sales data.

Usage (from the bastion, where the RDS endpoint is reachable):
    MYSQL_HOST=<rds-endpoint> MYSQL_PASSWORD='V5demoPass123!' python3 seed_mysql.py

Creates one table `sales` under database `salesdb` and fills it with random
transactions across 5 regions / 5 products, so the analysis flow has something
to chew on. Idempotent: drops and recreates the table each run.
"""
import os
import random

import pymysql

HOST = os.environ["MYSQL_HOST"]
PORT = int(os.environ.get("MYSQL_PORT", "3306"))
USER = os.environ.get("MYSQL_USER", "admin")
PASSWORD = os.environ["MYSQL_PASSWORD"]
DB = os.environ.get("MYSQL_DB", "salesdb")

REGIONS = ["华东", "华南", "华北", "西南", "华中"]
PRODUCTS = ["云服务器ECS", "对象存储OSS", "数据库RDS", "CDN加速", "容器服务ACK"]
REPS = ["张伟", "李娜", "王强", "刘洋", "陈静", "赵明", "孙磊", "周婷"]
# Per-region Q1 target (so achievement rate is interesting / all under target)
TARGETS = {"华东": 5000000, "华南": 4000000, "华北": 3500000, "西南": 2500000, "华中": 2000000}

conn = pymysql.connect(host=HOST, port=PORT, user=USER, password=PASSWORD, database=DB, charset="utf8mb4")
cur = conn.cursor()

cur.execute("DROP TABLE IF EXISTS sales")
cur.execute("""
CREATE TABLE sales (
    id INT AUTO_INCREMENT PRIMARY KEY,
    txn_date DATE NOT NULL,
    region VARCHAR(32) NOT NULL,
    product VARCHAR(64) NOT NULL,
    sales_rep VARCHAR(32) NOT NULL,
    amount DECIMAL(12,2) NOT NULL
) CHARACTER SET utf8mb4
""")

cur.execute("DROP TABLE IF EXISTS region_targets")
cur.execute("""
CREATE TABLE region_targets (
    region VARCHAR(32) PRIMARY KEY,
    q1_target DECIMAL(14,2) NOT NULL
) CHARACTER SET utf8mb4
""")
cur.executemany("INSERT INTO region_targets (region, q1_target) VALUES (%s, %s)",
                list(TARGETS.items()))

# 2000 random Q1 transactions (Jan-Mar 2026)
rows = []
for _ in range(2000):
    month = random.choice([1, 2, 3])
    day = random.randint(1, 28)
    region = random.choice(REGIONS)
    rows.append((
        f"2026-{month:02d}-{day:02d}",
        region,
        random.choice(PRODUCTS),
        random.choice(REPS),
        round(random.uniform(100, 8000), 2),
    ))
cur.executemany(
    "INSERT INTO sales (txn_date, region, product, sales_rep, amount) VALUES (%s,%s,%s,%s,%s)",
    rows,
)
conn.commit()

cur.execute("SELECT COUNT(*), ROUND(SUM(amount),2) FROM sales")
n, total = cur.fetchone()
print(f"seeded sales: {n} rows, total amount={total}")
cur.close()
conn.close()
