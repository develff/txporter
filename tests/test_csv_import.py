"""Tests for CSV import parsing, mapping and external_id generation."""

import io
import json
import os
import pytest
from unittest.mock import patch, MagicMock

from src.csv_import import (
    preview_csv,
    parse_and_map,
    build_external_id,
    load_mappings,
    save_mappings,
    _parse_date,
    _parse_amount,
    _resolve,
)

# ── Fixtures ───────────────────────────────────────────────────────────────────

SAMPLE_CSV = (
    "Timestamp (UTC),Transaction Description,Currency,Amount,To Currency,To Amount\n"
    "2024-01-15 09:23:41,Supermarkt Berlin,EUR,-42.50,,\n"
    "2024-01-16 14:05:12,Amazon DE,EUR,-29.99,,\n"
    "2024-01-17 08:00:00,Cashback,EUR,0.43,,\n"
    "2024-01-18 11:30:55,BTC Purchase,EUR,-200.00,BTC,0.00321000\n"
)

CRYPTO_COM_MAPPING = {
    "id": "crypto-com-visa",
    "name": "Crypto.com Visa",
    "delimiter": ",",
    "encoding": "utf-8",
    "skip_rows": 0,
    "account_name": "Crypto.com Visa",
    "fields": {
        "date":                  {"column": "Timestamp (UTC)", "date_format": "%Y-%m-%d %H:%M:%S"},
        "amount":                {"column": "Amount"},
        "currency_code":         {"column": "Currency"},
        "description":           {"column": "Transaction Description"},
        "foreign_amount":        {"column": "To Amount"},
        "foreign_currency_code": {"column": "To Currency"},
    },
}

FIXTURE_CSV_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "crypto_com_sample.csv")


# ── _parse_date ────────────────────────────────────────────────────────────────

class TestParseDate:
    def test_datetime_to_date(self):
        assert _parse_date("2024-01-15 09:23:41", "%Y-%m-%d %H:%M:%S") == "2024-01-15"

    def test_date_only_format(self):
        assert _parse_date("15.01.2024", "%d.%m.%Y") == "2024-01-15"

    def test_iso_date_passthrough(self):
        assert _parse_date("2024-01-15", "%Y-%m-%d") == "2024-01-15"

    def test_empty_string_returns_empty(self):
        assert _parse_date("", "%Y-%m-%d") == ""

    def test_no_format_returns_raw(self):
        assert _parse_date("2024-01-15", "") == "2024-01-15"

    def test_invalid_value_returns_raw(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING, logger="src.csv_import"):
            result = _parse_date("not-a-date", "%Y-%m-%d")
        assert result == "not-a-date"
        assert "Could not parse date" in caplog.text


# ── _parse_amount ──────────────────────────────────────────────────────────────

class TestParseAmount:
    def test_negative_amount(self):
        assert _parse_amount("-42.50") == -42.50

    def test_positive_amount(self):
        assert _parse_amount("0.43") == 0.43

    def test_european_decimal(self):
        assert _parse_amount("42,50", decimal_sep=",") == 42.50

    def test_european_thousands(self):
        assert _parse_amount("1.234,56", decimal_sep=",", thousands_sep=".") == 1234.56

    def test_empty_returns_zero(self):
        assert _parse_amount("") == 0.0

    def test_invalid_returns_zero(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING, logger="src.csv_import"):
            result = _parse_amount("N/A")
        assert result == 0.0
        assert "Could not parse amount" in caplog.text

    def test_whitespace_stripped(self):
        assert _parse_amount("  -5.00  ") == -5.0


# ── _resolve ───────────────────────────────────────────────────────────────────

class TestResolve:
    def test_column_lookup(self):
        assert _resolve({"Amount": "-42.50"}, {"column": "Amount"}) == "-42.50"

    def test_fixed_value(self):
        assert _resolve({"Amount": "-42.50"}, {"value": "EUR"}) == "EUR"

    def test_fixed_value_takes_priority_over_column(self):
        assert _resolve({"col": "x"}, {"column": "col", "value": "fixed"}) == "fixed"

    def test_missing_column_returns_empty(self):
        assert _resolve({}, {"column": "NoSuchColumn"}) == ""

    def test_empty_config_returns_empty(self):
        assert _resolve({"x": "1"}, {}) == ""


