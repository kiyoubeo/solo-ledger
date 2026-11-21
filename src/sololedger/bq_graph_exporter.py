"""Phase 10 BigQuery Property Graph Entity Exporter, Referential Integrity Verifier & GQL Analytics."""

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import httpx

from sololedger.config import resolve_bundled_path, settings
from sololedger.merchant_service import ACCOUNT_CODE_NAMES
from sololedger.models import BigQueryGraphExportRequest
from sololedger.storage_backend import METADATA_TOKEN_URL, StorageBackend, get_storage_backend

OAUTH2_TOKEN_URL = "https://oauth2.googleapis.com/token"

_BQ_IDENT_RE = re.compile(r"^[A-Za-z0-9_\-]+$")
_SITE_BRACKET_RE = re.compile(r"\[([^\[\]]+)\]")
_SITE_SUFFIX_RE = re.compile(r"([가-힣A-Za-z0-9]+현장)")
_GQL_BLOCK_RE = re.compile(
    r"--\s*@gql_query:\s*([A-Za-z0-9_]+)\s*\r?\n([\s\S]*?)\r?\n--\s*@end_gql_query"
)
_GENERIC_KEYWORD_FILLERS = {
    "친환경",
    "국내산",
    "현장",
    "구입",
    "구매",
    "결제",
    "주문",
    "용품",
    "기타",
}


def validate_bq_identifier(value: str, field_name: str = "identifier") -> str:
    """Validate GCP project_id or BigQuery dataset_id against safe identifier rules."""
    cleaned = (value or "").strip()
    if not cleaned or len(cleaned) > 128 or not _BQ_IDENT_RE.fullmatch(cleaned):
        raise ValueError(f"Invalid BigQuery {field_name}: {value!r}")
    return cleaned


def _load_bq_sql_template() -> str:
    sql_path = resolve_bundled_path("sql", "bigquery_property_graph.sql")
    if not sql_path.exists():
        raise FileNotFoundError(f"BigQuery property graph SQL template not found: {sql_path}")
    return sql_path.read_text(encoding="utf-8")


def render_bigquery_graph_ddl(
    project_id: str,
    dataset_id: str = "sololedger_analytics",
) -> str:
    """Render BigQuery table and CREATE PROPERTY GRAPH DDL with validated identifiers."""
    clean_proj = validate_bq_identifier(project_id, "project_id")
    clean_ds = validate_bq_identifier(dataset_id, "dataset_id")
    raw_sql = _load_bq_sql_template()
    ddl_part = raw_sql.split("-- @gql_query:", 1)[0]
    rendered = ddl_part.replace("{{PROJECT_ID}}", clean_proj).replace("{{DATASET_ID}}", clean_ds)
    return rendered.strip() + "\n"


def load_named_gql_queries(
    project_id: str,
    dataset_id: str = "sololedger_analytics",
) -> Dict[str, str]:
    """Load native GoogleSQL GQL queries from sql/bigquery_property_graph.sql."""
    clean_proj = validate_bq_identifier(project_id, "project_id")
    clean_ds = validate_bq_identifier(dataset_id, "dataset_id")
    raw_sql = _load_bq_sql_template()

    queries: Dict[str, str] = {}
    for match in _GQL_BLOCK_RE.finditer(raw_sql):
        q_name, q_body = match.groups()
        rendered = (
            q_body.replace("{{PROJECT_ID}}", clean_proj)
            .replace("{{DATASET_ID}}", clean_ds)
            .strip()
        )
        queries[q_name] = rendered
    return queries


def _extract_normalized_keyword(item_text: str) -> str:
    """Extract a canonical item keyword for multi-hop item-to-account recommendations."""
    stripped = _SITE_BRACKET_RE.sub(" ", item_text or "")
    stripped = re.sub(r"\b\d+\s*(?:개|통|박스|ea|EA|세트|롤|장|kg|L)\b", " ", stripped)
    tokens = re.findall(r"[가-힣A-Za-z0-9]{2,}", stripped)
    meaningful = [
        tok
        for tok in tokens
        if not tok.isdigit() and tok not in _GENERIC_KEYWORD_FILLERS and not tok.endswith("현장")
    ]
    if meaningful:
        return meaningful[0][:40]
    non_num = [tok for tok in tokens if not tok.isdigit()]
    if non_num:
        return non_num[0][:40]
    return "일반품목"


