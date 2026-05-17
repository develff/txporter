"""
Shared external_id builder for all txporter import paths.

Format: txporter:{account_id}:{date}:{amount:.2f}:{currency}:{suffix}

  account_id  — IBAN for bank profiles; "{mapping_id}:{account_name}" for generic CSV profiles
  date        — compact YYYYMMDD
  suffix priority:
    1. end_to_end_ref  — when present and not NOTPROVIDED (aligns FinTS and CSV paths)
    2. sha256(remote_iban|remote_name|description)[:8]
       remote_iban and remote_name come from the same SEPA data in both FinTS and CSV,
       so the fingerprint is identical for the same transaction regardless of import path.
"""

import hashlib

_NOTPROVIDED = frozenset({"NOTPROVIDED", "notprovided"})


def build_external_id(account_id: str, date: str, amount: float, currency: str,
                      end_to_end_ref: str = "", remote_iban: str = "",
                      remote_name: str = "", description: str = "") -> str:
    if end_to_end_ref and end_to_end_ref not in _NOTPROVIDED:
        suffix = end_to_end_ref
    else:
        fingerprint = "|".join([remote_iban, remote_name, description])
        suffix = hashlib.sha256(fingerprint.encode()).hexdigest()[:8]
    return f"txporter:{account_id}:{date}:{amount:.2f}:{currency}:{suffix}"
