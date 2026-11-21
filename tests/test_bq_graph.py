"""Unit and integration tests for Phase 10 BigQuery Property Graph Exporter & GQL Analytics."""

import json
from typing import Any, Dict, List

import httpx
import pytest

from sololedger.bq_graph_exporter import (
    analyze_graph_risks_and_recommendations,
    export_ledger_graph_to_bigquery,
    extract_property_graph_entities,
    load_named_gql_queries,
    provision_bigquery_dataset_and_graph,
    render_bigquery_graph_ddl,
    validate_bq_identifier,
    verify_graph_referential_integrity,
)
from sololedger.models import BigQueryGraphExportRequest
from sololedger.storage_backend import LocalStorageBackend

VALID_BIZ_NO_SITE = "1208147521"
VALID_BIZ_NO_DINING = "2118123451"


def _seed_sample_ledger(cursor) -> None:
    cursor.execute(
        """
        INSERT INTO merchants (biz_reg_no, merchant_name, tax_type_code, biz_status, is_pg_provider)
        VALUES (?, '던에드워드페인트', 'GENERAL', 'ACTIVE', 0),
               (?, '청담한정식', 'GENERAL', 'ACTIVE', 0)
        """,
        (VALID_BIZ_NO_SITE, VALID_BIZ_NO_DINING),
    )

    # 1. 501 Material cost for [강남현장]
    cursor.execute(
        """
        INSERT INTO card_transactions (
            card_last4, transacted_at, raw_merchant_name, biz_reg_no,
            total_amount, supply_value, vat_amount, account_code, account_name,
            vat_deduction_type, counterparty_info, reconcile_status
        ) VALUES (
            '3081', '2026-10-05 10:00:00', '던에드워드페인트', ?,
            330000, 300000, 30000, '501', '원재료비',
            'DEDUCTIBLE', '[강남현장] 친환경 수성페인트 2통', 'HOMETAX_VERIFIED'
        )
        """,
        (VALID_BIZ_NO_SITE,),
    )
    tx1 = int(cursor.lastrowid)
    cursor.execute(
        """
        INSERT INTO receipt_ocr_metadata (
            tx_id, source_channel, original_file_path, archived_file_name,
            file_hash_sha256, ocr_biz_reg_no, ocr_total_amount, line_items_json
        ) VALUES (?, 'WEB_UPLOAD', '/tmp/p1.jpg', '20261005_던에드워드_원재료비_330000.jpg',
                  'hash_bq_1', ?, 330000, ?)
        """,
        (
            tx1,
            VALID_BIZ_NO_SITE,
            json.dumps([{"item": "친환경 수성페인트 2통", "qty": 2, "price": 165000}], ensure_ascii=False),
        ),
    )

    # 2. 511 Subcontract cost for [판교현장]
    cursor.execute(
        """
        INSERT INTO card_transactions (
            card_last4, transacted_at, raw_merchant_name, biz_reg_no,
            total_amount, supply_value, vat_amount, account_code, account_name,
            vat_deduction_type, counterparty_info, reconcile_status
        ) VALUES (
            '3081', '2026-10-08 14:00:00', '개미중기', NULL,
            550000, 500000, 50000, '511', '외주용역비',
            'DEDUCTIBLE', '[판교현장] 스카이차 고소작업 임차', 'SMS_ONLY'
        )
        """
    )

    # 3 & 4. Mixed 811 (Welfare) and 813 (Entertainment) at the same restaurant merchant
    cursor.execute(
        """
        INSERT INTO card_transactions (
            card_last4, transacted_at, raw_merchant_name, biz_reg_no,
            total_amount, supply_value, vat_amount, account_code, account_name,
            vat_deduction_type, counterparty_info, reconcile_status
        ) VALUES (
            '3081', '2026-10-12 12:10:00', '청담한정식', ?,
            44000, 40000, 4000, '811', '복리후생비',
            'DEDUCTIBLE', '대표 단독 점심 식대', 'HOMETAX_VERIFIED'
        )
        """,
        (VALID_BIZ_NO_DINING,),
    )
    cursor.execute(
        """
        INSERT INTO card_transactions (
            card_last4, transacted_at, raw_merchant_name, biz_reg_no,
            total_amount, supply_value, vat_amount, account_code, account_name,
            vat_deduction_type, non_deduct_reason, counterparty_info, reconcile_status
        ) VALUES (
            '3081', '2026-10-15 19:30:00', '청담한정식', ?,
            220000, 200000, 20000, '813', '기업업무추진비',
            'NON_DEDUCTIBLE', 'ENTERTAINMENT', 'A사 김철수 대표 미팅 식사', 'HOMETAX_VERIFIED'
        )
        """,
        (VALID_BIZ_NO_DINING,),
    )

    # 5. Canceled transaction (must be excluded)
    cursor.execute(
        """
        INSERT INTO card_transactions (
            card_last4, transacted_at, raw_merchant_name, biz_reg_no,
            total_amount, supply_value, vat_amount, account_code, account_name,
            vat_deduction_type, reconcile_status
        ) VALUES (
            '3081', '2026-10-20 09:00:00', '던에드워드페인트', ?,
            -330000, -300000, -30000, '501', '원재료비',
            'DEDUCTIBLE', 'CANCELED'
        )
        """,
        (VALID_BIZ_NO_SITE,),
    )


