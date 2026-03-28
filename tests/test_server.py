"""Tests for sync REST endpoints."""

import json
import pytest
from unittest.mock import patch, MagicMock


SAMPLE_TRANSACTIONS = [
    {
        "external_id": "aqbanking:fints:1234:20260301:-179.95:EUR",
        "date": "20260301",
        "amount_eur": -179.95,
        "currency_code": "EUR",
        "transaction_text": "Effekten",
        "purpose": "WP-ABRECHNUNG Kauf",
        "remote_name": "",
        "remote_iban": "",
        "remote_account_number": "0366023258001",
    },
]


@pytest.fixture()
def sync_client(tmp_path):
    """Flask test client with a fints account that has an aqbanking_id."""
    config = {
        "accounts": [
            {"id": "consorsbank", "name": "consorsbank", "type": "fints",
             "blz": "76030080", "url": "u", "login": "l", "hbci_version": 300,
             "aqbanking_id": 9},
        ],
        "targets": {"firefly": {"enabled": False}},
    }
    config_path = str(tmp_path / "banks.json")
    with open(config_path, "w") as f:
        json.dump(config, f)

    import src.server as server_mod
    import src.setup as setup_mod

    orig_config = setup_mod.CONFIG_PATH
    setup_mod.CONFIG_PATH = config_path
    server_mod.config["accounts"] = config["accounts"]
    server_mod.config["targets"] = config["targets"]
    server_mod._pending_syncs.clear()

    yield server_mod.app.test_client()

    setup_mod.CONFIG_PATH = orig_config
    server_mod._pending_syncs.clear()


class TestSyncConfirmDryRun:
    def _inject_pending_sync(self, transactions):
        import src.server as server_mod
        mock_client = MagicMock()
        mock_client.complete_fetch.return_value = transactions
        mock_proc = MagicMock()
        account = {"id": "consorsbank", "name": "consorsbank", "type": "fints", "aqbanking_id": 9}
        server_mod._pending_syncs["consorsbank"] = {
            "proc": mock_proc, "client": mock_client, "account": account,
        }

    def test_dry_run_returns_transactions_as_json(self, sync_client):
        self._inject_pending_sync(SAMPLE_TRANSACTIONS)
        resp = sync_client.post("/sync/consorsbank/confirm?dry_run=true")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "dry_run"
        assert len(data["transactions"]) == 1
        assert data["transactions"][0]["amount_eur"] == -179.95

    def test_dry_run_does_not_forward_to_targets(self, sync_client):
        self._inject_pending_sync(SAMPLE_TRANSACTIONS)
        with patch("src.server._forward_to_targets") as mock_fwd:
            sync_client.post("/sync/consorsbank/confirm?dry_run=true")
        mock_fwd.assert_not_called()

    def test_normal_confirm_forwards_to_targets(self, sync_client):
        self._inject_pending_sync(SAMPLE_TRANSACTIONS)
        with patch("src.server._forward_to_targets") as mock_fwd:
            resp = sync_client.post("/sync/consorsbank/confirm")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "ok"
        mock_fwd.assert_called_once()

    def test_unknown_account_returns_404(self, sync_client):
        resp = sync_client.post("/sync/unknown/confirm?dry_run=true")
        assert resp.status_code == 404


class TestWebUI:
    def test_index_returns_html(self, sync_client):
        resp = sync_client.get("/")
        assert resp.status_code == 200
        assert b"txporter" in resp.data
        assert b"text/html" in resp.content_type.encode()
