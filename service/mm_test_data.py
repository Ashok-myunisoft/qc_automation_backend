"""Fixed test-data lookup for MM screens (no AI involved).

Why: for MM screens the AI "business context" agent wandered through the
database, picked an empty table (RAWPURCHASE) and then invented values such as
INVOICE001 / ITEM A. Here each header field is mapped to its master table and
column directly. The six tables below were confirmed by successful queries in
your earlier run log (MPARTY, MDEPARTMENT, MSTORE, MPAYMENTTERM, MCURRENCY).

Left out on purpose: the Biz Transaction Type list. Its values must belong to
the screen's own ClassId and the screen normally preselects one, so a blind
list could be wrong.
"""
from __future__ import annotations

import logging

from service import db_service

logger = logging.getLogger(__name__)

# (form field the screen shows, master table, column)
_MM_HEADER_LOOKUPS = [
    ("PartyName", "MPARTY", "PARTYNAME"),
    ("DepartmentName", "MDEPARTMENT", "DEPARTMENTNAME"),
    ("StoreName", "MSTORE", "STORENAME"),
    ("PaymentTermName", "MPAYMENTTERM", "PAYMENTTERMNAME"),
    ("CurrencyCode", "MCURRENCY", "CURRENCYCODE"),
]
_SAMPLE_SIZE = 5
_FETCH_SIZE = 12  # fetch a few extra, some are filtered out below
_JUNK_VALUES = {"", "none", "n/a", "na", "null"}


def _clean(values) -> list[str]:
    out: list[str] = []
    for v in values or []:
        s = str(v).strip()
        if s.lower() in _JUNK_VALUES or s in out:
            continue
        out.append(s)
        if len(out) >= _SAMPLE_SIZE:
            break
    return out


def _sample(table: str, column: str) -> list[str]:
    return _clean(db_service.get_sample_values(table, column, _FETCH_SIZE))


def _find_item_table() -> str | None:
    """Looks for the item master: a master table (name starts with M, but not
    MM = transaction tables) that has BOTH ITEMCODE and ITEMNAME columns."""
    codes = {r["table"] for r in db_service.search_tables_by_column("ITEMCODE", 200)
             if str(r["column"]).upper() == "ITEMCODE"}
    names = {r["table"] for r in db_service.search_tables_by_column("ITEMNAME", 200)
             if str(r["column"]).upper() == "ITEMNAME"}
    candidates = [
        t for t in codes & names
        if t.upper().startswith("M") and not t.upper().startswith("MM")
    ]
    # prefer the most obvious names, then alphabetical
    candidates.sort(key=lambda t: (t.upper() not in ("MITEM", "MITEMMASTER"), "ITEM" not in t.upper(), t))
    for table in candidates:
        try:
            if _sample(table, "ITEMNAME"):
                return table
        except Exception as e:  # keep trying the next candidate
            logger.warning("MM test data: item table candidate %s failed: %s", table, e)
    return None


def find_mm_test_data(module: str, screen_name: str) -> dict:
    """Returns {"PartyName": [...], ...} like the AI agent did, or {"error": ...}."""
    result: dict[str, list[str]] = {}

    for field, table, column in _MM_HEADER_LOOKUPS:
        try:
            values = _sample(table, column)
        except Exception as e:
            logger.warning("MM test data: %s (%s.%s) lookup failed: %s", field, table, column, e)
            continue
        if values:
            result[field] = values
        else:
            logger.warning("MM test data: %s.%s returned no usable rows", table, column)

    try:
        item_table = _find_item_table()
        if item_table:
            result["ItemName"] = _sample(item_table, "ITEMNAME")
            try:
                codes = _sample(item_table, "ITEMCODE")
                if codes:
                    result["ItemCode"] = codes
            except Exception as e:
                logger.warning("MM test data: ITEMCODE lookup on %s failed: %s", item_table, e)
            logger.info("MM test data: item master = %s", item_table)
        else:
            logger.warning("MM test data: no item master table found (needs ITEMCODE + ITEMNAME, name starting with M)")
    except Exception as e:
        logger.warning("MM test data: item lookup failed: %s", e)

    if not result:
        return {"error": f"Unable to find valid test data for '{screen_name}' screen."}
    logger.info("MM test data for %s/%s: %s", module, screen_name, {k: len(v) for k, v in result.items()})
    return result