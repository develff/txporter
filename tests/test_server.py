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


class TestHealthEndpoint:
    def test_health_returns_ok(self, sync_client):
        resp = sync_client.get("/health")
        assert resp.status_code == 200
        assert resp.get_json() == {"status": "ok"}


class TestSchedulerEdgeCases:
    def test_invalid_webhook_url_scheme_returns_400(self, sync_client):
        resp = sync_client.post(
            "/scheduler",
            data=json.dumps({"enabled": False, "webhook_url": "ftp://example.com"}),
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert "webhook_url" in resp.get_json()["error"]

    def test_invalid_timezone_returns_400(self, sync_client):
        resp = sync_client.post(
            "/scheduler",
            data=json.dumps({"enabled": False, "timezone": "Invalid/Zone"}),
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert "timezone" in resp.get_json()["error"].lower()

    def test_get_scheduler_config_exception_returns_empty(self, sync_client):
        import src.server as server_mod
        with patch("src.server.bank_setup.load_config", side_effect=Exception("disk error")):
            cfg = server_mod.get_scheduler_config()
        assert cfg == {}


class TestAccountEndpoints:
    def test_toggle_account_disables_account(self, sync_client):
        import src.server as server_mod
        resp = sync_client.post("/accounts/consorsbank/toggle")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "ok"
        assert data["enabled"] is False

    def test_toggle_account_unknown_returns_404(self, sync_client):
        resp = sync_client.post("/accounts/unknown/toggle")
        assert resp.status_code == 404

    def test_rename_account_success(self, sync_client):
        resp = sync_client.post(
            "/accounts/consorsbank/rename",
            data=json.dumps({"name": "My Consors"}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "ok"

    def test_rename_account_missing_name_returns_400(self, sync_client):
        resp = sync_client.post(
            "/accounts/consorsbank/rename",
            data=json.dumps({}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_rename_account_unknown_returns_404(self, sync_client):
        resp = sync_client.post(
            "/accounts/unknown/rename",
            data=json.dumps({"name": "X"}),
            content_type="application/json",
        )
        assert resp.status_code == 404


class TestSyncEndpoints:
    def test_sync_one_unknown_account_returns_404(self, sync_client):
        resp = sync_client.post(
            "/sync/unknown",
            data=json.dumps({}),
            content_type="application/json",
        )
        assert resp.status_code == 404

    def test_sync_all_skips_disabled_account(self, sync_client):
        import src.server as server_mod
        server_mod.config["accounts"][0]["enabled"] = False
        resp = sync_client.post("/sync", data=json.dumps({}), content_type="application/json")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["consorsbank"]["status"] == "skipped"
        server_mod.config["accounts"][0].pop("enabled", None)

    def test_sync_all_skips_account_without_aqbanking_id(self, sync_client):
        import src.server as server_mod
        orig = server_mod.config["accounts"][0].pop("aqbanking_id")
        resp = sync_client.post("/sync", data=json.dumps({}), content_type="application/json")
        assert resp.status_code == 200
        assert "consorsbank" not in resp.get_json()
        server_mod.config["accounts"][0]["aqbanking_id"] = orig


class TestFireWebhook:
    def test_fire_webhook_posts_payload(self):
        import src.server as server_mod
        with patch("src.server._requests.post") as mock_post:
            server_mod._fire_webhook("http://hook.example.com", "acc1", "sync error")
        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args
        assert call_kwargs[0][0] == "http://hook.example.com"
        payload = call_kwargs[1]["json"]
        assert payload["account"] == "acc1"
        assert payload["error"] == "sync error"

    def test_fire_webhook_empty_url_does_nothing(self):
        import src.server as server_mod
        with patch("src.server._requests.post") as mock_post:
            server_mod._fire_webhook("", "acc1", "error")
        mock_post.assert_not_called()

    def test_fire_webhook_exception_is_swallowed(self):
        import src.server as server_mod
        with patch("src.server._requests.post", side_effect=Exception("timeout")):
            server_mod._fire_webhook("http://hook.example.com", "acc1", "error")


class TestSaveLastSyncError:
    def test_saves_error_status_to_config(self, sync_client, tmp_path):
        import src.server as server_mod
        server_mod._save_last_sync_error("consorsbank", "error", "something failed")
        import src.setup as setup_mod
        saved = json.loads(open(setup_mod.CONFIG_PATH).read())
        acc = next(a for a in saved["accounts"] if a["id"] == "consorsbank")
        assert acc["last_sync_status"] == "error"
        assert acc["last_sync_error"] == "something failed"

    def test_save_error_exception_is_swallowed(self):
        import src.server as server_mod
        with patch("src.server.bank_setup.load_config", side_effect=Exception("disk error")):
            server_mod._save_last_sync_error("acc1", "error", "msg")


class TestForwardToTargets:
    def test_csv_target_writes_file(self, tmp_path):
        import src.server as server_mod
        orig_targets = server_mod.config["targets"].copy()
        csv_path = str(tmp_path / "output")
        server_mod.config["targets"] = {
            "csv": {"enabled": True, "path": csv_path},
        }
        account = {"id": "testaccount"}
        transactions = [{"date": "20260101", "amount": 10.0, "description": "test", "iban": "DE123"}]
        server_mod._forward_to_targets(transactions, account)
        assert (tmp_path / "output" / "testaccount.csv").exists()
        server_mod.config["targets"] = orig_targets

    def test_disabled_target_is_skipped(self, tmp_path):
        import src.server as server_mod
        orig_targets = server_mod.config["targets"].copy()
        csv_path = str(tmp_path / "output")
        server_mod.config["targets"] = {
            "csv": {"enabled": False, "path": csv_path},
        }
        server_mod._forward_to_targets([], {"id": "acc"})
        assert not (tmp_path / "output").exists()
        server_mod.config["targets"] = orig_targets


class TestRunScheduledSync:
    def test_skips_when_aqbanking_busy(self):
        import src.server as server_mod
        with patch("src.server.aqbanking_is_busy", return_value=True), \
             patch("src.server.start_sync") as mock_sync:
            server_mod._run_scheduled_sync()
        mock_sync.assert_not_called()

    def test_calls_start_sync_for_enabled_accounts(self, sync_client):
        import src.server as server_mod
        with patch("src.server.aqbanking_is_busy", return_value=False), \
             patch("src.server.start_sync", return_value={"status": "ok"}) as mock_sync:
            server_mod._run_scheduled_sync()
        mock_sync.assert_called_once()

    def test_fires_webhook_on_error(self, sync_client):
        import src.server as server_mod
        sched_cfg = {"enabled": True, "time": "20:00", "webhook_url": "http://hook.example.com"}
        with patch("src.server.aqbanking_is_busy", return_value=False), \
             patch("src.server.get_scheduler_config", return_value=sched_cfg), \
             patch("src.server.start_sync", return_value={"status": "error", "message": "fail"}), \
             patch("src.server._fire_webhook") as mock_webhook, \
             patch("src.server._save_last_sync_error"):
            server_mod._run_scheduled_sync()
        mock_webhook.assert_called_once_with("http://hook.example.com", "consorsbank", "fail")
