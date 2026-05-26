"""Tests for CSV import parsing, mapping and external_id generation."""

import io
import json
import os
import pytest
from unittest.mock import patch, MagicMock

from external_id import build_external_id
from src.csv_import import (
    preview_csv,
    parse_and_map,
    load_mappings,
    load_builtin_profiles,
    save_mappings,
    _parse_date,
    _parse_amount,
    _resolve,
    _iban_from_blz,
    _iban_from_header,
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
    def test_format_with_desc_hash(self):
        ext_id = build_external_id("crypto-com-visa:Crypto.com Visa", "20240115", -42.50, "EUR",
                                   description="Supermarkt Berlin")
        parts = ext_id.split(":")
        assert parts[0] == "txporter"
        assert parts[1] == "crypto-com-visa"
        assert parts[2] == "Crypto.com Visa"
        assert parts[3] == "20240115"
        assert parts[4] == "-42.50"
        assert parts[5] == "EUR"
        assert len(parts[6]) == 8

    def test_end_to_end_ref_used_when_real(self):
        ext_id = build_external_id("DE12345678901234567890", "20240115", -42.50, "EUR",
                                   end_to_end_ref="AB12345678")
        assert ext_id.endswith(":AB12345678")

    def test_notprovided_falls_to_fingerprint(self):
        a = build_external_id("DE12345678901234567890", "20240115", -42.50, "EUR",
                              end_to_end_ref="NOTPROVIDED",
                              remote_iban="DE99123456780000001234", remote_name="Shop GmbH",
                              description="Supermarkt")
        b = build_external_id("DE12345678901234567890", "20240115", -42.50, "EUR",
                              remote_iban="DE99123456780000001234", remote_name="Shop GmbH",
                              description="Supermarkt")
        assert a == b
        assert len(a.split(":")[-1]) == 8

    def test_no_ref_uses_fingerprint(self):
        ext_id = build_external_id("DE12345678901234567890", "20240115", -42.50, "EUR",
                                   remote_iban="DE99123456780000001234",
                                   remote_name="Shop GmbH", description="Supermarkt")
        assert len(ext_id.split(":")[-1]) == 8

    def test_fingerprint_differs_by_remote_iban(self):
        a = build_external_id("m:acc", "20240101", -10.0, "EUR",
                              remote_iban="DE11111111111111111111", description="Shop")
        b = build_external_id("m:acc", "20240101", -10.0, "EUR",
                              remote_iban="DE22222222222222222222", description="Shop")
        assert a != b

    def test_stable(self):
        a = build_external_id("m:acc", "20240101", -10.0, "EUR", description="Shop")
        b = build_external_id("m:acc", "20240101", -10.0, "EUR", description="Shop")
        assert a == b

    def test_different_descriptions_produce_different_ids(self):
        a = build_external_id("m:acc", "20240101", -10.0, "EUR", description="Shop A")
        b = build_external_id("m:acc", "20240101", -10.0, "EUR", description="Shop B")
        assert a != b

    def test_prefix(self):
        ext_id = build_external_id("m:acc", "20240101", -5.0, "EUR", description="x")
        assert ext_id.startswith("txporter:")


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

    def test_utf8_bom_stripped_from_header(self):
        data = "\ufeffDate,Amount\n2024-01-01,-1.00\n".encode("utf-8")
        result = preview_csv(data)
        assert result["headers"][0] == "Date"

    def test_extra_columns_do_not_raise(self):
        # Row with more columns than header — must not crash JSON serialisation
        data = "A,B\n1,2,3,4\n".encode()
        result = preview_csv(data)
        assert None not in result["rows"][0]
        assert result["rows"][0]["A"] == "1"

    def test_multiline_quoted_field(self):
        data = 'Date;Description\n"2024-01-01";"Line1\nLine2"\n'.encode()
        result = preview_csv(data, delimiter=";")
        assert result["rows"][0]["Description"] == "Line1\nLine2"


DKB_FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "dkb_sample.csv")