def _extract_site_info(
    counterparty_info: Optional[str], account_code: str
) -> Optional[Tuple[str, str, Optional[str]]]:
    """Derive ConstructionSite (site_id, site_name, client_hint) from memo or cost code."""
    memo = (counterparty_info or "").strip()
    m_bracket = _SITE_BRACKET_RE.search(memo)
    site_name: Optional[str] = None
    if m_bracket:
        candidate = m_bracket.group(1).strip()
        if candidate:
            site_name = candidate
    if not site_name:
        m_suffix = _SITE_SUFFIX_RE.search(memo)
        if m_suffix:
            site_name = m_suffix.group(1).strip()

    if site_name:
        digest = hashlib.sha1(site_name.encode("utf-8")).hexdigest()[:10].upper()
        return f"SITE_{digest}", site_name, None

    if account_code in ("501", "511"):
        return "DEFAULT_CONSTRUCTION_SITE", "기본시공현장", None

    return None


def _account_cost_nature(account_code: str) -> str:
    if account_code in ("501", "511"):
        return "CONSTRUCTION_COST"
    if account_code == "212":
        return "ASSET"
    if account_code == "999":
        return "UNCLASSIFIED"
    return "OPERATING_EXPENSE"


def extract_property_graph_entities(
    cursor: sqlite3.Cursor,
    year: Optional[int] = None,
    year_month: Optional[str] = None,
) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
    """Extract BigQuery Property Graph node and edge tables from SQLite ledger data."""
    if year_month is not None:
        if len(year_month) != 6 or not year_month.isdigit():
            raise ValueError(f"Invalid year_month format (expected YYYYMM): {year_month}")
        month_num = int(year_month[4:6])
        if month_num < 1 or month_num > 12:
            raise ValueError(f"Invalid month in year_month (expected 01..12): {year_month}")

    query = "SELECT * FROM card_transactions WHERE reconcile_status != 'CANCELED'"
    params: List[Any] = []
    if year_month is not None:
        query += " AND transacted_at LIKE ?"
        params.append(f"{year_month[:4]}-{year_month[4:6]}-%")
    elif year is not None:
        query += " AND transacted_at LIKE ?"
        params.append(f"{int(year):04d}-%")
    query += " ORDER BY transacted_at ASC, tx_id ASC"

    cursor.execute(query, params)
    tx_rows = [dict(r) for r in cursor.fetchall()]

    cursor.execute("SELECT * FROM merchants")
    db_merchants: Dict[str, Dict[str, Any]] = {
        str(r["biz_reg_no"]): dict(r) for r in cursor.fetchall()
    }

    cursor.execute(
        "SELECT tx_id, line_items_json FROM receipt_ocr_metadata "
        "WHERE tx_id IS NOT NULL ORDER BY receipt_id DESC"
    )
    receipt_items_by_tx: Dict[int, List[Dict[str, Any]]] = {}
    for rec in cursor.fetchall():
        tx_key = int(rec["tx_id"])
        if tx_key in receipt_items_by_tx:
            continue
        raw_li = rec["line_items_json"]
        if not raw_li:
            continue
        try:
            parsed = json.loads(str(raw_li))
            if isinstance(parsed, list):
                receipt_items_by_tx[tx_key] = [
                    it for it in parsed if isinstance(it, dict) and str(it.get("item") or "").strip()
                ]
        except Exception:
            continue

    merchants_map: Dict[str, Dict[str, Any]] = {}
    card_tx_list: List[Dict[str, Any]] = []
    order_items_list: List[Dict[str, Any]] = []
    account_cats_map: Dict[str, Dict[str, Any]] = {
        code: {
            "account_code": code,
            "account_name": name,
            "cost_nature": _account_cost_nature(code),
            "is_construction_cost": code in ("501", "511"),
        }
        for code, name in ACCOUNT_CODE_NAMES.items()
    }
    sites_map: Dict[str, Dict[str, Any]] = {}

    edge_purchased_at: List[Dict[str, Any]] = []
    edge_contains_item: List[Dict[str, Any]] = []
    edge_classified_as: List[Dict[str, Any]] = []
    edge_attributed_to: List[Dict[str, Any]] = []

    for row in tx_rows:
        tx_id = int(row["tx_id"])
        raw_merchant = str(row.get("raw_merchant_name") or "미상가맹점")
        transacted_at = str(row.get("transacted_at") or "")
        digits = re.sub(r"\D", "", transacted_at)
        tax_year = int(digits[:4]) if len(digits) >= 4 else 0
        ym = digits[:6] if len(digits) >= 6 else ""

        acc_code = str(row.get("account_code") or "999")
        acc_name = str(row.get("account_name") or ACCOUNT_CODE_NAMES.get(acc_code, "미분류"))
        if acc_code not in account_cats_map:
            account_cats_map[acc_code] = {
                "account_code": acc_code,
                "account_name": acc_name,
                "cost_nature": _account_cost_nature(acc_code),
                "is_construction_cost": acc_code in ("501", "511"),
            }

        raw_biz = str(row.get("biz_reg_no") or "").strip()
        if raw_biz:
            merchant_key = raw_biz
            if raw_biz in db_merchants:
                m_rec = db_merchants[raw_biz]
                merchants_map[raw_biz] = {
                    "biz_reg_no": raw_biz,
                    "merchant_name": str(m_rec.get("merchant_name") or raw_merchant),
                    "tax_type_code": str(m_rec.get("tax_type_code") or "UNKNOWN"),
                    "biz_status": str(m_rec.get("biz_status") or "ACTIVE"),
                    "is_pg_provider": bool(m_rec.get("is_pg_provider")),
                    "default_account_code": m_rec.get("default_account_code"),
                }
            else:
                merchants_map.setdefault(
                    raw_biz,
                    {
                        "biz_reg_no": raw_biz,
                        "merchant_name": raw_merchant,
                        "tax_type_code": "UNKNOWN",
                        "biz_status": "ACTIVE",
                        "is_pg_provider": False,
                        "default_account_code": None,
                    },
                )
        else:
            m_hash = hashlib.sha1(raw_merchant.encode("utf-8")).hexdigest()[:10].upper()
            merchant_key = f"UNVERIFIED_{m_hash}"
            merchants_map.setdefault(
                merchant_key,
                {
                    "biz_reg_no": merchant_key,
                    "merchant_name": raw_merchant,
                    "tax_type_code": "UNKNOWN",
                    "biz_status": "ACTIVE",
                    "is_pg_provider": False,
                    "default_account_code": None,
                },
            )

        total_amount = int(row.get("total_amount") or 0)
        supply_value = int(row.get("supply_value") or 0)
        vat_amount = int(row.get("vat_amount") or 0)
        vat_ded_type = str(row.get("vat_deduction_type") or "PENDING")
        memo = row.get("counterparty_info")

        card_tx_list.append(
            {
                "tx_id": tx_id,
                "card_last4": str(row.get("card_last4") or "0000"),
                "approval_no": row.get("approval_no"),
                "transacted_at": transacted_at,
                "tax_year": tax_year,
                "year_month": ym,
                "raw_merchant_name": raw_merchant,
                "biz_reg_no": merchant_key,
                "total_amount": total_amount,
                "supply_value": supply_value,
                "vat_amount": vat_amount,
                "account_code": acc_code,
                "account_name": acc_name,
                "vat_deduction_type": vat_ded_type,
                "non_deduct_reason": row.get("non_deduct_reason"),
                "is_vehicle_expense": bool(row.get("is_vehicle_expense")),
                "counterparty_info": memo,
                "reconcile_status": str(row.get("reconcile_status") or "SMS_ONLY"),
            }
        )

        edge_purchased_at.append(
            {
                "edge_id": f"PA_{tx_id}_{merchant_key}",
                "tx_id": tx_id,
                "biz_reg_no": merchant_key,
                "total_amount": total_amount,
                "transacted_at": transacted_at,
            }
        )

        edge_classified_as.append(
            {
                "edge_id": f"CA_{tx_id}_{acc_code}",
                "tx_id": tx_id,
                "account_code": acc_code,
                "vat_deduction_type": vat_ded_type,
            }
        )

        raw_items = receipt_items_by_tx.get(tx_id) or []
        if not raw_items and memo and str(memo).strip():
            raw_items = [{"item": str(memo).strip(), "qty": 1, "price": total_amount}]

        for idx, item_obj in enumerate(raw_items, start=1):
            item_name = str(item_obj.get("item") or "").strip()
            if not item_name:
                continue
            qty = max(1, int(item_obj.get("qty") or 1))
            unit_price = int(item_obj.get("price") or 0)
            line_amount = unit_price * qty if unit_price else total_amount
            item_id = f"ITEM_{tx_id}_{idx}"
            order_items_list.append(
                {
                    "item_id": item_id,
                    "tx_id": tx_id,
                    "item_name": item_name,
                    "normalized_keyword": _extract_normalized_keyword(item_name),
                    "qty": qty,
                    "unit_price": unit_price,
                    "line_amount": line_amount,
                }
            )
            edge_contains_item.append(
                {
                    "edge_id": f"CI_{tx_id}_{item_id}",
                    "tx_id": tx_id,
                    "item_id": item_id,
                    "qty": qty,
                    "line_amount": line_amount,
                }
            )

        site_info = _extract_site_info(memo, acc_code)
        if site_info is not None:
            site_id, site_name, client_hint = site_info
            sites_map.setdefault(
                site_id,
                {
                    "site_id": site_id,
                    "site_name": site_name,
                    "client_hint": client_hint,
                },
            )
            edge_attributed_to.append(
                {
                    "edge_id": f"AT_{tx_id}_{site_id}",
                    "tx_id": tx_id,
                    "site_id": site_id,
                    "attributed_amount": total_amount,
                }
            )

    return {
        "nodes": {
            "merchants": list(merchants_map.values()),
            "card_transactions": card_tx_list,
            "order_line_items": order_items_list,
            "account_categories": list(account_cats_map.values()),
            "construction_sites": list(sites_map.values()),
        },
        "edges": {
            "edge_purchased_at": edge_purchased_at,
            "edge_contains_item": edge_contains_item,
            "edge_classified_as": edge_classified_as,
            "edge_attributed_to": edge_attributed_to,
        },
    }


