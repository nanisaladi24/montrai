#!/usr/bin/env python
"""
Polygon / massive.com S3 flat-file sync.

Downloads daily aggregate .csv.gz files from the massive.com flatfiles bucket
into a local cache (data/polygon_s3/). Incremental — skips dates already on
disk. One file per trading day, one row per ticker.

Usage:
    python scripts/polygon_s3_sync.py --dataset stocks    --start 2026-01-01 --end 2026-04-20
    python scripts/polygon_s3_sync.py --dataset options   --start 2026-04-01 --end 2026-04-20
    python scripts/polygon_s3_sync.py --dataset indices   --start 2024-01-01 --end 2026-04-20
    python scripts/polygon_s3_sync.py --dataset all       --days 30

Datasets:
    stocks     us_stocks_sip/day_aggs_v1/
    options    us_options_opra/day_aggs_v1/  (per-contract OCC rows)
    indices    us_indices/day_aggs_v1/
    crypto     global_crypto/day_aggs_v1/
    forex      global_forex/day_aggs_v1/

Env (read from .env):
    POLYGON_S3_ACCESS_KEY, POLYGON_S3_SECRET_KEY,
    POLYGON_S3_ENDPOINT (default https://files.massive.com),
    POLYGON_S3_BUCKET   (default flatfiles)
"""
from __future__ import annotations
import argparse
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


DATASETS = {
    "stocks":  "us_stocks_sip/day_aggs_v1",
    "options": "us_options_opra/day_aggs_v1",
    "indices": "us_indices/day_aggs_v1",
    "crypto":  "global_crypto/day_aggs_v1",
    "forex":   "global_forex/day_aggs_v1",
}


def _client():
    return boto3.client(
        "s3",
        endpoint_url=os.getenv("POLYGON_S3_ENDPOINT", "https://files.massive.com"),
        aws_access_key_id=os.environ["POLYGON_S3_ACCESS_KEY"],
        aws_secret_access_key=os.environ["POLYGON_S3_SECRET_KEY"],
        config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
    )


def _cache_root() -> Path:
    root = Path(__file__).resolve().parent.parent / "data" / "polygon_s3"
    root.mkdir(parents=True, exist_ok=True)
    return root


def daterange(start: date, end: date):
    d = start
    while d <= end:
        if d.weekday() < 5:  # skip weekends (market holidays still attempted)
            yield d
        d += timedelta(days=1)


def s3_key(dataset_prefix: str, d: date) -> str:
    return f"{dataset_prefix}/{d.year}/{d.month:02d}/{d.isoformat()}.csv.gz"


def local_path(dataset_prefix: str, d: date) -> Path:
    return _cache_root() / dataset_prefix / f"{d.year}" / f"{d.month:02d}" / f"{d.isoformat()}.csv.gz"


def sync(dataset: str, start: date, end: date, bucket: str, force: bool = False) -> tuple[int, int, int]:
    """Return (downloaded, skipped_existing, missing_at_source)."""
    if dataset not in DATASETS:
        raise ValueError(f"Unknown dataset '{dataset}'. Options: {', '.join(DATASETS)}")
    prefix = DATASETS[dataset]
    client = _client()

    downloaded = skipped = missing = 0
    for d in daterange(start, end):
        key = s3_key(prefix, d)
        dest = local_path(prefix, d)
        if dest.exists() and not force:
            skipped += 1
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            client.download_file(bucket, key, str(dest))
            size_mb = dest.stat().st_size / 1024 / 1024
            print(f"  ✓ {dataset:8s} {d} ({size_mb:.1f} MB)")
            downloaded += 1
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("NoSuchKey", "404"):
                missing += 1  # weekend or holiday
            else:
                print(f"  ✗ {dataset:8s} {d}: {code}")
    return downloaded, skipped, missing


def main():
    ap = argparse.ArgumentParser(description="Sync Polygon S3 flat files locally.")
    ap.add_argument("--dataset", choices=list(DATASETS) + ["all"], required=True)
    ap.add_argument("--start", help="YYYY-MM-DD (inclusive)")
    ap.add_argument("--end",   help="YYYY-MM-DD (inclusive)")
    ap.add_argument("--days",  type=int, help="Last N calendar days ending today (alt to --start/--end)")
    ap.add_argument("--force", action="store_true", help="Re-download files that already exist locally")
    args = ap.parse_args()

    if args.days:
        end = date.today()
        start = end - timedelta(days=args.days)
    elif args.start and args.end:
        start = datetime.strptime(args.start, "%Y-%m-%d").date()
        end = datetime.strptime(args.end, "%Y-%m-%d").date()
    else:
        ap.error("Provide --days N or both --start and --end")

    bucket = os.getenv("POLYGON_S3_BUCKET", "flatfiles")
    datasets = list(DATASETS) if args.dataset == "all" else [args.dataset]

    totals = {"downloaded": 0, "skipped": 0, "missing": 0}
    for ds in datasets:
        print(f"\n── {ds} ({DATASETS[ds]}) {start} → {end} ──")
        dl, sk, mi = sync(ds, start, end, bucket, force=args.force)
        totals["downloaded"] += dl
        totals["skipped"] += sk
        totals["missing"] += mi

    print(f"\nDone. downloaded={totals['downloaded']} skipped={totals['skipped']} missing={totals['missing']}")


if __name__ == "__main__":
    main()