def test_bigquery_property_graph_ddl_and_gql_syntax_compliance():
    assert validate_bq_identifier("sololedger-prod-01", "project_id") == "sololedger-prod-01"
    with pytest.raises(ValueError):
        validate_bq_identifier("bad; DROP TABLE x;--", "project_id")
    with pytest.raises(ValueError):
        validate_bq_identifier("", "dataset_id")

    ddl = render_bigquery_graph_ddl("my-gcp-proj", "my_tax_ds")
    assert "CREATE PROPERTY GRAPH IF NOT EXISTS `my-gcp-proj.my_tax_ds.sololedger_tax_graph`" in ddl
    assert "SOURCE KEY (tx_id) REFERENCES CardTransaction (tx_id)" in ddl
    assert "DESTINATION KEY (biz_reg_no) REFERENCES Merchant (biz_reg_no)" in ddl
    assert "DESTINATION KEY (item_id) REFERENCES OrderLineItem (item_id)" in ddl
    assert "DESTINATION KEY (account_code) REFERENCES AccountCategory (account_code)" in ddl
    assert "DESTINATION KEY (site_id) REFERENCES ConstructionSite (site_id)" in ddl
    assert "MEASURE(SUM(total_amount)) AS sum_total_spend" in ddl

    queries = load_named_gql_queries("my-gcp-proj", "my_tax_ds")
    assert set(queries.keys()) == {
        "mixed_welfare_entertainment_merchants",
        "item_keyword_account_recommendation",
        "construction_site_cost_rollup",
    }
    mixed_q = queries["mixed_welfare_entertainment_merchants"]
    assert mixed_q.startswith("GRAPH `my-gcp-proj.my_tax_ds.sololedger_tax_graph`")
    assert "FILTER EXISTS {" in mixed_q
    assert "WHERE " not in mixed_q


def test_property_graph_entity_serialization_and_referential_integrity(db_conn):
    cur = db_conn.cursor()
    _seed_sample_ledger(cur)
    db_conn.commit()

    bundle = extract_property_graph_entities(cur, year_month="202610")
    ok, errors = verify_graph_referential_integrity(bundle)
    assert ok is True, f"Integrity errors: {errors}"

    nodes = bundle["nodes"]
    edges = bundle["edges"]
    # Canceled tx must be excluded -> 4 active transactions
    assert len(nodes["card_transactions"]) == 4
    # Synthetic merchant created for biz_reg_no=None ('개미중기') -> 3 merchants total
    assert len(nodes["merchants"]) == 3
    assert any(m["biz_reg_no"].startswith("UNVERIFIED_") for m in nodes["merchants"])
    assert len(edges["edge_purchased_at"]) == 4
    assert len(edges["edge_classified_as"]) == 4

    site_names = {s["site_name"] for s in nodes["construction_sites"]}
    assert site_names == {"강남현장", "판교현장"}

    analytics = analyze_graph_risks_and_recommendations(bundle)
    mixed = analytics["mixed_account_merchants"]
    assert len(mixed) == 1
    assert mixed[0]["merchant_name"] == "청담한정식"
    assert "MIXED_811_813" in mixed[0]["flag_reasons"]
    assert mixed[0]["total_spend"] == 264_000

    recs = analytics["item_account_recommendations"]
    assert any(
        r["normalized_keyword"] == "수성페인트" and r["account_code"] == "501"
        for r in recs
    )

    rollup = {r["site_name"]: r for r in analytics["construction_site_rollup"]}
    assert rollup["강남현장"]["material_501_amount"] == 330_000
    assert rollup["판교현장"]["subcontract_511_amount"] == 550_000


