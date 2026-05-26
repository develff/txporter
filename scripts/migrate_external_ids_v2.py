#!/usr/bin/env python3
"""Migration script v2: recompute external_ids to use the multi-field fingerprint fallback.

The new suffix priority (mirrors external_id.build_external_id):
  1. sepa_ct_id      (endToEndReference — stored by txporter during AqBanking/CSV import)
  2. sha256(destination_iban|destination_name|description)[:8]
     All three fields are stored in Firefly from both AqBanking and CSV import paths,
     so the fingerprint is identical for the same real transaction regardless of origin.

Old schemes this replaces:
  - primanota (internal_reference) as suffix — AqBanking-only, not reproducible from CSV
  - sha256(description)[:8] alone — less unique than the multi-field fingerprint

Only transactions whose external_id starts with 'txporter:' and whose new external_id
differs from the current one are updated.

The script is idempotent: re-running it is safe.

Usage:
    python3 scripts/migrate_external_ids_v2.py --url https://firefly.example.com --token <pat>
    python3 scripts/migrate_external_ids_v2.py --dry-run   # preview only
"""

import argparse
import hashlib
import os
import sys
import time

try:
    import requests
except ImportError:
    print("requests is required: pip install requests", file=sys.stderr)
    sys.exit(1)

_NOTPROVIDED = frozenset({"NOTPROVIDED", "notprovided"})


def _new_external_id(iban: str, date: str, amount: str, currency: str,
                     sepa_ct_id: str, dest_iban: str, dest_name: str,
                     description: str) -> str:
    if sepa_ct_id and sepa_ct_id not in _NOTPROVIDED:
        suffix = sepa_ct_id
    else:
        fingerprint = "|".join([dest_iban or "", dest_name or "", description or ""])
        suffix = hashlib.sha256(fingerprint.encode()).hexdigest()[:8]
    return f"txporter:{iban}:{date}:{amount}:{currency}:{suffix}"


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json",
            "Content-Type": "application/json"}


def fetch_all_transactions(base_url: str, token: str) -> list:
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

    updated = skipped = errors = 0
    for group in groups:
        group_id = group["id"]
        splits = group.get("attributes", {}).get("transactions", [])
        new_splits = []
        changed = False

        for split in splits:
            ext_id = (split.get("external_id") or "").strip()
            if not ext_id.startswith("txporter:"):
                new_splits.append(split)
                continue

            parts = ext_id.split(":")
            # Format: txporter:{iban}:{date}:{amount}:{currency}:{suffix}
            if len(parts) < 6:
                new_splits.append(split)
                continue

            iban     = parts[1]
            date     = parts[2]
            amount   = parts[3]
            currency = parts[4]

            sepa_ct_id  = (split.get("sepa_ct_id") or "").strip()
            description = (split.get("description") or "").strip()
            # Firefly stores counterparty as destination for withdrawals, source for deposits
            dest_iban = (split.get("destination_iban") or
                         split.get("source_iban") or "").strip()
            dest_name = (split.get("destination_name") or
                         split.get("source_name") or "").strip()

            new_id = _new_external_id(iban, date, amount, currency,
                                      sepa_ct_id, dest_iban, dest_name, description)
            if new_id != ext_id:
                print(f"  {'[dry-run] ' if dry_run else ''}PATCH {group_id}: "
                      f"{ext_id!r}\n           → {new_id!r}")
                new_splits.append({**split, "external_id": new_id})
                changed = True
            else:
                new_splits.append(split)

        if not changed:
            skipped += 1
            continue

        if dry_run:
            updated += 1
            continue

        try:
            resp = requests.put(
                f"{base_url}/api/v1/transactions/{group_id}",
                headers=_headers(token),
                json={"transactions": new_splits},
                timeout=30,
            )
            resp.raise_for_status()
            updated += 1
            time.sleep(0.05)
        except requests.HTTPError as exc:
            print(f"  ERROR {group_id}: {exc}", file=sys.stderr)
            errors += 1

    print(f"\nDone. updated={updated}, unchanged={skipped}, errors={errors}")
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
