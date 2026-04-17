"""
txporter - CSV import with column mapping
Parses CSV files and maps columns to the Firefly III transaction format.
"""

import csv
import hashlib
import io
import json
import logging
import os
from datetime import datetime

logger = logging.getLogger(__name__)

_CONFIG_PATH = os.environ.get("TXPORTER_CONFIG", "/home/txporter/config/banks.json")
MAPPINGS_PATH = os.environ.get(
    "TXPORTER_CSV_MAPPINGS",
    os.path.join(os.path.dirname(_CONFIG_PATH), "csv_mappings.json"),
)

# Ordered list of Firefly fields available for CSV mapping.
# transform: "date" | "amount" | None — controls which extra options the UI shows.
FIREFLY_FIELDS = [
    {"id": "date",                  "label": "Date",                  "required": True,  "transform": "date"},
    {"id": "amount",                "label": "Amount",                "required": True,  "transform": "amount"},
    {"id": "currency_code",         "label": "Currency Code",         "required": True,  "transform": None},
    {"id": "description",           "label": "Description",           "required": True,  "transform": None},
    {"id": "remote_name",           "label": "Counterparty Name",     "required": False, "transform": None},
    {"id": "foreign_amount",        "label": "Foreign Amount",        "required": False, "transform": "amount"},
    {"id": "foreign_currency_code", "label": "Foreign Currency",      "required": False, "transform": None},
    {"id": "category_name",         "label": "Category",              "required": False, "transform": None},
    {"id": "budget_name",           "label": "Budget",                "required": False, "transform": None},
    {"id": "tags",                  "label": "Tags",                  "required": False, "transform": None},
    {"id": "book_date",             "label": "Book Date (Valuta)",    "required": False, "transform": "date"},
    {"id": "sepa_ct_id",            "label": "SEPA End-to-End Ref",   "required": False, "transform": None},
    {"id": "internal_reference",    "label": "Internal Reference",    "required": False, "transform": None},
    {"id": "notes",                 "label": "Notes",                 "required": False, "transform": None},
]