def verify_graph_referential_integrity(
    bundle: Dict[str, Dict[str, List[Dict[str, Any]]]],
) -> Tuple[bool, List[str]]:
    """Verify node PK uniqueness and 100% foreign-key resolution for every edge table."""
    errors: List[str] = []
    nodes = bundle["nodes"]
    edges = bundle["edges"]

    pk_specs = [
        ("merchants", "biz_reg_no"),
        ("card_transactions", "tx_id"),
        ("order_line_items", "item_id"),
        ("account_categories", "account_code"),
        ("construction_sites", "site_id"),
    ]
    node_keys: Dict[str, set] = {}
    for table_name, pk_col in pk_specs:
        seen = set()
        for row in nodes.get(table_name, []):
            val = row.get(pk_col)
            if val is None or val == "":
                errors.append(f"Null primary key {pk_col} in node table {table_name}")
                continue
            if val in seen:
                errors.append(f"Duplicate primary key {pk_col}={val!r} in {table_name}")
            seen.add(val)
        node_keys[table_name] = seen

    fk_specs = [
        ("edge_purchased_at", "tx_id", "card_transactions", "biz_reg_no", "merchants"),
        ("edge_contains_item", "tx_id", "card_transactions", "item_id", "order_line_items"),
        ("edge_classified_as", "tx_id", "card_transactions", "account_code", "account_categories"),
        ("edge_attributed_to", "tx_id", "card_transactions", "site_id", "construction_sites"),
    ]
    for edge_tbl, src_col, src_node_tbl, dst_col, dst_node_tbl in fk_specs:
        seen_edge_ids = set()
        for row in edges.get(edge_tbl, []):
            eid = row.get("edge_id")
            if not eid or eid in seen_edge_ids:
                errors.append(f"Invalid or duplicate edge_id={eid!r} in {edge_tbl}")
            seen_edge_ids.add(eid)

            if row.get(src_col) not in node_keys[src_node_tbl]:
                errors.append(
                    f"Dangling source key {src_col}={row.get(src_col)!r} in {edge_tbl}"
                )
            if row.get(dst_col) not in node_keys[dst_node_tbl]:
                errors.append(
                    f"Dangling destination key {dst_col}={row.get(dst_col)!r} in {edge_tbl}"
                )

    return len(errors) == 0, errors


