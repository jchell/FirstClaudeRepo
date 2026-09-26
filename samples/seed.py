"""Seeds the dev test sources (docker-compose.dev.yml) with sample data.

    docker compose -f docker-compose.yml -f docker-compose.dev.yml run --rm seed

Idempotent: re-running resets the sample data. All credentials are the dev
sources' own throwaway ones.
"""

from __future__ import annotations

import io
import json
import os
import random
import sys
import time
from datetime import UTC, datetime, timedelta

PW = "devsource"
TODAY = datetime.now(UTC)
STAMP = TODAY.strftime("%Y%m%d")
random.seed(42)

FIRST = ["Ann", "Bob", "Cyra", "Dev", "Eli", "Fay", "Gus", "Hana", "Ivo", "Jun", "Kai", "Lea"]
LAST = ["Ng", "Olsen", "Park", "Quinn", "Rossi", "Silva", "Tran", "Umar", "Vega", "Wong"]
CITIES = ["Oslo", "Lyon", "Porto", "Graz", "Turku", "Cork"]


def customers(n: int = 60) -> list[dict]:
    out = []
    for i in range(1, n + 1):
        first, last = random.choice(FIRST), random.choice(LAST)
        out.append({
            "id": i, "first_name": first, "last_name": last,
            "email": f"{first.lower()}.{last.lower()}{i}@example.com",
            "phone": f"+47 {random.randint(400, 999)} {random.randint(10, 99)} {random.randint(100, 999)}",
            "city": random.choice(CITIES), "state": random.choice(["NO", "FR", "PT", "AT", "FI", "IE"]),
            "updated_at": TODAY - timedelta(days=n - i),
        })  # fmt: skip
    return out


def orders(n: int = 200) -> list[dict]:
    return [
        {"order_id": i, "customer_id": random.randint(1, 60), "amount": round(random.uniform(5, 500), 2),
         "status": random.choice(["new", "paid", "shipped", "returned"]), "order_date": (TODAY - timedelta(days=i % 30)).date().isoformat()}
        for i in range(1, n + 1)
    ]  # fmt: skip


def retry(name: str, fn, attempts: int = 30) -> None:
    for i in range(attempts):
        try:
            fn()
            print(f"seeded {name}")
            return
        except Exception as e:  # sources may still be starting
            if i == attempts - 1:
                print(f"FAILED {name}: {type(e).__name__}: {e}", file=sys.stderr)
                raise
            time.sleep(2)


def seed_postgres() -> None:
    import psycopg

    with psycopg.connect(f"host=src-postgres dbname=sales user=dev password={PW}", autocommit=True) as c:
        c.execute("drop schema if exists crm cascade; create schema crm")
        c.execute(
            "create table crm.customers (id int primary key, first_name text, last_name text, email text, phone text,"
            " city text, state text, updated_at timestamptz not null)"
        )
        with c.cursor() as cur:
            cur.executemany(
                "insert into crm.customers values (%(id)s,%(first_name)s,%(last_name)s,%(email)s,%(phone)s,%(city)s,"
                "%(state)s,%(updated_at)s)",
                customers(),
            )
        c.execute("create table crm.orders (order_id int primary key, customer_id int, amount numeric(10,2), status text, order_date date)")
        with c.cursor() as cur:
            cur.executemany("insert into crm.orders values (%(order_id)s,%(customer_id)s,%(amount)s,%(status)s,%(order_date)s)", orders())


def seed_mysql() -> None:
    import pymysql

    c = pymysql.connect(host="src-mysql", user="dev", password=PW, database="shop", autocommit=True)
    with c.cursor() as cur:
        cur.execute("drop table if exists products")
        cur.execute("create table products (sku varchar(20) primary key, name varchar(100), price decimal(8,2), stock int, updated_at datetime)")
        cur.executemany(
            "insert into products values (%s,%s,%s,%s,%s)",
            [(f"SKU-{i:04d}", f"Product {i}", round(random.uniform(1, 99), 2), random.randint(0, 500),
              (TODAY - timedelta(hours=i)).replace(tzinfo=None)) for i in range(1, 81)],
        )  # fmt: skip
    c.close()


def seed_mssql() -> None:
    import pymssql

    c = pymssql.connect(server="src-mssql", user="sa", password="Dev-Source-2026", autocommit=True)
    cur = c.cursor()
    cur.execute("if db_id('erp') is null create database erp")
    cur.execute("use erp; if object_id('dbo.suppliers') is not null drop table dbo.suppliers")
    cur.execute("use erp; create table dbo.suppliers (id int primary key, name nvarchar(100), country nchar(2), rating decimal(3,1))")
    cur.executemany("insert into erp.dbo.suppliers values (%d, %s, %s, %s)",
                    [(i, f"Supplier {i}", random.choice(["NO", "DE", "US"]), round(random.uniform(1, 5), 1)) for i in range(1, 31)])  # fmt: skip
    c.close()


def seed_mongo() -> None:
    from pymongo import MongoClient

    db = MongoClient("src-mongo", 27017, username="dev", password=PW, serverSelectionTimeoutMS=5000)["catalog"]
    db.reviews.drop()
    db.reviews.insert_many([
        {"review_id": i, "sku": f"SKU-{random.randint(1, 80):04d}", "stars": random.randint(1, 5),
         "text": random.choice(["great", "ok", "meh", "love it"]), "author": {"name": random.choice(FIRST), "verified": i % 2 == 0},
         "created_at": TODAY - timedelta(minutes=i)}
        for i in range(1, 101)
    ])  # fmt: skip