DKB_MAPPING = {
    "id": "dkb-girokonto",
    "name": "DKB Girokonto",
    "delimiter": ";",
    "encoding": "utf-8",
    "skip_rows": 4,
    "account_name": "DKB Girokonto",
    "fields": {
        "date":          {"column": "Buchungsdatum", "date_format": "%d.%m.%y"},
        "amount":        {"column": "Betrag (\u20ac)", "decimal_sep": ","},
        "currency_code": {"value": "EUR"},
        "description":   {"column": "Verwendungszweck"},
        "remote_name":   {"column": "Zahlungsempf\u00e4nger*in"},
    },
}


class TestDKBFormat:
    def _fixture_bytes(self):
        with open(DKB_FIXTURE_PATH, "rb") as f:
            return f.read()

    def test_preview_strips_bom_and_reads_headers(self):
        result = preview_csv(self._fixture_bytes(), delimiter=";", encoding="utf-8", skip_rows=4)
        assert result["headers"][0] == "Buchungsdatum"
        assert "Betrag (\u20ac)" in result["headers"]

    def test_preview_returns_data_rows(self):
        result = preview_csv(self._fixture_bytes(), delimiter=";", encoding="utf-8", skip_rows=4)
        assert len(result["rows"]) == 5

    def test_preview_no_none_keys(self):
        result = preview_csv(self._fixture_bytes(), delimiter=";", encoding="utf-8", skip_rows=4)
        for row in result["rows"]:
            assert None not in row

    def test_parse_and_map_date_two_digit_year(self):
        txs = parse_and_map(self._fixture_bytes(), DKB_MAPPING)
        assert txs[0]["date"] == "2026-04-17"

    def test_parse_and_map_amount_comma_decimal(self):
        txs = parse_and_map(self._fixture_bytes(), DKB_MAPPING)
        assert txs[0]["amount_eur"] == -41.70

    def test_parse_and_map_positive_amount(self):
        txs = parse_and_map(self._fixture_bytes(), DKB_MAPPING)
        incoming = [t for t in txs if t["amount_eur"] > 0]
        assert len(incoming) == 1
        assert incoming[0]["amount_eur"] == 600.00

    def test_parse_and_map_all_rows(self):
        txs = parse_and_map(self._fixture_bytes(), DKB_MAPPING)
        assert len(txs) == 5


# ── 1822direkt multiline format ────────────────────────────────────────────────

DIREKT_FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "1822direkt_sample.csv")

DIREKT_MAPPING = {
    "id": "1822direkt",
    "name": "1822direkt",
    "delimiter": ",",
    "encoding": "utf-8",
    "skip_rows": 0,
    "join_multiline": True,
    "account_name": "1822direkt",
    "fields": {
        "date":          {"column": "date", "date_format": "%Y%m%d"},
        "amount":        {"column": "amount_eur"},
        "currency_code": {"column": "currency_code"},
        "description":   {"column": "purpose"},
        "remote_name":   {"column": "remote_name"},
        "external_id":   {"column": "external_id"},
    },
}


_ML_HDR = "date,amount,currency,name,desc,iban,ext_id"


class TestMultilineJoin:
    def test_two_line_row_joined(self):
        # Line 1 has 5 fields (< 7), continuation line provides the rest
        data = (_ML_HDR + "\n2024-01-01,-8,EUR,SHOP,First line\nSecond line,,id1\n").encode()
        result = preview_csv(data, join_multiline=True)
        assert len(result["rows"]) == 1
        assert result["rows"][0]["desc"] == "First line\nSecond line"

    def test_nine_line_row_joined(self):
        parts = "\n".join([f"Part{i}" for i in range(2, 10)])
        data = (_ML_HDR + "\n2024-01-01,-8,EUR,SHOP,Part1\n" + parts + ",,id1\n").encode()
        result = preview_csv(data, join_multiline=True)
        assert len(result["rows"]) == 1
        assert result["rows"][0]["desc"].startswith("Part1\nPart2")
        assert "Part9" in result["rows"][0]["desc"]

    def test_mixed_single_and_multiline(self):
        data = (
            _ML_HDR + "\n"
            "2024-01-01,-8,EUR,SHOP1,Single line,DE01,id1\n"
            "2024-01-02,-5,EUR,SHOP2,Multi line\npart 2,,id2\n"
        ).encode()
        result = preview_csv(data, join_multiline=True)
        assert len(result["rows"]) == 2
        assert "\n" not in result["rows"][0]["desc"]
        assert "\n" in result["rows"][1]["desc"]

    def test_quoted_commas_still_work(self):
        data = (_ML_HDR + '\n2024-01-01,-8,EUR,SHOP,"field,with,commas",DE01,id1\n').encode()
        result = preview_csv(data, join_multiline=True)
        assert result["rows"][0]["desc"] == "field,with,commas"

    def test_join_multiline_false_does_not_merge(self):
        data = (_ML_HDR + "\n2024-01-01,-8,EUR,SHOP,line1\nline2,,id1\n").encode()
        result = preview_csv(data, join_multiline=False)
        assert len(result["rows"]) == 2


