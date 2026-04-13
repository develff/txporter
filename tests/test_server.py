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
        account = {"id": "consorsbank", "name": "consorsbank", "type": "fints", "aqbanking_id": 9}
        server_mod._pending_syncs["consorsbank"] = {
            "client": mock_client, "account": account,
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

    def test_export_format_json_returns_transactions(self, sync_client):
        self._inject_pending_sync(SAMPLE_TRANSACTIONS)
        resp = sync_client.post("/sync/consorsbank/confirm?export_format=json")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "ok"
        assert data["export_format"] == "json"
        assert len(data["transactions"]) == 1

    def test_export_format_csv_does_not_forward_to_targets(self, sync_client):
        self._inject_pending_sync(SAMPLE_TRANSACTIONS)
        with patch("src.server._forward_to_targets") as mock_fwd:
            resp = sync_client.post("/sync/consorsbank/confirm?export_format=csv")
        assert resp.status_code == 200
        mock_fwd.assert_not_called()

    def test_unknown_account_returns_404(self, sync_client):
        resp = sync_client.post("/sync/unknown/confirm?dry_run=true")
        assert resp.status_code == 404


class TestStartSyncExportFormat:
    """Tests for export_format in the no-TAN (inline) sync path."""

    def _mock_no_tan_sync(self, sync_client, export_format_body=None):
        import src.server as server_mod
        mock_client = MagicMock()
        mock_client.start_fetch.return_value = {"status": "ok", "transactions": SAMPLE_TRANSACTIONS}

        with patch("src.server.AqBankingClient", return_value=mock_client):
            body = {}
            if export_format_body:
                body["export_format"] = export_format_body
            resp = sync_client.post(
                "/sync/consorsbank",
                data=__import__("json").dumps(body),
                content_type="application/json",
            )
        return resp

    def test_no_tan_json_export_returns_transactions(self, sync_client):
        resp = self._mock_no_tan_sync(sync_client, export_format_body="json")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "ok"
        assert data["export_format"] == "json"
        assert len(data["transactions"]) == 1

    def test_no_tan_csv_export_skips_targets(self, sync_client):
        with patch("src.server._forward_to_targets") as mock_fwd:
            resp = self._mock_no_tan_sync(sync_client, export_format_body="csv")
        assert resp.status_code == 200
        mock_fwd.assert_not_called()

    def test_no_tan_no_export_format_forwards_to_targets(self, sync_client):
        with patch("src.server._forward_to_targets", return_value={}) as mock_fwd:
            resp = self._mock_no_tan_sync(sync_client, export_format_body=None)
        assert resp.status_code == 200
        mock_fwd.assert_called_once()


class TestWebUI:
    def test_index_returns_html(self, sync_client):
        resp = sync_client.get("/")
        assert resp.status_code == 200
        assert b"txporter" in resp.data
        assert b"text/html" in resp.content_type.encode()


class TestScheduler:
    def test_get_scheduler_defaults(self, sync_client):
        resp = sync_client.get("/scheduler")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["enabled"] is False
        assert data["next_run"] is None

    def test_post_scheduler_saves_and_returns_config(self, sync_client, tmp_path):
        resp = sync_client.post(
            "/scheduler",
            data=json.dumps({"enabled": True, "time": "20:00", "webhook_url": ""}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["enabled"] is True
        assert data["time"] == "20:00"
        assert data["next_run"] is not None

    def test_post_scheduler_persists_to_config(self, sync_client, tmp_path):
        sync_client.post(
            "/scheduler",
            data=json.dumps({"enabled": True, "time": "08:00", "webhook_url": "http://example.com/hook"}),
            content_type="application/json",
        )
        import src.setup as setup_mod
        saved = json.loads(open(setup_mod.CONFIG_PATH).read())
        assert saved["scheduler"]["enabled"] is True
        assert saved["scheduler"]["time"] == "08:00"
        assert saved["scheduler"]["webhook_url"] == "http://example.com/hook"

    def test_post_scheduler_invalid_time_format(self, sync_client):
        resp = sync_client.post(
            "/scheduler",
            data=json.dumps({"enabled": True, "time": "8pm"}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_post_scheduler_time_out_of_range(self, sync_client):
        resp = sync_client.post(
            "/scheduler",
            data=json.dumps({"enabled": True, "time": "25:00"}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_post_scheduler_disabled_skips_time_validation(self, sync_client):
        resp = sync_client.post(
            "/scheduler",
            data=json.dumps({"enabled": False, "time": ""}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        assert resp.get_json()["enabled"] is False

    def test_get_scheduler_returns_next_run_when_enabled(self, sync_client):
        sync_client.post(
            "/scheduler",
            data=json.dumps({"enabled": True, "time": "20:00", "webhook_url": ""}),
            content_type="application/json",
        )
        resp = sync_client.get("/scheduler")
        assert resp.status_code == 200
        assert resp.get_json()["next_run"] is not None


class TestLastSyncStatus:
    def test_last_sync_status_ok_in_accounts(self, sync_client, tmp_path):
        """After a successful sync, last_sync_status=ok is returned by /accounts."""
        import src.server as server_mod
        import src.setup as setup_mod

        mock_client = MagicMock()
        mock_client.start_fetch.return_value = {"status": "ok", "transactions": []}

        with patch("src.server.AqBankingClient", return_value=mock_client), \
             patch("src.server._forward_to_targets", return_value={}):
            sync_client.post(
                "/sync/consorsbank",
                data=json.dumps({}),
                content_type="application/json",
            )

        resp = sync_client.get("/accounts")
        assert resp.status_code == 200
        acc = next(a for a in resp.get_json() if a["id"] == "consorsbank")
        assert acc.get("last_sync_status") == "ok"


class TestTagsEndpoint:
    def test_returns_empty_list_when_firefly_disabled(self, sync_client):
        # sync_client fixture has firefly enabled=False
        resp = sync_client.get("/tags")
        assert resp.status_code == 200
        assert resp.get_json() == []

    def test_returns_tags_from_firefly(self, sync_client):
        import src.server as server_mod
        server_mod.config["targets"]["firefly"] = {
            "enabled": True, "url": "https://firefly.example.com", "token": "t",
        }
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {
            "data": [{"attributes": {"tag": "Food"}}, {"attributes": {"tag": "Travel"}}],
            "meta": {"pagination": {"total_pages": 1}},
        }
        with patch("src.firefly.requests.get", return_value=mock_resp):
            resp = sync_client.get("/tags")
        assert resp.status_code == 200
        assert resp.get_json() == ["Food", "Travel"]
        server_mod.config["targets"]["firefly"]["enabled"] = False

    def test_returns_empty_list_on_firefly_error(self, sync_client):
        import src.server as server_mod
        server_mod.config["targets"]["firefly"] = {
            "enabled": True, "url": "https://firefly.example.com", "token": "t",
        }
        with patch("src.firefly.requests.get", side_effect=Exception("boom")):
            resp = sync_client.get("/tags")
        assert resp.status_code == 200
        assert resp.get_json() == []
        server_mod.config["targets"]["firefly"]["enabled"] = False