def analyze_graph_risks_and_recommendations(
    bundle: Dict[str, Dict[str, List[Dict[str, Any]]]],
) -> Dict[str, Any]:
    """Run local multi-hop graph traversals equivalent to the BigQuery GQL audit queries."""
    nodes = bundle["nodes"]
    edges = bundle["edges"]

    tx_by_id = {int(r["tx_id"]): r for r in nodes["card_transactions"]}
    merchant_by_no = {str(r["biz_reg_no"]): r for r in nodes["merchants"]}
    item_by_id = {str(r["item_id"]): r for r in nodes["order_line_items"]}
    acc_by_code = {str(r["account_code"]): r for r in nodes["account_categories"]}
    site_by_id = {str(r["site_id"]): r for r in nodes["construction_sites"]}

    # 1. Mixed 811/813 merchants & non-general DEDUCTIBLE merchants
    tx_ids_by_merchant: Dict[str, List[int]] = {}
    for edge in edges["edge_purchased_at"]:
        tx_ids_by_merchant.setdefault(str(edge["biz_reg_no"]), []).append(int(edge["tx_id"]))

    mixed_merchants: List[Dict[str, Any]] = []
    for biz_no, tx_ids in tx_ids_by_merchant.items():
        m_node = merchant_by_no.get(biz_no, {})
        txs = [tx_by_id[tid] for tid in tx_ids if tid in tx_by_id]
        acc_codes = {str(t["account_code"]) for t in txs}
        has_deductible = any(str(t["vat_deduction_type"]) == "DEDUCTIBLE" for t in txs)
        non_general = m_node.get("tax_type_code") in (
            "SIMPLIFIED",
            "EXEMPT",
            "UNREGISTERED",
        ) or m_node.get("biz_status") in ("SUSPENDED", "CLOSED")

        reasons: List[str] = []
        if "811" in acc_codes and "813" in acc_codes:
            reasons.append("MIXED_811_813")
        if non_general and has_deductible:
            reasons.append("NON_GENERAL_DEDUCTIBLE")
        if reasons:
            mixed_merchants.append(
                {
                    "biz_reg_no": biz_no,
                    "merchant_name": m_node.get("merchant_name"),
                    "flag_reasons": reasons,
                    "account_codes": sorted(acc_codes),
                    "tx_count": len(txs),
                    "total_spend": sum(int(t["total_amount"]) for t in txs),
                }
            )
    mixed_merchants.sort(key=lambda x: (-int(x["total_spend"]), str(x["biz_reg_no"])))

    # 2. Item keyword -> AccountCategory recommendations (2-hop: OrderLineItem <- Tx -> Account)
    classified_by_tx: Dict[int, str] = {
        int(e["tx_id"]): str(e["account_code"]) for e in edges["edge_classified_as"]
    }
    kw_stats: Dict[Tuple[str, str], Dict[str, Any]] = {}
    seen_kw_tx: Dict[Tuple[str, str], set] = {}
    for edge in edges["edge_contains_item"]:
        tid = int(edge["tx_id"])
        acc_code = classified_by_tx.get(tid)
        if not acc_code or acc_code == "999":
            continue
        item_node = item_by_id.get(str(edge["item_id"]))
        tx_node = tx_by_id.get(tid)
        acc_node = acc_by_code.get(acc_code)
        if not item_node or not tx_node or not acc_node:
            continue
        kw = str(item_node["normalized_keyword"])
        pair = (kw, acc_code)
        entry = kw_stats.setdefault(
            pair,
            {
                "normalized_keyword": kw,
                "account_code": acc_code,
                "account_name": acc_node["account_name"],
                "support_tx_count": 0,
                "total_spend": 0,
            },
        )
        tx_set = seen_kw_tx.setdefault(pair, set())
        if tid not in tx_set:
            tx_set.add(tid)
            entry["support_tx_count"] += 1
            entry["total_spend"] += int(tx_node["total_amount"])

    item_recommendations = sorted(
        kw_stats.values(),
        key=lambda r: (-int(r["support_tx_count"]), -int(r["total_spend"]), r["normalized_keyword"]),
    )

    # 3. Construction site cost rollup (2-hop: ConstructionSite <- Tx -> Account)
    site_rollup_map: Dict[str, Dict[str, Any]] = {}
    for edge in edges["edge_attributed_to"]:
        tid = int(edge["tx_id"])
        sid = str(edge["site_id"])
        acc_code = classified_by_tx.get(tid)
        tx_node = tx_by_id.get(tid)
        site_node = site_by_id.get(sid)
        if not tx_node or not site_node or not acc_code:
            continue
        s_entry = site_rollup_map.setdefault(
            sid,
            {
                "site_id": sid,
                "site_name": site_node["site_name"],
                "material_501_amount": 0,
                "subcontract_511_amount": 0,
                "other_amount": 0,
                "total_site_cost": 0,
                "tx_count": 0,
            },
        )
        amt = int(tx_node["total_amount"])
        s_entry["tx_count"] += 1
        s_entry["total_site_cost"] += amt
        if acc_code == "501":
            s_entry["material_501_amount"] += amt
        elif acc_code == "511":
            s_entry["subcontract_511_amount"] += amt
        else:
            s_entry["other_amount"] += amt

    site_rollup = sorted(
        site_rollup_map.values(),
        key=lambda r: (-int(r["total_site_cost"]), str(r["site_id"])),
    )

    return {
        "mixed_account_merchants": mixed_merchants,
        "item_account_recommendations": item_recommendations,
        "construction_site_rollup": site_rollup,
    }


