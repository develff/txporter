"""Tests for config.py — load/save and startup migration."""

import json
import os
import pytest

import src.config as config_mod


@pytest.fixture()
def config_dir(tmp_path):
    orig = config_mod.CONFIG_PATH
    config_mod.CONFIG_PATH = str(tmp_path / "config.json")
    yield tmp_path
    config_mod.CONFIG_PATH = orig


class TestEnsureConfigExists:
    def test_creates_minimal_config_when_absent(self, config_dir):
        config_mod.ensure_config_exists(str(config_dir / "config.json"))
        assert os.path.exists(config_dir / "config.json")
        data = json.loads((config_dir / "config.json").read_text())
        assert data["accounts"] == []
        assert "firefly" in data["targets"]
        assert "csv" in data["targets"]

    def test_does_not_overwrite_existing_config(self, config_dir):
        path = config_dir / "config.json"
        path.write_text(json.dumps({"accounts": [{"id": "x"}], "targets": {}}))
        config_mod.ensure_config_exists(str(path))
        data = json.loads(path.read_text())
        assert data["accounts"][0]["id"] == "x"

    def test_migrates_banks_json_when_config_absent(self, config_dir):
        legacy = config_dir / "banks.json"
        legacy.write_text(json.dumps({"accounts": [{"id": "dkb"}], "targets": {}}))
        config_path = config_dir / "config.json"

        config_mod.ensure_config_exists(str(config_path))

        assert config_path.exists()
        assert not legacy.exists()
        data = json.loads(config_path.read_text())
        assert data["accounts"][0]["id"] == "dkb"

    def test_banks_json_not_touched_when_config_already_exists(self, config_dir):
        config_path = config_dir / "config.json"
        config_path.write_text(json.dumps({"accounts": [], "targets": {}}))
        legacy = config_dir / "banks.json"
        legacy.write_text(json.dumps({"accounts": [{"id": "old"}], "targets": {}}))

        config_mod.ensure_config_exists(str(config_path))

        assert legacy.exists()
        data = json.loads(config_path.read_text())
        assert data["accounts"] == []