# ── build_external_id ──────────────────────────────────────────────────────────

class TestBuildExternalId:
    def test_format(self):
        ext_id = build_external_id("crypto-com-visa", "Crypto.com Visa",
                                   "2024-01-15", -42.50, "EUR", "Supermarkt Berlin")
        parts = ext_id.split(":")
        assert parts[0] == "csv"
        assert parts[1] == "crypto-com-visa"
        assert parts[2] == "Crypto.com Visa"
        assert parts[3] == "2024-01-15"
        assert parts[4] == "-42.50"
        assert parts[5] == "EUR"
        assert len(parts[6]) == 8          # 8-char hex hash

    def test_stable(self):
        a = build_external_id("m", "acc", "2024-01-01", -10.0, "EUR", "Shop")
        b = build_external_id("m", "acc", "2024-01-01", -10.0, "EUR", "Shop")
        assert a == b

    def test_different_descriptions_produce_different_ids(self):
        a = build_external_id("m", "acc", "2024-01-01", -10.0, "EUR", "Shop A")
        b = build_external_id("m", "acc", "2024-01-01", -10.0, "EUR", "Shop B")
        assert a != b

    def test_prefix(self):
        ext_id = build_external_id("m", "acc", "2024-01-01", -5.0, "EUR", "x")
        assert ext_id.startswith("csv:")


# ── preview_csv ────────────────────────────────────────────────────────────────

class TestPreviewCsv:
    def test_returns_headers_and_rows(self):
        result = preview_csv(SAMPLE_CSV.encode())
        assert result["headers"] == [
            "Timestamp (UTC)", "Transaction Description", "Currency",
            "Amount", "To Currency", "To Amount",
        ]
        assert len(result["rows"]) == 4

    def test_max_5_rows(self):
        # 6 data rows — preview should cap at 5
        rows = "\n".join([f"2024-01-{i:02d},desc,EUR,-1.00,," for i in range(1, 8)])
        csv_bytes = f"Timestamp (UTC),Transaction Description,Currency,Amount,To Currency,To Amount\n{rows}\n".encode()
        result = preview_csv(csv_bytes)
        assert len(result["rows"]) == 5

    def test_semicolon_delimiter(self):
        data = "Col1;Col2\nA;B\n".encode()
        result = preview_csv(data, delimiter=";")
        assert result["headers"] == ["Col1", "Col2"]
        assert result["rows"][0]["Col2"] == "B"

    def test_skip_rows(self):
        # 2 header/comment rows before the actual header
        data = "# comment\nignored\nDate,Amount\n2024-01-01,-1.00\n".encode()
        result = preview_csv(data, skip_rows=2)
        assert result["headers"] == ["Date", "Amount"]

    def test_empty_csv_returns_empty(self):
        result = preview_csv(b"")
        assert result["headers"] == []
        assert result["rows"] == []


# ── parse_and_map ──────────────────────────────────────────────────────────────

