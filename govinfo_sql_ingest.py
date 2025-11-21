#!/usr/bin/env python3
"""
GovInfo + GovInfo Bulk + GPO API + GovInfo Search
Unified PostgreSQL Ingest CLI

DB:
  host=100.90.23.60
  db=opendiscourse
  user=opendiscourse
  pass=opendiscourse123

This script:
  1. Fetches ALL GovInfo collections.
  2. For each collection, fetches all packageIds (last 20 years).
  3. Retrieves summary metadata.
  4. Inserts into PostgreSQL tables:
       collections
       packages
       downloads

Run:
  export GOVINFO_API_KEY=YOUR_KEY
  python govinfo_sql_ingest.py ingest --years 20

"""

import os
import sys
import json
import logging
import datetime as dt
import psycopg2
import requests
from psycopg2.extras import execute_values
from argparse import ArgumentParser

API = "https://api.govinfo.gov"
DB = {
    "host": "100.90.23.60",
    "dbname": "opendiscourse",
    "user": "opendiscourse",
    "password": "opendiscourse123",
}

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")

# ------------------------------------------------------------
# DB SETUP
# ------------------------------------------------------------
def db_connect():
    return psycopg2.connect(**DB)

def ensure_tables():
    sql = """
    CREATE TABLE IF NOT EXISTS collections (
        collection_code TEXT PRIMARY KEY,
        collection_name TEXT,
        package_count BIGINT,
        granule_count BIGINT,
        raw JSONB
    );

    CREATE TABLE IF NOT EXISTS packages (
        package_id TEXT PRIMARY KEY,
        collection_code TEXT,
        date_issued TEXT,
        last_modified TEXT,
        summary JSONB
    );

    CREATE TABLE IF NOT EXISTS downloads (
        id SERIAL PRIMARY KEY,
        package_id TEXT,
        format TEXT,
        url TEXT
    );
    """
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(sql)
    conn.commit()
    conn.close()

# ------------------------------------------------------------
# API HELPERS
# ------------------------------------------------------------
def api_key():
    k = os.getenv("GOVINFO_API_KEY")
    if not k:
        logging.error("Missing GOVINFO_API_KEY env var")
        sys.exit(1)
    return k

def get_json(url, params=None):
    params = params or {}
    params["api_key"] = api_key()
    r = requests.get(url, params=params, timeout=60)
    r.raise_for_status()
    return r.json()

# ------------------------------------------------------------
# INGEST COLLECTIONS
# ------------------------------------------------------------
def ingest_collections():
    url = f"{API}/collections"
    data = get_json(url)
    items = data.get("collections", [])

    rows = []
    for c in items:
        rows.append((
            c.get("collectionCode"),
            c.get("collectionName"),
            c.get("packageCount"),
            c.get("granuleCount"),
            json.dumps(c)
        ))

    conn = db_connect()
    cur = conn.cursor()
    execute_values(cur,
        "INSERT INTO collections VALUES %s ON CONFLICT (collection_code) DO UPDATE SET raw = EXCLUDED.raw",
        rows
    )
    conn.commit()
    conn.close()

    return [r[0] for r in rows]

# ------------------------------------------------------------
# INGEST PACKAGES (Published service)
# ------------------------------------------------------------
def date_range_years(years):
    end = dt.date.today()
    start = dt.date(end.year - years, end.month, end.day)
    return start.isoformat(), end.isoformat()

def fetch_packages_for_collection(code, start, end):
    url = f"{API}/published/{start}/{end}"
    offset = "*"
    all_items = []

    while True:
        params = {"collection": code, "offsetMark": offset, "pageSize": 500}
        data = get_json(url, params)
        items = data.get("packages") or data.get("results") or []
        if not items:
            break

        for p in items:
            all_items.append({
                "packageId": p.get("packageId"),
                "dateIssued": p.get("dateIssued"),
                "lastModified": p.get("lastModified"),
            })

        nxt = data.get("nextPage")
        if not nxt:
            break
        if "offsetMark=" in nxt:
            offset = nxt.split("offsetMark=")[-1]
        else:
            offset = nxt

    return all_items

# ------------------------------------------------------------
# INGEST PACKAGE SUMMARY & DOWNLOAD LINKS
# ------------------------------------------------------------
def fetch_summary(pid):
    url = f"{API}/packages/{pid}/summary"
    return get_json(url)

def ingest_packages(code, items):
    conn = db_connect()
    cur = conn.cursor()

    pkg_rows = []
    dl_rows = []

    for p in items:
        pid = p["packageId"]
        if not pid:
            continue

        try:
            s = fetch_summary(pid)
        except Exception:
            continue

        pkg_rows.append((
            pid,
            code,
            p.get("dateIssued"),
            p.get("lastModified"),
            json.dumps(s)
        ))

        d = s.get("download", {})
        for fmt, url in d.items():
            dl_rows.append((pid, fmt, url))

    if pkg_rows:
        execute_values(cur,
            "INSERT INTO packages VALUES %s ON CONFLICT (package_id) DO UPDATE SET summary = EXCLUDED.summary",
            pkg_rows
        )

    if dl_rows:
        execute_values(cur,
            "INSERT INTO downloads (package_id, format, url) VALUES %s",
            dl_rows
        )

    conn.commit()
    conn.close()

# ------------------------------------------------------------
# MAIN CLI
# ------------------------------------------------------------
def main():
    ap = ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")

    ing = sub.add_parser("ingest")
    ing.add_argument("--years", type=int, default=20)

    args = ap.parse_args()

    if args.cmd == "ingest":
        ensure_tables()
        logging.info("Ingesting collections…")
        cols = ingest_collections()

        start, end = date_range_years(args.years)
        logging.info(f"Date range: {start} → {end}")

        for code in cols:
            logging.info(f"Processing {code}…")
            items = fetch_packages_for_collection(code, start, end)
            logging.info(f"Found {len(items)} packages for {code}")
            ingest_packages(code, items)

        logging.info("DONE.")

if __name__ == "__main__":
    main()