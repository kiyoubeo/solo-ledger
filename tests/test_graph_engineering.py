"""Unit and integration tests for Graph Engineering, Runtime Auto-Wiring, and BigQuery ADC."""

import base64
import json
import sys

import httpx
from fastapi.testclient import TestClient

from sololedger.api import app
from sololedger.bq_graph_exporter import (
    _resolve_bq_token,
    get_bigquery_connection_status,
    provision_bigquery_dataset_and_graph,
)
from sololedger.db import get_connection
from sololedger.graph_engineering import verify_engineering_graph
from sololedger.workflow_graph import load_active_checkpoint


def test_verify_engineering_graph_passes_on_repo_and_frozen_mode(tmp_path, monkeypatch):
    report = verify_engineering_graph()
    assert report["passed"] is True, f"Graph errors: {report['errors']}"
    sm = report["summary"]
    assert sm["cycle_count"] == 0
    assert sm["untested_module_count"] == 0
    assert sm["autowire_verified_count"] == 5
    assert sm["sqlite_table_count"] >= 5
    assert sm["state_graph_node_count"] == 6
    assert sm["bq_table_count"] == 9
    assert sm["gql_query_count"] >= 3
    assert "flowchart LR" in report["mermaid"]

    # Frozen PyInstaller binary simulation (no src/sololedger .py files on disk)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    empty_src = tmp_path / "frozen_no_src"
    empty_src.mkdir()
    frozen_report = verify_engineering_graph(src_dir=empty_src)
    assert frozen_report["summary"]["mode"] == "frozen"
    assert frozen_report["summary"]["cycle_count"] == 0
    assert frozen_report["summary"]["state_graph_node_count"] == 6


def test_verify_engineering_graph_detects_circular_imports(tmp_path):
    synth_src = tmp_path / "src_synth"
    synth_src.mkdir()
    (synth_src / "alpha.py").write_text("import sololedger.beta\n", encoding="utf-8")
    (synth_src / "beta.py").write_text("import sololedger.alpha\n", encoding="utf-8")

    report = verify_engineering_graph(src_dir=synth_src)
    assert report["passed"] is False
    assert report["summary"]["cycle_count"] >= 1
    assert any("Circular module dependency detected" in err for err in report["errors"])


def test_runtime_autowire_sms_receipt_telegram_and_tax_pack_audit(client, test_db_path):
    headers = {"X-API-Key": "test-secret-key"}

    # 1. Coupang SMS auto-runs workflow_graph and persists INTERRUPTED checkpoint
    r_pg = client.post(
        "/api/v1/webhooks/sms",
        headers=headers,
        json={
            "raw_message": "[Web발신] 신한법인(9876) 승인 49,000원 일시불 10/20 14:00 쿠팡(주) 승인번호:77112233",
            "received_at": "2026-10-20T14:00:05",
        },
    )
    assert r_pg.status_code == 200
    pg_body = r_pg.json()
    pg_tx_id = pg_body["tx_id"]
    assert pg_body["workflow_status"] == "INTERRUPTED"
    assert pg_body["checkpoint_id"] is not None
    assert "human_interrupt" in pg_body["workflow_nodes"]

    # 2. strict_audit=True on /api/v1/exports/tax-pack blocks export with 409 when ERROR exists
    r_blocked = client.post(
        "/api/v1/exports/tax-pack",
        headers=headers,
        json={"year_month": "202610", "strict_audit": True},
    )
    assert r_blocked.status_code == 409
    blk_detail = r_blocked.json()["detail"]
    assert blk_detail["error"] == "tax_audit_blocked"
    assert pg_tx_id in blk_detail["pre_audit"]["flagged_tx_ids"]

    # 3. Receipt upload resolves PG transaction and auto-completes workflow checkpoint
    r_up = client.post(
        "/api/v1/receipts/upload",
        headers=headers,
        json={
            "file_name": "coupang_49000.jpg",
            "file_content_b64": base64.b64encode(b"autowire-receipt-49000").decode("ascii"),
            "tx_id": pg_tx_id,
            "ocr_override": {
                "biz_reg_no": "120-88-00767",
                "merchant_name": "쿠팡(주)",
                "approval_no": "77112233",
                "transacted_at": "2026-10-20 14:00:00",
                "total_amount": 49000,
                "supply_value": 44545,
                "vat_amount": 4455,
                "line_items": [{"item": "무선 마우스 패드", "qty": 1, "price": 49000}],
                "confidence": 0.98,
            },
        },
    )
    assert r_up.status_code == 200
    assert r_up.json()["workflow_checkpoint"]["status"] == "COMPLETED"

    conn = get_connection(test_db_path)
    try:
        assert load_active_checkpoint(conn.cursor(), pg_tx_id) is None
    finally:
        conn.close()

    # 4. Dining SMS -> Telegram meal_meeting creates INTERRUPTED checkpoint -> reply completes it
    r_cafe = client.post(
        "/api/v1/webhooks/sms",
        headers=headers,
        json={
            "raw_message": "[Web발신] 신한법인(9876) 승인 33,000원 일시불 10/21 12:30 스타벅스역삼점 승인번호:88112233",
            "received_at": "2026-10-21T12:30:05",
        },
    )
    assert r_cafe.status_code == 200
    cafe_tx_id = r_cafe.json()["tx_id"]
    assert r_cafe.json()["workflow_status"] == "COMPLETED"

    r_cb = client.post(
        "/api/v1/webhooks/telegram",
        headers=headers,
        json={"callback_query": {"id": "cb-auto-1", "data": f"meal_meeting:{cafe_tx_id}"}},
    )
    assert r_cb.status_code == 200
    assert r_cb.json()["workflow_checkpoint"]["status"] == "INTERRUPTED"
    assert (
        r_cb.json()["workflow_checkpoint"]["interrupt_reason"]
        == "MISSING_ENTERTAINMENT_ATTENDEE"
    )

    r_reply = client.post(
        "/api/v1/webhooks/telegram",
        headers=headers,
        json={
            "message": {
                "reply_to_message": {"text": f"전표번호: #{cafe_tx_id}"},
                "text": "C사 이영희 팀장 미팅",
            }
        },
    )
    assert r_reply.status_code == 200
    assert r_reply.json()["workflow_checkpoint"]["status"] == "COMPLETED"

    # 5. Export tax-pack now passes strict_audit=True and returns pre_audit report
    r_exp = client.post(
        "/api/v1/exports/tax-pack",
        headers=headers,
        json={"year_month": "202610", "strict_audit": True},
    )
    assert r_exp.status_code == 200
    exp_body = r_exp.json()
    assert exp_body["exported_count"] == 2
    assert exp_body["pre_audit"]["ready_for_export"] is True
    assert exp_body["pre_audit"]["error_count"] == 0