class TestParseAndMap:
    def test_parses_all_rows(self):
        txs = parse_and_map(SAMPLE_CSV.encode(), CRYPTO_COM_MAPPING)
        assert len(txs) == 4

    def test_date_converted(self):
        txs = parse_and_map(SAMPLE_CSV.encode(), CRYPTO_COM_MAPPING)
        assert txs[0]["date"] == "2024-01-15"

    def test_amount_parsed(self):
        txs = parse_and_map(SAMPLE_CSV.encode(), CRYPTO_COM_MAPPING)
        assert txs[0]["amount_eur"] == -42.50
        assert txs[2]["amount_eur"] == 0.43

    def test_currency_code(self):
        txs = parse_and_map(SAMPLE_CSV.encode(), CRYPTO_COM_MAPPING)
        assert txs[0]["currency_code"] == "EUR"

    def test_description(self):
        txs = parse_and_map(SAMPLE_CSV.encode(), CRYPTO_COM_MAPPING)
        assert txs[0]["description"] == "Supermarkt Berlin"

    def test_external_id_generated(self):
        txs = parse_and_map(SAMPLE_CSV.encode(), CRYPTO_COM_MAPPING)
        assert txs[0]["external_id"].startswith("csv:crypto-com-visa:")

    def test_external_id_stable(self):
        txs_a = parse_and_map(SAMPLE_CSV.encode(), CRYPTO_COM_MAPPING)
        txs_b = parse_and_map(SAMPLE_CSV.encode(), CRYPTO_COM_MAPPING)
        assert txs_a[0]["external_id"] == txs_b[0]["external_id"]

    def test_foreign_amount_parsed(self):
        txs = parse_and_map(SAMPLE_CSV.encode(), CRYPTO_COM_MAPPING)
        # Row 4: BTC Purchase with To Amount = 0.00321000
        btc_tx = txs[3]
        assert btc_tx.get("foreign_amount") == pytest.approx(0.00321)
        assert btc_tx.get("foreign_currency_code") == "BTC"

    def test_foreign_amount_empty_skipped(self):
        # Row 0 has empty To Amount — should not set foreign_amount
        txs = parse_and_map(SAMPLE_CSV.encode(), CRYPTO_COM_MAPPING)
        assert "foreign_amount" not in txs[0]

    def test_blank_rows_skipped(self):
        csv_with_blank = SAMPLE_CSV + "\n,,,,, \n"
        txs = parse_and_map(csv_with_blank.encode(), CRYPTO_COM_MAPPING)
        assert len(txs) == 4

    def test_fixed_currency_value(self):
        mapping = {**CRYPTO_COM_MAPPING, "fields": {
            **CRYPTO_COM_MAPPING["fields"],
            "currency_code": {"value": "EUR"},
        }}
        txs = parse_and_map(SAMPLE_CSV.encode(), mapping)
        assert all(t["currency_code"] == "EUR" for t in txs)

    def test_optional_fields_absent_when_empty(self):
        txs = parse_and_map(SAMPLE_CSV.encode(), CRYPTO_COM_MAPPING)
        assert "category_name" not in txs[0]
        assert "budget_name" not in txs[0]
        assert "tags" not in txs[0]

    def test_optional_fields_included_when_mapped(self):
        mapping = {**CRYPTO_COM_MAPPING, "fields": {
            **CRYPTO_COM_MAPPING["fields"],
            "category_name": {"value": "Card"},
            "tags": {"value": "crypto,visa"},
        }}
        txs = parse_and_map(SAMPLE_CSV.encode(), mapping)
        assert txs[0]["category_name"] == "Card"
        assert txs[0]["tags"] == "crypto,visa"

    def test_fixture_file_parsed(self):
        with open(FIXTURE_CSV_PATH, "rb") as f:
            data = f.read()
        txs = parse_and_map(data, CRYPTO_COM_MAPPING)
        assert len(txs) == 5
        assert txs[0]["description"] == "Supermarkt Berlin"
        assert txs[0]["amount_eur"] == -42.50

    def test_semicolon_delimiter(self):
        data = "Date;Amount;Currency;Desc\n2024-01-01;-5,00;EUR;Shop\n".encode()
        mapping = {
            "id": "test", "name": "T", "delimiter": ";", "encoding": "utf-8",
            "skip_rows": 0, "account_name": "Test",
            "fields": {
                "date":         {"column": "Date", "date_format": "%Y-%m-%d"},
                "amount":       {"column": "Amount", "decimal_sep": ","},
                "currency_code":{"value": "EUR"},
                "description":  {"column": "Desc"},
            },
        }
        txs = parse_and_map(data, mapping)
        assert len(txs) == 1
        assert txs[0]["amount_eur"] == -5.0

    def test_external_id_override_column(self):
        data = "Date,Amount,Currency,Desc,TxId\n2024-01-01,-5.00,EUR,Shop,TX-001\n".encode()
        mapping = {
            "id": "test", "name": "T", "delimiter": ",", "encoding": "utf-8",
            "skip_rows": 0, "account_name": "Test",
            "fields": {
                "date":         {"column": "Date", "date_format": "%Y-%m-%d"},
                "amount":       {"column": "Amount"},
                "currency_code":{"value": "EUR"},
                "description":  {"column": "Desc"},
                "external_id":  {"column": "TxId"},
            },
        }
        txs = parse_and_map(data, mapping)
        assert txs[0]["external_id"] == "TX-001"