class Test1822Format:
    def _fixture_bytes(self):
        with open(DIREKT_FIXTURE_PATH, "rb") as f:
            return f.read()

    def test_preview_headers(self):
        result = preview_csv(self._fixture_bytes(), join_multiline=True)
        assert result["headers"] == [
            "date", "amount_eur", "currency_code", "remote_name", "purpose",
            "remote_iban", "external_id",
        ]

    def test_preview_yields_5_rows(self):
        result = preview_csv(self._fixture_bytes(), join_multiline=True)
        assert len(result["rows"]) == 5

    def test_preview_all_rows_have_7_fields(self):
        result = preview_csv(self._fixture_bytes(), join_multiline=True)
        for row in result["rows"]:
            assert len(row) == 7

    def test_parse_and_map_all_rows(self):
        txs = parse_and_map(self._fixture_bytes(), DIREKT_MAPPING)
        assert len(txs) == 10

    def test_parse_and_map_date(self):
        txs = parse_and_map(self._fixture_bytes(), DIREKT_MAPPING)
        assert txs[0]["date"] == "2026-02-26"

    def test_parse_and_map_multiline_purpose_preserved(self):
        txs = parse_and_map(self._fixture_bytes(), DIREKT_MAPPING)
        payone = next(t for t in txs if "PAYONE" in t["description"])
        assert "\n" in payone["description"]

    def test_parse_and_map_external_id_from_column(self):
        txs = parse_and_map(self._fixture_bytes(), DIREKT_MAPPING)
        assert txs[0]["external_id"].startswith("aqbanking:fints:")

    def test_parse_and_map_salary_positive(self):
        txs = parse_and_map(self._fixture_bytes(), DIREKT_MAPPING)
        salary = next(t for t in txs if t["amount_eur"] == 6409.0)
        assert salary["description"] == "BEZUEGE 03/2026 10000001/001A"


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
        assert txs[0]["external_id"].startswith("txporter:crypto-com-visa:Crypto.com Visa:")

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
        builtin = str(tmp_path / "bank_csv_profiles.json")
        with patch("src.csv_import.MAPPINGS_PATH", path), \
             patch("src.csv_import._BUILTIN_PROFILES_PATH", builtin):
            save_mappings([{"id": "test", "name": "Test"}])
            result = load_mappings()
        assert result == [{"id": "test", "name": "Test"}]

    def test_load_returns_empty_list_when_missing(self, tmp_path):
        path = str(tmp_path / "nonexistent.json")
        builtin = str(tmp_path / "no_builtins.json")
        with patch("src.csv_import.MAPPINGS_PATH", path), \
             patch("src.csv_import._BUILTIN_PROFILES_PATH", builtin):
            assert load_mappings() == []

    def test_save_creates_parent_dirs(self, tmp_path):
        path = str(tmp_path / "subdir" / "csv_mappings.json")
        with patch("src.csv_import.MAPPINGS_PATH", path):
            save_mappings([])
        assert os.path.exists(path)


# ── Server endpoint tests ──────────────────────────────────────────────────────

@pytest.fixture()
def csv_client(tmp_path):
    """Flask test client with CSV mappings and builtin profiles paths pointed at temp dirs."""
    mappings_path = str(tmp_path / "csv_mappings.json")
    builtin_path = str(tmp_path / "bank_csv_profiles.json")
    import src.server as server_mod
    import src.csv_import as csv_mod

    orig_mappings = csv_mod.MAPPINGS_PATH
    orig_builtin = csv_mod._BUILTIN_PROFILES_PATH
    csv_mod.MAPPINGS_PATH = mappings_path
    csv_mod._BUILTIN_PROFILES_PATH = builtin_path

    yield server_mod.app.test_client()

    csv_mod.MAPPINGS_PATH = orig_mappings
    csv_mod._BUILTIN_PROFILES_PATH = orig_builtin


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