def orders_csv(rows: list[dict]) -> bytes:
    buf = io.StringIO()
    buf.write("order_id,customer_id,amount,status,order_date\n")
    for r in rows:
        buf.write(f"{r['order_id']},{r['customer_id']},{r['amount']},{r['status']},{r['order_date']}\n")
    return buf.getvalue().encode()


def seed_sftp() -> None:
    import fsspec

    fs = fsspec.filesystem("sftp", host="src-sftp", port=22, username="dev", password=PW, skip_instance_cache=True)
    fs.makedirs("/upload/orders", exist_ok=True)
    for f in fs.glob("/upload/orders/*"):
        fs.rm(f)
    data = orders()
    fs.pipe_file(f"/upload/orders/orders_{STAMP}_1.csv", orders_csv(data[:120]))
    fs.pipe_file(f"/upload/orders/orders_{STAMP}_2.csv", orders_csv(data[120:]))
    yesterday = (TODAY - timedelta(days=1)).strftime("%Y%m%d")
    fs.pipe_file(f"/upload/orders/orders_{yesterday}_1.csv", orders_csv(data[:5]))


def seed_ftp() -> None:
    import fsspec

    fs = fsspec.filesystem("ftp", host="src-ftp", port=21, username="dev", password=PW, skip_instance_cache=True)
    # pure-ftpd chroots the user to their home directory, so "/" is /home/dev.
    try:
        fs.mkdir("/exports")
    except Exception:
        pass
    lines = "\n".join(json.dumps({"invoice": f"INV-{i}", "amount": round(random.uniform(10, 900), 2),
                                  "currency": "EUR", "customer_id": random.randint(1, 60)}) for i in range(1, 51))  # fmt: skip
    fs.pipe_file(f"/exports/invoices_{STAMP}.jsonl", lines.encode() + b"\n")


def seed_smb() -> None:
    import fsspec

    fs = fsspec.filesystem("smb", host="src-smb", username="dev", password=PW, skip_instance_cache=True)
    fs.makedirs("/landing/finance", exist_ok=True)
    rows = "\n".join(f"{m},{random.choice(['ops', 'sales', 'it'])},{random.randint(1000, 9000)}" for m in range(1, 13))
    fs.pipe_file("/landing/finance/budget.csv", b"month,department,budget\n" + rows.encode() + b"\n")


def seed_s3() -> None:
    import s3fs

    fs = s3fs.S3FileSystem(key="devsource", secret="devsource-secret", client_kwargs={"endpoint_url": "http://src-s3:9000"},
                           skip_instance_cache=True)  # fmt: skip
    if not fs.exists("partner-drop"):
        fs.mkdir("partner-drop")
    import pyarrow as pa
    import pyarrow.parquet as pq

    buf = io.BytesIO()
    pq.write_table(pa.table({"sku": [f"SKU-{i:04d}" for i in range(1, 21)], "partner_price": [i * 1.5 for i in range(1, 21)]}), buf)
    fs.pipe_file(f"partner-drop/prices/prices_{STAMP}.parquet", buf.getvalue())


def seed_kafka() -> None:
    from confluent_kafka import Producer
    from confluent_kafka.admin import AdminClient, NewTopic

    servers = os.environ.get("SEED_KAFKA", "redpanda:9092")
    admin = AdminClient({"bootstrap.servers": servers})
    if "web.clickstream" not in admin.list_topics(timeout=10).topics:
        admin.create_topics([NewTopic("web.clickstream", 3, 1)])["web.clickstream"].result(10)
    p = Producer({"bootstrap.servers": servers})
    for i in range(1, 301):
        p.produce("web.clickstream", json.dumps({"session": f"s{i % 40}", "page": random.choice(["/", "/cart", "/p/1"]),
                                                  "ts": (TODAY - timedelta(seconds=i)).isoformat()}).encode(), key=f"s{i % 40}")  # fmt: skip
    p.flush(10)


def seed_landing() -> None:
    root = os.environ.get("SEED_LANDING", "/landing")
    os.makedirs(f"{root}/customers", exist_ok=True)
    with open(f"{root}/customers/customers_{STAMP}.csv", "w") as f:
        f.write("id,first_name,last_name,email,city\n")
        for c in customers(25):
            f.write(f"{c['id']},{c['first_name']},{c['last_name']},{c['email']},{c['city']}\n")


if __name__ == "__main__":
    only = set(sys.argv[1:])
    steps = {
        "landing": seed_landing, "postgres": seed_postgres, "mysql": seed_mysql, "mongo": seed_mongo,
        "sftp": seed_sftp, "ftp": seed_ftp, "smb": seed_smb, "s3": seed_s3, "kafka": seed_kafka, "mssql": seed_mssql,
    }  # fmt: skip
    if not only:
        only = set(steps) - {"mssql"}  # SQL Server only when its profile runs: seed.py mssql
    for name in steps:
        if name in only:
            retry(name, steps[name])