# ── load/save mappings ─────────────────────────────────────────────────────────

class TestMappingsPersistence:
    def test_save_and_load(self, tmp_path):
        path = str(tmp_path / "csv_mappings.json")
        with patch("src.csv_import.MAPPINGS_PATH", path):
            save_mappings([{"id": "test", "name": "Test"}])
            result = load_mappings()
        assert result == [{"id": "test", "name": "Test"}]

    def test_load_returns_empty_list_when_missing(self, tmp_path):
        path = str(tmp_path / "nonexistent.json")
        with patch("src.csv_import.MAPPINGS_PATH", path):
            assert load_mappings() == []

    def test_save_creates_parent_dirs(self, tmp_path):
        path = str(tmp_path / "subdir" / "csv_mappings.json")
        with patch("src.csv_import.MAPPINGS_PATH", path):
            save_mappings([])
        assert os.path.exists(path)


# ── Server endpoint tests ──────────────────────────────────────────────────────

@pytest.fixture()
def csv_client(tmp_path):
    """Flask test client with CSV mappings path pointed at a temp dir."""
    mappings_path = str(tmp_path / "csv_mappings.json")
    import src.server as server_mod
    import src.csv_import as csv_mod

    orig = csv_mod.MAPPINGS_PATH
    csv_mod.MAPPINGS_PATH = mappings_path

    yield server_mod.app.test_client()

    csv_mod.MAPPINGS_PATH = orig


class TestCsvFieldsEndpoint:
    def test_returns_list(self, csv_client):
        resp = csv_client.get("/csv/fields")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, list)
        assert any(f["id"] == "date" and f["required"] for f in data)
        assert any(f["id"] == "description" and f["required"] for f in data)

    def test_includes_optional_fields(self, csv_client):
        resp = csv_client.get("/csv/fields")
        ids = [f["id"] for f in resp.get_json()]
        assert "category_name" in ids
        assert "foreign_amount" in ids
        assert "tags" in ids