# ── _iban_from_blz ─────────────────────────────────────────────────────────────

class TestIbanFromBlz:
    def test_known_account(self):
        iban = _iban_from_blz("50050201", "0123456789")
        assert iban == "DE38500502010123456789"

    def test_leading_zeros_stripped_then_padded(self):
        assert _iban_from_blz("50050222", "0000000001") == _iban_from_blz("50050222", "1")

    def test_invalid_blz_returns_none(self):
        assert _iban_from_blz("5005022", "0123456789") is None  # 7 digits

    def test_account_too_long_returns_none(self):
        assert _iban_from_blz("50050222", "12345678901") is None  # 11 digits


# ── _resolve with columns list ─────────────────────────────────────────────────

class TestResolveColumns:
    def test_columns_joined(self):
        row = {"Vwz.0": "Part one", "Vwz.1": "Part two", "Vwz.2": ""}
        cfg = {"columns": ["Vwz.0", "Vwz.1", "Vwz.2"], "join": " "}
        assert _resolve(row, cfg) == "Part one Part two"

    def test_empty_columns_skipped(self):
        row = {"A": "Hello", "B": "", "C": "World"}
        cfg = {"columns": ["A", "B", "C"]}
        assert _resolve(row, cfg) == "Hello World"

    def test_custom_join_separator(self):
        row = {"A": "X", "B": "Y"}
        cfg = {"columns": ["A", "B"], "join": ", "}
        assert _resolve(row, cfg) == "X, Y"


# ── builtin profiles ───────────────────────────────────────────────────────────

class TestBuiltinProfiles:
    def test_load_builtin_profiles_returns_list(self):
        profiles = load_builtin_profiles()
        assert isinstance(profiles, list)
        assert len(profiles) >= 1

    def test_1822direkt_profile_present(self):
        profiles = load_builtin_profiles()
        ids = [p["id"] for p in profiles]
        assert "1822direkt" in ids

    def test_1822direkt_profile_marked_builtin(self):
        profiles = load_builtin_profiles()
        p = next(p for p in profiles if p["id"] == "1822direkt")
        assert p.get("builtin") is True

    def test_load_mappings_empty_without_pins(self, tmp_path):
        user_path = str(tmp_path / "csv_mappings.json")
        with patch("src.csv_import.MAPPINGS_PATH", user_path):
            assert load_mappings() == []

    def test_pinned_builtin_resolved_in_load_mappings(self, tmp_path):
        user_path = str(tmp_path / "csv_mappings.json")
        with open(user_path, "w") as f:
            json.dump([{"id": "1822direkt", "pinned": True}], f)
        with patch("src.csv_import.MAPPINGS_PATH", user_path):
            mappings = load_mappings()
        assert len(mappings) == 1
        assert mappings[0]["id"] == "1822direkt"
        assert mappings[0].get("builtin") is True

    def test_save_mappings_stores_builtin_as_pin_stub(self, tmp_path):
        user_path = str(tmp_path / "csv_mappings.json")
        builtin = next(p for p in load_builtin_profiles() if p["id"] == "1822direkt")
        with patch("src.csv_import.MAPPINGS_PATH", user_path):
            save_mappings([builtin, {"id": "user", "name": "kept"}])
            with open(user_path) as f:
                saved = json.load(f)
        direkt = next(m for m in saved if m["id"] == "1822direkt")
        assert direkt == {"id": "1822direkt", "pinned": True}
        assert any(m["id"] == "user" for m in saved)

    def test_pin_builtin_via_server(self, tmp_path):
        import src.server as server_mod
        import src.csv_import as csv_mod
        orig = csv_mod.MAPPINGS_PATH
        csv_mod.MAPPINGS_PATH = str(tmp_path / "csv_mappings.json")
        try:
            resp = server_mod.app.test_client().post(
                "/csv/mappings/pin",
                data=json.dumps({"id": "1822direkt"}),
                content_type="application/json",
            )
        finally:
            csv_mod.MAPPINGS_PATH = orig
        assert resp.status_code == 200
        assert resp.get_json()["id"] == "1822direkt"

    def test_pinned_builtin_removable_via_delete(self, tmp_path):
        import src.server as server_mod
        import src.csv_import as csv_mod
        orig = csv_mod.MAPPINGS_PATH
        path = str(tmp_path / "csv_mappings.json")
        with open(path, "w") as f:
            json.dump([{"id": "1822direkt", "pinned": True}], f)
        csv_mod.MAPPINGS_PATH = path
        try:
            resp = server_mod.app.test_client().delete("/csv/mappings/1822direkt")
        finally:
            csv_mod.MAPPINGS_PATH = orig
        assert resp.status_code == 200

    def test_builtin_save_returns_409(self, tmp_path):
        import src.server as server_mod
        import src.csv_import as csv_mod
        orig = csv_mod.MAPPINGS_PATH
        csv_mod.MAPPINGS_PATH = str(tmp_path / "csv_mappings.json")
        try:
            resp = server_mod.app.test_client().post(
                "/csv/mappings",
                data=json.dumps({"id": "1822direkt", "name": "Override attempt"}),
                content_type="application/json",
            )
        finally:
            csv_mod.MAPPINGS_PATH = orig
        assert resp.status_code == 409