def load_mappings() -> list:
    if not os.path.exists(MAPPINGS_PATH):
        return []
    with open(MAPPINGS_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_mappings(mappings: list) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(MAPPINGS_PATH)), exist_ok=True)
    with open(MAPPINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(mappings, f, indent=2, ensure_ascii=False)


def _open_csv(file_bytes: bytes, encoding: str, delimiter: str,
              skip_rows: int) -> tuple:
    """Decode bytes, skip rows, read header row, return (headers, StringIO).

    Uses csv.reader for skipping so quoted multiline fields are handled
    correctly. Strips UTF-8 BOM from the first header field. The returned
    StringIO is positioned immediately after the header row.
    Returns ([], empty StringIO) when the file has fewer rows than expected.
    """
    text = file_bytes.decode(encoding, errors="replace")
    f = io.StringIO(text)
    raw = csv.reader(f, delimiter=delimiter)
    for _ in range(skip_rows):
        try:
            next(raw)
        except StopIteration:
            return [], io.StringIO("")
    try:
        headers = next(raw)
    except StopIteration:
        return [], io.StringIO("")
    if headers and headers[0].startswith("\ufeff"):
        headers[0] = headers[0][1:]
    return headers, f


def _clean_row(row: dict) -> dict:
    """Drop the None restkey entry DictReader inserts for extra columns."""
    return {k: (v if v is not None else "") for k, v in row.items() if k is not None}


def _iter_rows_simple(f: io.StringIO, headers: list, delimiter: str):
    for row in csv.DictReader(f, fieldnames=headers, delimiter=delimiter):
        yield _clean_row(row)


def _flush_buf(buf: list, n: int, headers: list):
    while len(buf) < n:
        buf.append("")
    return dict(zip(headers, buf[:n]))


def _iter_rows_multiline(f: io.StringIO, headers: list, delimiter: str):
    n = len(headers)
    raw = csv.reader(f, delimiter=delimiter)
    buf = None
    for fields in raw:
        if buf is None:
            if len(fields) >= n:
                yield dict(zip(headers, fields[:n]))
            elif fields and any(v.strip() for v in fields):
                buf = list(fields)
        else:
            if fields:
                buf[-1] = buf[-1] + "\n" + fields[0]
                buf.extend(fields[1:])
            if len(buf) >= n:
                yield _flush_buf(buf, n, headers)
                buf = None
    if buf and any(v.strip() for v in buf):
        yield _flush_buf(buf, n, headers)


def _iter_rows(f: io.StringIO, headers: list, delimiter: str,
               join_multiline: bool = False):
    """Yield cleaned row dicts from f starting at its current position.

    join_multiline: when True, rows with fewer fields than the header are
    treated as continuations of the preceding row's last field (handles CSV
    exports where multi-line fields are not quoted).
    """
    if join_multiline:
        yield from _iter_rows_multiline(f, headers, delimiter)
    else:
        yield from _iter_rows_simple(f, headers, delimiter)


def preview_csv(file_bytes: bytes, delimiter: str = ",", encoding: str = "utf-8",
                skip_rows: int = 0, join_multiline: bool = False) -> dict:
    """Parse CSV bytes and return headers + first 5 data rows."""
    headers, f = _open_csv(file_bytes, encoding, delimiter, skip_rows)
    rows = []
    for i, row in enumerate(_iter_rows(f, headers, delimiter, join_multiline)):
        if i >= 5:
            break
        rows.append(row)
    return {"headers": headers, "rows": rows}


def _resolve(row: dict, field_cfg: dict) -> str:
    """Return the field value: fixed 'value' key takes priority over a 'column' lookup."""
    if "value" in field_cfg:
        return str(field_cfg["value"])
    col = field_cfg.get("column", "")
    return (row.get(col) or "") if col else ""


def _parse_date(value: str, fmt: str) -> str:
    """Parse a date/datetime string with strptime and return YYYY-MM-DD."""
    if not value or not fmt:
        return value
    try:
        return datetime.strptime(value.strip(), fmt).strftime("%Y-%m-%d")
    except ValueError:
        logger.warning("Could not parse date '%s' with format '%s'", value, fmt)
        return value


def _parse_amount(value: str, decimal_sep: str = ".", thousands_sep: str = "") -> float:
    """Parse an amount string to float, supporting European number formats."""
    if not value:
        return 0.0
    v = value.strip()
    if thousands_sep:
        v = v.replace(thousands_sep, "")
    if decimal_sep and decimal_sep != ".":
        v = v.replace(decimal_sep, ".")
    try:
        return float(v)
    except ValueError:
        logger.warning("Could not parse amount '%s'", value)
        return 0.0


def build_external_id(mapping_id: str, account_name: str, date: str,
                      amount: float, currency: str, description: str) -> str:
    """Build a stable external_id following the same convention as HBCI imports.

    Format: csv:{mapping_id}:{account_name}:{date}:{amount:.2f}:{currency}:{desc_hash}
    The description hash (first 8 hex chars of SHA-256) acts as the uniqueness
    component, analogous to bank_reference in the HBCI scheme.
    """
    desc_hash = hashlib.sha256(description.encode()).hexdigest()[:8]
    return ":".join(["csv", mapping_id, account_name, date, f"{amount:.2f}", currency, desc_hash])


def parse_and_map(file_bytes: bytes, mapping: dict) -> list:
    """Parse a CSV file using a mapping profile and return neutral transaction dicts.

    The returned dicts use the same keys as AqBanking transactions so that the
    existing FireflyClient.import_transactions() pipeline works unchanged.
    """
    delimiter = mapping.get("delimiter", ",")
    encoding = mapping.get("encoding", "utf-8")
    skip_rows = int(mapping.get("skip_rows", 0))
    join_multiline = bool(mapping.get("join_multiline", False))
    account_name = mapping.get("account_name", "")
    mapping_id = mapping.get("id", "csv")
    fields = mapping.get("fields", {})

    _headers, f = _open_csv(file_bytes, encoding, delimiter, skip_rows)

    transactions = []
    for row in _iter_rows(f, _headers, delimiter, join_multiline):
        if not any(v.strip() for v in row.values() if v):
            continue  # skip blank rows

        # ── Required fields ───────────────────────────────────────────────────
        date_cfg = fields.get("date", {})
        date = _parse_date(_resolve(row, date_cfg), date_cfg.get("date_format", ""))

        amount_cfg = fields.get("amount", {})
        amount = _parse_amount(
            _resolve(row, amount_cfg),
            decimal_sep=amount_cfg.get("decimal_sep", "."),
            thousands_sep=amount_cfg.get("thousands_sep", ""),
        )

        currency = _resolve(row, fields.get("currency_code", {})) or "EUR"
        description = _resolve(row, fields.get("description", {}))

        # ── external_id ───────────────────────────────────────────────────────
        ext_id_cfg = fields.get("external_id", {})
        ext_id = _resolve(row, ext_id_cfg) if ext_id_cfg else ""
        if not ext_id:
            ext_id = build_external_id(mapping_id, account_name, date, amount, currency, description)

        tx = {
            "external_id": ext_id,
            "date": date,
            "amount_eur": amount,
            "currency_code": currency,
            "description": description,
        }

        # ── Optional string fields ────────────────────────────────────────────
        for field_id in ("remote_name", "foreign_currency_code",
                         "category_name", "budget_name", "tags",
                         "sepa_ct_id", "internal_reference", "notes"):
            cfg = fields.get(field_id, {})
            if cfg:
                val = _resolve(row, cfg)
                if val:
                    tx[field_id] = val

        # ── Optional date fields ──────────────────────────────────────────────
        book_cfg = fields.get("book_date", {})
        if book_cfg:
            raw = _resolve(row, book_cfg)
            if raw:
                parsed = _parse_date(raw, book_cfg.get("date_format", ""))
                if parsed:
                    tx["book_date"] = parsed

        # ── Optional amount fields ────────────────────────────────────────────
        fa_cfg = fields.get("foreign_amount", {})
        if fa_cfg:
            raw = _resolve(row, fa_cfg)
            if raw:
                val = _parse_amount(
                    raw,
                    decimal_sep=fa_cfg.get("decimal_sep", "."),
                    thousands_sep=fa_cfg.get("thousands_sep", ""),
                )
                if val:
                    tx["foreign_amount"] = val

        transactions.append(tx)

    return transactions