def test_bigquery_adc_token_refresh_and_dataset_provisioning(tmp_path, monkeypatch):
    adc_path = tmp_path / "adc.json"
    adc_path.write_text(
        json.dumps(
            {
                "type": "authorized_user",
                "client_id": "test-client-id.apps.googleusercontent.com",
                "client_secret": "test-client-secret",
                "refresh_token": "test-refresh-token",
                "quota_project_id": "sololedger-corp-gcp",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(adc_path))
    monkeypatch.delenv("SOLOLEDGER_BQ_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("SOLOLEDGER_GCS_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("SOLOLEDGER_BQ_PROJECT_ID", raising=False)

    status_info = get_bigquery_connection_status()
    assert status_info["adc_found"] is True
    assert status_info["configured_project_id"] == "sololedger-corp-gcp"
    assert status_info["ready_for_cloud_load"] is True

    calls = {"oauth": 0, "dataset": 0, "queries": []}

    def _mock_gcp(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            calls["oauth"] += 1
            return httpx.Response(200, json={"access_token": "adc-refreshed-token-123"})
        if request.url.path.endswith("/datasets"):
            calls["dataset"] += 1
            assert request.headers["Authorization"] == "Bearer adc-refreshed-token-123"
            return httpx.Response(200, json={"id": "sololedger-corp-gcp:sololedger_analytics"})
        if request.url.path.endswith("/queries"):
            payload = json.loads(request.content.decode("utf-8"))
            calls["queries"].append(payload["query"])
            return httpx.Response(200, json={"jobComplete": True})
        return httpx.Response(404)

    transport = httpx.MockTransport(_mock_gcp)
    token = _resolve_bq_token(transport=transport)
    assert token == "adc-refreshed-token-123"
    assert calls["oauth"] == 1

    prov = provision_bigquery_dataset_and_graph(
        dataset_id="sololedger_analytics",
        location="asia-northeast3",
        bq_transport=transport,
    )
    assert prov["status"] == "provisioned"
    assert prov["project_id"] == "sololedger-corp-gcp"
    assert prov["dataset_status"] == "created"
    # 5 node tables + 4 edge tables + 1 CREATE PROPERTY GRAPH = 10 statements
    assert prov["ddl_statements_executed"] == 10
    assert calls["dataset"] == 1
    assert len(calls["queries"]) == 10
    assert any("CREATE PROPERTY GRAPH" in q for q in calls["queries"])


def test_engineering_graph_and_bigquery_status_endpoints_and_dashboard(monkeypatch):
    monkeypatch.setenv("SOLOLEDGER_API_KEY", "test-secret-key")
    client = TestClient(app)
    headers = {"X-API-Key": "test-secret-key"}

    g_res = client.get("/api/v1/system/engineering-graph", headers=headers)
    assert g_res.status_code == 200
    g_data = g_res.json()
    assert g_data["passed"] is True
    assert g_data["summary"]["autowire_verified_count"] == 5

    g_full = client.get("/api/v1/system/engineering-graph?full=true", headers=headers)
    assert g_full.status_code == 200
    assert "graph" in g_full.json()
    assert len(g_full.json()["graph"]["nodes"]) > 10

    bq_st = client.get("/api/v1/analytics/bigquery-status", headers=headers)
    assert bq_st.status_code == 200
    assert "ready_for_cloud_load" in bq_st.json()

    assert client.post("/api/v1/analytics/bigquery-provision").status_code == 401
    assert (
        client.post(
            "/api/v1/analytics/bigquery-provision",
            json={"project_id": "bad;id"},
            headers=headers,
        ).status_code
        == 400
    )

    dash = client.get("/dashboard?year_month=ALL")
    assert dash.status_code == 200
    for token in (
        'id="btn-run-bq-cloud-sync"',
        'id="btn-run-eng-graph"',
        "function runBigQueryCloudSync(",
        "function runEngineeringGraphCheck(",
    ):
        assert token in dash.text
    assert "{{" not in dash.text
    assert "}}" not in dash.text