# ── 1822direkt bank export format ──────────────────────────────────────────────

_BANK_FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "1822direkt_bank_sample.csv")

_1822DIREKT_BANK_PROFILE = next(
    p for p in load_builtin_profiles() if p["id"] == "1822direkt"
)


class Test1822BankExportFormat:
    def _fixture_bytes(self):
        with open(_BANK_FIXTURE_PATH, "rb") as f:
            return f.read()

    def test_preview_returns_correct_headers(self):
        profile = _1822DIREKT_BANK_PROFILE
        result = preview_csv(
            self._fixture_bytes(),
            delimiter=profile["delimiter"],
            encoding=profile["encoding"],
        )
        assert "Buchungstag" in result["headers"]
        assert "Soll/Haben" in result["headers"]
        assert "Vwz.0" in result["headers"]
        assert "End-to-End-Identifikation" in result["headers"]

    def test_parse_all_rows(self):
        txs = parse_and_map(self._fixture_bytes(), _1822DIREKT_BANK_PROFILE)
        assert len(txs) == 5

    def test_date_parsed(self):
        txs = parse_and_map(self._fixture_bytes(), _1822DIREKT_BANK_PROFILE)
        assert txs[0]["date"] == "2026-02-27"

    def test_amount_german_decimal(self):
        txs = parse_and_map(self._fixture_bytes(), _1822DIREKT_BANK_PROFILE)
        salary = next(t for t in txs if t["amount_eur"] > 0)
        assert salary["amount_eur"] == pytest.approx(6409.0)

    def test_debit_amount_negative(self):
        txs = parse_and_map(self._fixture_bytes(), _1822DIREKT_BANK_PROFILE)
        debits = [t for t in txs if t["amount_eur"] < 0]
        assert len(debits) == 4

    def test_currency_always_eur(self):
        txs = parse_and_map(self._fixture_bytes(), _1822DIREKT_BANK_PROFILE)
        assert all(t["currency_code"] == "EUR" for t in txs)

    def test_purpose_from_vwz_columns(self):
        txs = parse_and_map(self._fixture_bytes(), _1822DIREKT_BANK_PROFILE)
        salary = next(t for t in txs if t["amount_eur"] > 0)
        assert "BEZUEGE" in salary["description"]
        assert "10000001/001A" in salary["description"]

    def test_remote_name_mapped(self):
        txs = parse_and_map(self._fixture_bytes(), _1822DIREKT_BANK_PROFILE)
        salary = next(t for t in txs if t["amount_eur"] > 0)
        assert salary.get("remote_name") == "LBV MUSTERSTADT 10000001/001A"

    def test_remote_iban_mapped(self):
        txs = parse_and_map(self._fixture_bytes(), _1822DIREKT_BANK_PROFILE)
        salary = next(t for t in txs if t["amount_eur"] > 0)
        assert salary.get("remote_iban") == "DE02600000000101010101"

    def test_external_id_iban_format(self):
        txs = parse_and_map(self._fixture_bytes(), _1822DIREKT_BANK_PROFILE)
        salary = next(t for t in txs if t["amount_eur"] > 0)
        assert salary["external_id"] == "txporter:DE38500502010123456789:20260227:6409.00:EUR:41d8bc4f"

    def test_external_id_debit_format(self):
        txs = parse_and_map(self._fixture_bytes(), _1822DIREKT_BANK_PROFILE)
        card = next(t for t in txs if t["amount_eur"] == pytest.approx(-8.0))
        assert card["external_id"] == "txporter:DE38500502010123456789:20260226:-8.00:EUR:4e72d170"

    def test_external_id_matches_aqbanking_format(self):
        txs = parse_and_map(self._fixture_bytes(), _1822DIREKT_BANK_PROFILE)
        for tx in txs:
            parts = tx["external_id"].split(":")
            assert parts[0] == "txporter"
            assert parts[1].startswith("DE")  # IBAN
            assert len(parts[2]) == 8 and parts[2].isdigit()  # YYYYMMDD