def _find_adc_credentials_path() -> Optional[Path]:
    """Locate local Google Application Default Credentials JSON if present."""
    candidates: List[Path] = []
    explicit_gac = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if explicit_gac:
        candidates.append(Path(explicit_gac))
    appdata = os.getenv("APPDATA", "").strip()
    if appdata:
        candidates.append(Path(appdata) / "gcloud" / "application_default_credentials.json")
    candidates.append(
        Path.home() / "AppData" / "Roaming" / "gcloud" / "application_default_credentials.json"
    )
    candidates.append(
        Path.home() / ".config" / "gcloud" / "application_default_credentials.json"
    )
    for cand in candidates:
        if cand.exists() and cand.is_file():
            return cand
    return None


def _read_adc_json() -> Optional[Dict[str, Any]]:
    adc_path = _find_adc_credentials_path()
    if adc_path is None:
        return None
    try:
        parsed = json.loads(adc_path.read_text(encoding="utf-8"))
        return parsed if isinstance(parsed, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _detect_adc_project_id() -> str:
    data = _read_adc_json()
    if not data:
        return ""
    return str(data.get("quota_project_id") or data.get("project_id") or "").strip()


def _resolve_bq_token(
    access_token: Optional[str] = None,
    transport: Optional[httpx.BaseTransport] = None,
) -> str:
    explicit = (
        access_token
        or settings.bq_access_token
        or settings.gcs_access_token
        or ""
    ).strip()
    if explicit:
        return explicit

    adc_data = _read_adc_json()
    if (
        adc_data
        and adc_data.get("type") == "authorized_user"
        and adc_data.get("client_id")
        and adc_data.get("client_secret")
        and adc_data.get("refresh_token")
    ):
        with httpx.Client(transport=transport, timeout=10.0) as client:
            resp = client.post(
                OAUTH2_TOKEN_URL,
                data={
                    "client_id": str(adc_data["client_id"]),
                    "client_secret": str(adc_data["client_secret"]),
                    "refresh_token": str(adc_data["refresh_token"]),
                    "grant_type": "refresh_token",
                },
            )
            resp.raise_for_status()
            refreshed = str(resp.json().get("access_token") or "").strip()
        if refreshed:
            return refreshed

    if transport is None:
        gcloud_bin = shutil.which("gcloud")
        if gcloud_bin:
            proc = subprocess.run(
                [gcloud_bin, "auth", "print-access-token"],
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
            )
            cli_token = (proc.stdout or "").strip()
            if proc.returncode == 0 and cli_token:
                return cli_token

    try:
        with httpx.Client(transport=transport, timeout=5.0) as client:
            resp = client.get(METADATA_TOKEN_URL, headers={"Metadata-Flavor": "Google"})
            resp.raise_for_status()
            token = str(resp.json().get("access_token") or "").strip()
    except httpx.HTTPError as exc:
        raise RuntimeError(
            "Failed to obtain OAuth2 access token from ADC, gcloud CLI, or GCP Metadata Server."
        ) from exc
    if not token:
        raise RuntimeError("Failed to obtain OAuth2 access token for BigQuery REST API.")
    return token


def get_bigquery_connection_status() -> Dict[str, Any]:
    """Report BigQuery project configuration and local ADC / gcloud readiness."""
    configured_proj = settings.bq_project_id or _detect_adc_project_id()
    adc_path = _find_adc_credentials_path()
    gcloud_path = shutil.which("gcloud")
    explicit_token = bool((settings.bq_access_token or settings.gcs_access_token).strip())
    return {
        "project_id": configured_proj or "sololedger-analytics",
        "configured_project_id": configured_proj,
        "dataset_id": settings.bq_dataset_id,
        "adc_found": adc_path is not None,
        "adc_path": str(adc_path) if adc_path else None,
        "gcloud_cli_found": gcloud_path is not None,
        "explicit_token_set": explicit_token,
        "ready_for_cloud_load": bool(
            configured_proj and (adc_path is not None or gcloud_path is not None or explicit_token)
        ),
    }


def _split_ddl_statements(ddl_sql: str) -> List[str]:
    stmts: List[str] = []
    for chunk in ddl_sql.split(";"):
        non_comment = [
            ln for ln in chunk.splitlines() if ln.strip() and not ln.strip().startswith("--")
        ]
        if non_comment:
            stmts.append(chunk.strip() + ";")
    return stmts


def provision_bigquery_dataset_and_graph(
    project_id: Optional[str] = None,
    dataset_id: Optional[str] = None,
    location: str = "asia-northeast3",
    bq_transport: Optional[httpx.BaseTransport] = None,
    access_token: Optional[str] = None,
) -> Dict[str, Any]:
    """Ensure BigQuery dataset exists and execute node/edge/Property Graph DDL statements."""
    raw_project = project_id or settings.bq_project_id or _detect_adc_project_id()
    if not raw_project:
        raise ValueError(
            "GCP project_id is required (set SOLOLEDGER_BQ_PROJECT_ID or ADC quota_project_id)."
        )
    clean_proj = validate_bq_identifier(raw_project, "project_id")
    clean_ds = validate_bq_identifier(dataset_id or settings.bq_dataset_id, "dataset_id")
    clean_loc = (location or "asia-northeast3").strip()

    token = _resolve_bq_token(access_token=access_token, transport=bq_transport)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    ddl_sql = render_bigquery_graph_ddl(project_id=clean_proj, dataset_id=clean_ds)
    stmts = _split_ddl_statements(ddl_sql)

    ds_url = (
        f"https://bigquery.googleapis.com/bigquery/v2/projects/"
        f"{quote(clean_proj, safe='')}/datasets"
    )
    q_url = (
        f"https://bigquery.googleapis.com/bigquery/v2/projects/"
        f"{quote(clean_proj, safe='')}/queries"
    )

    with httpx.Client(transport=bq_transport, timeout=30.0) as client:
        ds_resp = client.post(
            ds_url,
            headers=headers,
            json={
                "datasetReference": {"projectId": clean_proj, "datasetId": clean_ds},
                "location": clean_loc,
                "access": [
                    {"role": "OWNER", "specialGroup": "projectOwners"},
                    {"role": "WRITER", "specialGroup": "projectWriters"},
                    {"role": "READER", "specialGroup": "projectReaders"},
                ],
            },
        )
        if ds_resp.status_code in (200, 201):
            ds_status = "created"
        elif ds_resp.status_code == 409:
            ds_status = "already_exists"
        else:
            ds_resp.raise_for_status()
            ds_status = "created"

        for stmt in stmts:
            q_resp = client.post(
                q_url,
                headers=headers,
                json={
                    "query": stmt,
                    "useLegacySql": False,
                    "location": clean_loc,
                    "timeoutMs": 25000,
                },
            )
            q_resp.raise_for_status()
            q_data = q_resp.json()
            job_id = str((q_data.get("jobReference") or {}).get("jobId") or "")
            loc_ref = str((q_data.get("jobReference") or {}).get("location") or clean_loc)
            while q_data.get("jobComplete") is False and job_id:
                poll_resp = client.get(
                    f"{q_url}/{quote(job_id, safe='')}",
                    headers=headers,
                    params={"location": loc_ref, "timeoutMs": 25000},
                )
                poll_resp.raise_for_status()
                q_data = poll_resp.json()

    return {
        "status": "provisioned",
        "project_id": clean_proj,
        "dataset_id": clean_ds,
        "location": clean_loc,
        "dataset_status": ds_status,
        "ddl_statements_executed": len(stmts),
    }


def _parse_bq_query_response(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    fields = payload.get("schema", {}).get("fields") or []
    raw_rows = payload.get("rows") or []
    parsed: List[Dict[str, Any]] = []
    for raw_row in raw_rows:
        cells = raw_row.get("f") or []
        item: Dict[str, Any] = {}
        for field_meta, cell in zip(fields, cells):
            col_name = str(field_meta.get("name") or "")
            col_type = str(field_meta.get("type") or "STRING").upper()
            val = cell.get("v")
            if val is not None and col_type in ("INT64", "INTEGER"):
                val = int(val)
            elif val is not None and col_type in ("FLOAT64", "FLOAT", "NUMERIC"):
                val = float(val)
            elif val is not None and col_type in ("BOOL", "BOOLEAN"):
                val = str(val).lower() == "true"
            item[col_name] = val
        parsed.append(item)
    return parsed


def _stream_and_query_bigquery(
    project_id: str,
    dataset_id: str,
    table_rows_map: Dict[str, List[Dict[str, Any]]],
    gql_queries: Dict[str, str],
    run_gql_audit: bool,
    transport: Optional[httpx.BaseTransport] = None,
    access_token: Optional[str] = None,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    token = _resolve_bq_token(access_token=access_token, transport=transport)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    loaded_counts: Dict[str, int] = {}
    with httpx.Client(transport=transport, timeout=30.0) as client:
        for table_name, rows in table_rows_map.items():
            if not rows:
                loaded_counts[table_name] = 0
                continue
            url = (
                f"https://bigquery.googleapis.com/bigquery/v2/projects/"
                f"{quote(project_id, safe='')}/datasets/{quote(dataset_id, safe='')}/"
                f"tables/{quote(table_name, safe='')}/insertAll"
            )
            payload = {
                "rows": [
                    {"insertId": f"{table_name}_{idx}", "json": row}
                    for idx, row in enumerate(rows, start=1)
                ]
            }
            resp = client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            body = resp.json()
            if body.get("insertErrors"):
                raise RuntimeError(
                    f"BigQuery insertAll reported errors for {table_name}: {body['insertErrors']}"
                )
            loaded_counts[table_name] = len(rows)

        gql_rows: List[Dict[str, Any]] = []
        if run_gql_audit and "mixed_welfare_entertainment_merchants" in gql_queries:
            q_url = (
                f"https://bigquery.googleapis.com/bigquery/v2/projects/"
                f"{quote(project_id, safe='')}/queries"
            )
            q_resp = client.post(
                q_url,
                headers=headers,
                json={
                    "query": gql_queries["mixed_welfare_entertainment_merchants"],
                    "useLegacySql": False,
                    "timeoutMs": 25000,
                },
            )
            q_resp.raise_for_status()
            gql_rows = _parse_bq_query_response(q_resp.json())

    return {"status": "loaded", "loaded_counts": loaded_counts}, gql_rows


def export_ledger_graph_to_bigquery(
    cursor: sqlite3.Cursor,
    request: Optional[BigQueryGraphExportRequest] = None,
    storage: Optional[StorageBackend] = None,
    bq_transport: Optional[httpx.BaseTransport] = None,
    access_token: Optional[str] = None,
) -> Dict[str, Any]:
    """Serialize ledger data into BigQuery Property Graph NDJSON files and optionally load/query."""
    req = request or BigQueryGraphExportRequest()
    raw_project = (
        req.project_id
        if req.project_id is not None
        else (settings.bq_project_id or _detect_adc_project_id() or "sololedger-analytics")
    )
    raw_dataset = (
        req.dataset_id
        if req.dataset_id is not None
        else settings.bq_dataset_id
    )
    project_id = validate_bq_identifier(raw_project, "project_id")
    dataset_id = validate_bq_identifier(raw_dataset, "dataset_id")

    bundle = extract_property_graph_entities(
        cursor,
        year=req.year,
        year_month=req.year_month,
    )
    integrity_ok, integrity_errors = verify_graph_referential_integrity(bundle)
    if not integrity_ok:
        raise ValueError(f"Graph referential integrity check failed: {integrity_errors}")

    ddl_sql = render_bigquery_graph_ddl(project_id=project_id, dataset_id=dataset_id)
    gql_queries = load_named_gql_queries(project_id=project_id, dataset_id=dataset_id)
    analytics = analyze_graph_risks_and_recommendations(bundle)

    store = storage or get_storage_backend()
    tag = req.year_month or (str(req.year) if req.year is not None else "all")
    prefix = f"bigquery_exports/{tag}"

    all_tables: Dict[str, List[Dict[str, Any]]] = {}
    all_tables.update(bundle["nodes"])
    all_tables.update(bundle["edges"])

    export_uris: Dict[str, str] = {}
    for tbl_name, rows in all_tables.items():
        ndjson_text = (
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n"
            if rows
            else ""
        )
        export_uris[tbl_name] = store.put_bytes(
            f"{prefix}/{tbl_name}.ndjson",
            ndjson_text.encode("utf-8"),
            content_type="application/x-ndjson",
        )

    ddl_uri = store.put_bytes(
        f"{prefix}/property_graph_ddl.sql",
        ddl_sql.encode("utf-8"),
        content_type="text/sql; charset=utf-8",
    )

    result: Dict[str, Any] = {
        "status": "exported",
        "project_id": project_id,
        "dataset_id": dataset_id,
        "filter": {"year": req.year, "year_month": req.year_month},
        "referential_integrity_ok": integrity_ok,
        "integrity_errors": integrity_errors,
        "node_counts": {k: len(v) for k, v in bundle["nodes"].items()},
        "edge_counts": {k: len(v) for k, v in bundle["edges"].items()},
        "export_uris": export_uris,
        "ddl_uri": ddl_uri,
        "gql_queries": gql_queries,
        "analytics": analytics,
    }

    if req.execute_bigquery_load:
        bq_load_info, gql_audit_rows = _stream_and_query_bigquery(
            project_id=project_id,
            dataset_id=dataset_id,
            table_rows_map=all_tables,
            gql_queries=gql_queries,
            run_gql_audit=req.run_gql_audit,
            transport=bq_transport,
            access_token=access_token,
        )
        result["bigquery_load"] = bq_load_info
        result["gql_audit_rows"] = gql_audit_rows

    return result
