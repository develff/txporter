"""Pytest configuration: add src/ to sys.path and set env vars for server tests."""
import sys
import os
import json

# ── Env vars must be set BEFORE any src imports, since config.py reads them at
# module import time as module-level constants. ────────────────────────────────

_FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
_FIXTURE_CONFIG = os.path.join(_FIXTURE_DIR, "banks.test.json")

os.makedirs(_FIXTURE_DIR, exist_ok=True)
if not os.path.exists(_FIXTURE_CONFIG):
    with open(_FIXTURE_CONFIG, "w") as _f:
        json.dump({"accounts": [], "targets": {}}, _f)

os.environ.setdefault("TXPORTER_CONFIG", _FIXTURE_CONFIG)
os.environ.setdefault("TXPORTER_PROFILES", os.path.join(
    os.path.dirname(__file__), "..", "config", "bank_profiles.json"
))

# ── Add src/ to path so server.py's bare imports (aqbanking, config, setup) work.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# ── Ensure bare `import setup/aqbanking/config` (used by server.py) resolves to
# the same module object as `import src.*` (used in tests), so patches and class
# instances are consistent across both import styles. ─────────────────────────
import src.setup
import src.aqbanking
import src.config
sys.modules.setdefault("setup", src.setup)
sys.modules.setdefault("aqbanking", src.aqbanking)
sys.modules.setdefault("config", src.config)
