#!/usr/bin/env python3
"""Migration script: rename external_id prefix aqbanking:fints: → txporter:

Run once against a live Firefly III instance to update all transactions that
were imported before the external_id prefix was unified to 'txporter'.

The script is idempotent: transactions already using 'txporter:' are skipped.

Usage:
    python3 scripts/migrate_external_ids.py --url https://firefly.example.com --token <pat>

Or set environment variables FIREFLY_URL and FIREFLY_TOKEN instead of flags.
"""

import argparse
import os
import sys
import time

try:
    import requests
except ImportError:
    print("requests is required: pip install requests", file=sys.stderr)
    sys.exit(1)

OLD_PREFIX = "aqbanking:fints:"
NEW_PREFIX = "txporter:"


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json",
            "Content-Type": "application/json"}


def fetch_all_transactions(base_url: str, token: str) -> list:
    """Return all transaction groups from Firefly III, all pages."""
    results = []
    page = 1
    while True:
        resp = requests.get(
            f"{base_url}/api/v1/transactions",
            headers=_headers(token),
            params={"page": page, "limit": 100},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        results.extend(data.get("data", []))
        meta = data.get("meta", {}).get("pagination", {})
        if page >= meta.get("total_pages", 1):
            break
        page += 1
    return results


def migrate(base_url: str, token: str, dry_run: bool) -> None:
    base_url = base_url.rstrip("/")
    print(f"Fetching all transactions from {base_url} …")
    groups = fetch_all_transactions(base_url, token)
    print(f"  → {len(groups)} transaction groups fetched")

    migrated = skipped = errors = 0
    for group in groups:
        group_id = group["id"]
        transactions = group.get("attributes", {}).get("transactions", [])
        for split in transactions:
            ext_id = (split.get("external_id") or "").strip()
            if not ext_id.startswith(OLD_PREFIX):
                continue
            new_ext_id = NEW_PREFIX + ext_id[len(OLD_PREFIX):]
            if dry_run:
                print(f"  [dry-run] PATCH /api/v1/transactions/{group_id}: "
                      f"{ext_id!r} → {new_ext_id!r}")
                migrated += 1
                continue
            try:
                resp = requests.put(
                    f"{base_url}/api/v1/transactions/{group_id}",
                    headers=_headers(token),
                    json={"transactions": [{**split, "external_id": new_ext_id}]},
                    timeout=30,
                )
                resp.raise_for_status()
                print(f"  PATCHED {group_id}: {ext_id!r} → {new_ext_id!r}")
                migrated += 1
                time.sleep(0.05)
            except requests.HTTPError as exc:
                print(f"  ERROR {group_id}: {exc}", file=sys.stderr)
                errors += 1

    print(f"\nDone. migrated={migrated}, skipped={skipped}, errors={errors}")
    if errors:
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=os.environ.get("FIREFLY_URL", ""),
                        help="Firefly III base URL (or set FIREFLY_URL)")
    parser.add_argument("--token", default=os.environ.get("FIREFLY_TOKEN", ""),
                        help="Firefly III personal access token (or set FIREFLY_TOKEN)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be changed without writing anything")
    args = parser.parse_args()

    if not args.url or not args.token:
        parser.error("--url and --token are required (or set FIREFLY_URL / FIREFLY_TOKEN)")

    migrate(args.url, args.token, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