def test_export_ndjson_and_mock_bigquery_rest_load_and_gql(db_conn, tmp_path):
    cur = db_conn.cursor()
    _seed_sample_ledger(cur)
    db_conn.commit()

    streamed_tables: Dict[str, List[Dict[str, Any]]] = {}
    executed_queries: List[str] = []

    def _mock_bq_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/insertAll"):
            table_name = path.split("/tables/")[1].split("/insertAll")[0]
            payload = json.loads(request.content.decode("utf-8"))
            streamed_tables[table_name] = [row["json"] for row in payload["rows"]]
            return httpx.Response(200, json={"kind": "bigquery#tableDataInsertAllResponse"})

        if request.method == "POST" and path.endswith("/queries"):
            q_payload = json.loads(request.content.decode("utf-8"))
            assert q_payload.get("timeoutMs") == 25000
            executed_queries.append(q_payload["query"])
            return httpx.Response(
                200,
                json={
                    "jobComplete": True,
                    "schema": {
                        "fields": [
                            {"name": "biz_reg_no", "type": "STRING"},
                            {"name": "merchant_name", "type": "STRING"},
                            {"name": "tx_count", "type": "INT64"},
                            {"name": "total_spend", "type": "INT64"},
                        ]
                    },
                    "rows": [
                        {
                            "f": [
                                {"v": VALID_BIZ_NO_DINING},
                                {"v": "청담한정식"},
                                {"v": "2"},
                                {"v": "264000"},
                            ]
                        }
                    ],
                },
            )
        return httpx.Response(404, json={"error": f"Unhandled BigQuery route: {path}"})

    storage = LocalStorageBackend(archive_dir=tmp_path / "bq_archive")
    res = export_ledger_graph_to_bigquery(
        cur,
        request=BigQueryGraphExportRequest(
            project_id="my-gcp-proj",
            dataset_id="sololedger_analytics",
            year=2026,
            execute_bigquery_load=True,
            run_gql_audit=True,
        ),
        storage=storage,
        bq_transport=httpx.MockTransport(_mock_bq_handler),
        access_token="mock-bq-token",
    )

    assert res["status"] == "exported"
    assert res["referential_integrity_ok"] is True
    assert storage.exists("bigquery_exports/2026/card_transactions.ndjson")
    assert storage.exists("bigquery_exports/2026/property_graph_ddl.sql")

    ndjson_text = storage.get_bytes(
        "bigquery_exports/2026/card_transactions.ndjson"
    ).decode("utf-8")
    ndjson_lines = ndjson_text.strip().splitlines()
    assert len(ndjson_lines) == 4
    assert res["bigquery_load"]["status"] == "loaded"
    assert streamed_tables["card_transactions"]
    assert len(streamed_tables["edge_attributed_to"]) == 2
    assert len(executed_queries) == 1
    assert executed_queries[0].startswith("GRAPH `my-gcp-proj.sololedger_analytics.sololedger_tax_graph`")
    assert res["gql_audit_rows"] == [
        {
            "biz_reg_no": VALID_BIZ_NO_DINING,
            "merchant_name": "청담한정식",
            "tx_count": 2,
            "total_spend": 264_000,
        }
    ]


def test_bigquery_export_api_endpoint(client, db_conn):
    headers = {"X-API-Key": "test-secret-key"}
    assert client.post("/api/v1/analytics/bigquery-export").status_code == 401

    assert (
        client.post(
            "/api/v1/analytics/bigquery-export",
            json={"project_id": "invalid;project"},
            headers=headers,
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/v1/analytics/bigquery-export",
            json={"year_month": "202699"},
            headers=headers,
        ).status_code
        == 400
    )

    cur = db_conn.cursor()
    _seed_sample_ledger(cur)
    db_conn.commit()

    resp = client.post(
        "/api/v1/analytics/bigquery-export",
        json={"project_id": "sololedger-demo", "dataset_id": "tax_kg", "year_month": "202610"},
        headers=headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "exported"
    assert body["referential_integrity_ok"] is True
    assert body["node_counts"]["card_transactions"] == 4
    assert body["edge_counts"]["edge_attributed_to"] == 2
    assert body["analytics"]["mixed_account_merchants"][0]["merchant_name"] == "청담한정식"


def test_provision_bigquery_dataset_uses_project_special_groups_access():
    dataset_payloads: List[Dict[str, Any]] = []
    query_payloads: List[Dict[str, Any]] = []
    poll_calls: List[str] = []

    def _mock_provision_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/datasets"):
            payload = json.loads(request.content.decode("utf-8"))
            dataset_payloads.append(payload)
            return httpx.Response(200, json={"kind": "bigquery#dataset"})

        if request.method == "POST" and path.endswith("/queries"):
            payload = json.loads(request.content.decode("utf-8"))
            query_payloads.append(payload)
            idx = len(query_payloads)
            if idx == 1:
                return httpx.Response(
                    200,
                    json={
                        "jobComplete": False,
                        "jobReference": {"jobId": "job-ddl-1", "location": "asia-northeast3"},
                    },
                )
            return httpx.Response(
                200,
                json={
                    "jobComplete": True,
                    "jobReference": {"jobId": f"job-ddl-{idx}", "location": "asia-northeast3"},
                },
            )

        if request.method == "GET" and path.endswith("/queries/job-ddl-1"):
            poll_calls.append(str(request.url))
            return httpx.Response(200, json={"jobComplete": True})

        return httpx.Response(404, json={"error": f"Unhandled route: {request.method} {path}"})

    res = provision_bigquery_dataset_and_graph(
        project_id="my-org-proj",
        dataset_id="sololedger_analytics",
        location="asia-northeast3",
        bq_transport=httpx.MockTransport(_mock_provision_handler),
        access_token="mock-bq-token",
    )

    assert res["status"] == "provisioned"
    assert res["dataset_status"] == "created"
    assert res["ddl_statements_executed"] == 10
    assert len(dataset_payloads) == 1
    assert dataset_payloads[0]["access"] == [
        {"role": "OWNER", "specialGroup": "projectOwners"},
        {"role": "WRITER", "specialGroup": "projectWriters"},
        {"role": "READER", "specialGroup": "projectReaders"},
    ]
    assert len(query_payloads) == 10
    assert all(q.get("timeoutMs") == 25000 for q in query_payloads)
    assert len(poll_calls) == 1
    assert "timeoutMs=25000" in poll_calls[0]