# ── _iban_from_header ──────────────────────────────────────────────────────────

_DKB_FIXTURE_BYTES = open(
    os.path.join(os.path.dirname(__file__), "fixtures", "dkb_sample.csv"), "rb"
).read()

_DKB_BUILTIN_PROFILE = next(
    p for p in load_builtin_profiles() if p["id"] == "dkb-girokonto"
)


class TestIbanFromHeader:
    def test_extracts_iban_from_dkb_header(self):
        iban = _iban_from_header(_DKB_FIXTURE_BYTES, "utf-8", ";", 0, 1)
        assert iban == "DE89370400440532013000"

    def test_missing_row_returns_none(self):
        data = b"A;B\nC;D\n"
        assert _iban_from_header(data, "utf-8", ";", 5, 1) is None

    def test_missing_col_returns_none(self):
        data = b"OnlyOneCol\n"
        assert _iban_from_header(data, "utf-8", ";", 0, 1) is None


# ── DKB Girokonto built-in profile ────────────────────────────────────────────

class TestDKBBuiltinProfile:
    def test_profile_present_and_builtin(self):
        assert _DKB_BUILTIN_PROFILE.get("builtin") is True

    def test_parse_all_rows(self):
        txs = parse_and_map(_DKB_FIXTURE_BYTES, _DKB_BUILTIN_PROFILE)
        assert len(txs) == 5

    def test_date_parsed(self):
        txs = parse_and_map(_DKB_FIXTURE_BYTES, _DKB_BUILTIN_PROFILE)
        assert txs[0]["date"] == "2026-04-17"

    def test_amount_parsed(self):
        txs = parse_and_map(_DKB_FIXTURE_BYTES, _DKB_BUILTIN_PROFILE)
        assert txs[0]["amount_eur"] == pytest.approx(-41.70)

    def test_currency_eur(self):
        txs = parse_and_map(_DKB_FIXTURE_BYTES, _DKB_BUILTIN_PROFILE)
        assert all(t["currency_code"] == "EUR" for t in txs)

    def test_description_mapped(self):
        txs = parse_and_map(_DKB_FIXTURE_BYTES, _DKB_BUILTIN_PROFILE)
        assert "VISA" in txs[0]["description"]

    def test_remote_name_mapped(self):
        txs = parse_and_map(_DKB_FIXTURE_BYTES, _DKB_BUILTIN_PROFILE)
        assert txs[0].get("remote_name") == "Rewe Hamburg Mitte - Curve"

    def test_remote_iban_mapped(self):
        txs = parse_and_map(_DKB_FIXTURE_BYTES, _DKB_BUILTIN_PROFILE)
        assert txs[0].get("remote_iban") == "DE75512108001245126199"

    def test_external_id_uses_header_iban(self):
        txs = parse_and_map(_DKB_FIXTURE_BYTES, _DKB_BUILTIN_PROFILE)
        # Kundenreferenz "466106571503124" is real (not NOTPROVIDED) → used directly
        assert txs[0]["external_id"] == "txporter:DE89370400440532013000:20260417:-41.70:EUR:466106571503124"

    def test_external_id_matches_aqbanking_format(self):
        txs = parse_and_map(_DKB_FIXTURE_BYTES, _DKB_BUILTIN_PROFILE)
        for tx in txs:
            parts = tx["external_id"].split(":")
            assert parts[0] == "txporter"
            assert parts[1].startswith("DE")
            assert len(parts[2]) == 8 and parts[2].isdigit()
