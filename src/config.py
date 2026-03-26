"""
txporter - Configuration loader
"""

import json
import os

CONFIG_PATH = os.environ.get("TXPORTER_CONFIG", "/home/txporter/config/banks.json")


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)