class TestCsvPreviewEndpoint:
    def test_returns_headers_and_rows(self, csv_client):
        resp = csv_client.post(
            "/csv/preview",
            data={"file": (io.BytesIO(SAMPLE_CSV.encode()), "test.csv"),
                  "delimiter": ",", "encoding": "utf-8", "skip_rows": "0"},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert "Timestamp (UTC)" in data["headers"]
        assert len(data["rows"]) == 4

    def test_no_file_returns_400(self, csv_client):
        resp = csv_client.post("/csv/preview")
        assert resp.status_code == 400


class TestCsvMappingsEndpoints:
    def test_empty_list(self, csv_client):
        resp = csv_client.get("/csv/mappings")
        assert resp.status_code == 200
        assert resp.get_json() == []

    def test_save_and_list(self, csv_client):
        mapping = {"id": "test", "name": "Test Mapping", "delimiter": ","}
        resp = csv_client.post(
            "/csv/mappings",
            data=json.dumps(mapping),
            content_type="application/json",
        )
        assert resp.status_code == 200
        listed = csv_client.get("/csv/mappings").get_json()
        assert len(listed) == 1
        assert listed[0]["id"] == "test"

    def test_save_missing_id_returns_400(self, csv_client):
        resp = csv_client.post(
            "/csv/mappings",
            data=json.dumps({"name": "No ID"}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_update_existing(self, csv_client):
        csv_client.post("/csv/mappings",
                        data=json.dumps({"id": "m1", "name": "Old"}),
                        content_type="application/json")
        csv_client.post("/csv/mappings",
                        data=json.dumps({"id": "m1", "name": "New"}),
                        content_type="application/json")
        listed = csv_client.get("/csv/mappings").get_json()
        assert len(listed) == 1
        assert listed[0]["name"] == "New"

    def test_delete(self, csv_client):
        csv_client.post("/csv/mappings",
                        data=json.dumps({"id": "m1", "name": "Test"}),
                        content_type="application/json")
        resp = csv_client.delete("/csv/mappings/m1")
        assert resp.status_code == 200
        assert csv_client.get("/csv/mappings").get_json() == []

    def test_delete_not_found(self, csv_client):
        resp = csv_client.delete("/csv/mappings/nonexistent")
        assert resp.status_code == 404


class TestCsvImportEndpoint:
    def test_imports_to_firefly(self, csv_client):
        mapping_json = json.dumps(CRYPTO_COM_MAPPING)
        with patch("src.server._forward_to_targets",
                   return_value={"found": 4, "imported": 4, "skipped": 0}) as mock_fwd:
            resp = csv_client.post(
                "/csv/import",
                data={"file": (io.BytesIO(SAMPLE_CSV.encode()), "test.csv"),
                      "mapping": mapping_json},
                content_type="multipart/form-data",
            )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "ok"
        assert data["found"] == 4
        mock_fwd.assert_called_once()
        txs, account = mock_fwd.call_args.args
        assert len(txs) == 4
        assert account["name"] == "Crypto.com Visa"

    def test_no_file_returns_400(self, csv_client):
        resp = csv_client.post(
            "/csv/import",
            data={"mapping": json.dumps(CRYPTO_COM_MAPPING)},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 400

    def test_no_mapping_returns_400(self, csv_client):
        resp = csv_client.post(
            "/csv/import",
            data={"file": (io.BytesIO(SAMPLE_CSV.encode()), "test.csv")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 400


# ── FireflyClient new fields ───────────────────────────────────────────────────

class TestFireflyNewFields:
    """Verify that the new CSV fields are passed through to the Firefly API payload."""

    def _post(self, tx):
        from src.firefly import FireflyClient
        client = FireflyClient({"url": "https://firefly.example.com", "token": "t"})
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.status_code = 200
        with patch("src.firefly.requests.post", return_value=mock_resp) as mock_post:
            client._create_transaction(tx, {"name": "Crypto.com Visa"})
        return mock_post.call_args.kwargs["json"]["transactions"][0]

    def _base_tx(self, **kw):
        return {
            "external_id": "csv:test:acc:2024-01-15:-42.50:EUR:abcd1234",
            "date": "2024-01-15",
            "amount_eur": -42.50,
            "currency_code": "EUR",
            "description": "Supermarkt Berlin",
            **kw,
        }

    def test_direct_description_used(self):
        split = self._post(self._base_tx())
        assert split["description"] == "Supermarkt Berlin"

    def test_category_name_passed(self):
        split = self._post(self._base_tx(category_name="Food"))
        assert split["category_name"] == "Food"

    def test_budget_name_passed(self):
        split = self._post(self._base_tx(budget_name="Monthly"))
        assert split["budget_name"] == "Monthly"

    def test_tags_string_split(self):
        split = self._post(self._base_tx(tags="crypto,visa,card"))
        assert split["tags"] == ["crypto", "visa", "card"]

    def test_tags_list_passed(self):
        split = self._post(self._base_tx(tags=["a", "b"]))
        assert split["tags"] == ["a", "b"]

    def test_foreign_amount_and_currency(self):
        split = self._post(self._base_tx(foreign_amount=0.00321, foreign_currency_code="BTC"))
        assert split["foreign_currency_code"] == "BTC"
        assert float(split["foreign_amount"]) == pytest.approx(0.00321)

    def test_no_category_when_absent(self):
        split = self._post(self._base_tx())
        assert "category_name" not in split

    def test_hbci_description_fallback(self):
        """HBCI transactions without 'description' key still use _build_description."""
        tx = {
            "external_id": "aqbanking:fints:123:20240115:-42.50:EUR",
            "date": "20240115",
            "amount_eur": -42.50,
            "currency_code": "EUR",
            "transaction_text": "DAUERAUFTRAG",
            "purpose": "Miete",
            "remote_name": "",
        }
        split = self._post(tx)
        assert split["description"] == "DAUERAUFTRAG – Miete"
