"""
txporter - Configuration loader
"""

import json
import os

CONFIG_PATH = os.environ.get("TXPORTER_CONFIG", "/home/txporter/config/config.json")
CATALOG_PATH = os.environ.get("TXPORTER_CATALOG", "/home/txporter/config/bank_profiles.json")

# Fields that live in the catalog and are stripped from user config on save
_CATALOG_FIELDS = {"blz", "url", "hbci_version", "tan_mode", "type"}


def load_catalog() -> dict:
    try:
        with open(CATALOG_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def _merge_catalog(accounts: list, catalog: dict) -> list:
    """Merge catalog fields into accounts that reference a catalog entry."""
    result = []
    for account in accounts:
        acc = dict(account)
        ref = acc.get("catalog_ref")
        if ref and ref in catalog:
            for field in _CATALOG_FIELDS:
                if field in catalog[ref] and field not in acc:
                    acc[field] = catalog[ref][field]
        result.append(acc)
    return result


def _strip_catalog(accounts: list) -> list:
    """Remove catalog-derived fields from accounts that have a catalog_ref."""
    result = []
    for account in accounts:
        acc = dict(account)
        if acc.get("catalog_ref"):
            for field in _CATALOG_FIELDS:
                acc.pop(field, None)
        result.append(acc)
    return result


def load_config(path: str = None) -> dict:
    path = path or CONFIG_PATH
    with open(path) as f:
        cfg = json.load(f)
    catalog = load_catalog()
    cfg["accounts"] = _merge_catalog(cfg.get("accounts", []), catalog)
    return cfg


def save_config(cfg: dict, path: str = None) -> None:
    path = path or CONFIG_PATH
    to_save = dict(cfg)
    to_save["accounts"] = _strip_catalog(cfg.get("accounts", []))
    with open(path, "w") as f:
        json.dump(to_save, f, indent=2)
        f.write("\n")


def ensure_config_exists(path: str = None) -> None:
    """Create a minimal config.json if none exists yet."""
    path = path or CONFIG_PATH
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        save_config({
            "accounts": [],
            "targets": {
                "firefly": {"enabled": False, "url": "http://firefly:8080", "token": ""},
                "csv": {"enabled": False, "path": "/home/txporter/output"},
            },
        }, path)
