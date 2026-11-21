"""Multi-Layer Code, Runtime, Schema & Domain Knowledge Graph Engineering Engine."""

import ast
import importlib
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from sololedger.config import PROJECT_ROOT, resolve_bundled_path

REQUIRED_SQLITE_TABLES = (
    "merchants",
    "card_transactions",
    "receipt_ocr_metadata",
    "tax_classification_rules",
    "workflow_checkpoints",
)

REQUIRED_STATE_GRAPH_NODES = (
    "ingest_parse",
    "nts_and_rule_eval",
    "llm_tax_inference",
    "arithmetic_tax_validator",
    "human_interrupt",
    "commit_ledger",
)

REQUIRED_BQ_NODE_TABLES = (
    "merchants",
    "card_transactions",
    "order_line_items",
    "account_categories",
    "construction_sites",
)

REQUIRED_BQ_EDGE_TABLES = (
    "edge_purchased_at",
    "edge_contains_item",
    "edge_classified_as",
    "edge_attributed_to",
)

REQUIRED_RUNTIME_AUTOWIRES: Tuple[Tuple[str, str], ...] = (
    ("POST /api/v1/webhooks/sms", "run_transaction_workflow"),
    ("POST /api/v1/webhooks/push", "run_transaction_workflow"),
    ("POST /api/v1/webhooks/telegram", "sync_checkpoint_from_ledger"),
    ("POST /api/v1/receipts/upload", "sync_checkpoint_from_ledger"),
    ("POST /api/v1/exports/tax-pack", "run_pre_export_tax_audit"),
)

TRACKED_GRAPH_SYMBOLS = {
    "run_transaction_workflow",
    "resume_transaction_workflow",
    "sync_checkpoint_from_ledger",
    "run_pre_export_tax_audit",
    "export_ledger_graph_to_bigquery",
    "provision_bigquery_dataset_and_graph",
    "get_bigquery_connection_status",
    "verify_engineering_graph",
}

_STATE_ROUTER_EXPECTED_TARGETS: Dict[str, Tuple[str, ...]] = {
    "_route_after_rule_eval": ("llm_tax_inference", "human_interrupt", "commit_ledger"),
    "_route_after_validator": ("llm_tax_inference", "human_interrupt", "commit_ledger"),
}

_CREATE_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(([\s\S]*?)\)\s*;",
    re.IGNORECASE,
)
_FK_RE = re.compile(
    r"FOREIGN\s+KEY\s*\(([^)]+)\)\s*REFERENCES\s+([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)
_BQ_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+`[^`]+\.([A-Za-z_][A-Za-z0-9_]*)`",
    re.IGNORECASE,
)
_BQ_PROP_GRAPH_RE = re.compile(
    r"CREATE\s+(?:OR\s+REPLACE\s+)?PROPERTY\s+GRAPH\s+(?:IF\s+NOT\s+EXISTS\s+)?`([^`]+)`",
    re.IGNORECASE,
)
_GQL_QUERY_RE = re.compile(r"--\s*@gql_query:\s*([A-Za-z0-9_]+)")


def _extract_sololedger_imports(tree: ast.AST, valid_modules: Set[str]) -> Set[str]:
    imported: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in valid_modules:
                    imported.add(alias.name)
                elif alias.name.startswith("sololedger."):
                    base = ".".join(alias.name.split(".")[:2])
                    if base in valid_modules:
                        imported.add(base)
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module in valid_modules:
                imported.add(node.module)
            elif node.module == "sololedger":
                for alias in node.names:
                    cand = f"sololedger.{alias.name}"
                    if cand in valid_modules:
                        imported.add(cand)
    return imported


def _find_import_cycles(adj: Dict[str, Set[str]]) -> List[List[str]]:
    color: Dict[str, int] = {m: 0 for m in adj}
    cycles: List[List[str]] = []

    def _dfs(node: str, stack: List[str]) -> None:
        color[node] = 1
        for nxt in sorted(adj.get(node, ())):
            if color.get(nxt, 0) == 1:
                idx = stack.index(nxt)
                cycles.append(stack[idx:] + [nxt])
            elif color.get(nxt, 0) == 0:
                _dfs(nxt, stack + [nxt])
        color[node] = 2

    for mod in sorted(adj):
        if color[mod] == 0:
            _dfs(mod, [mod])
    return cycles


def _collect_called_names(fn_node: ast.AST) -> Set[str]:
    names: Set[str] = set()
    for node in ast.walk(fn_node):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def _parse_api_ast_routes(api_py_path: Path) -> Dict[str, Set[str]]:
    """Extract FastAPI route keys ('METHOD /path') -> called symbol names from api.py AST."""
    tree = ast.parse(api_py_path.read_text(encoding="utf-8"))
    route_calls: Dict[str, Set[str]] = {}
    http_methods = {"get", "post", "put", "patch", "delete"}

    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        called = _collect_called_names(node)
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call):
                continue
            func = dec.func
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "app"
                and func.attr.lower() in http_methods
                and dec.args
                and isinstance(dec.args[0], ast.Constant)
                and isinstance(dec.args[0].value, str)
            ):
                route_key = f"{func.attr.upper()} {dec.args[0].value}"
                route_calls[route_key] = called
    return route_calls


def _collect_live_api_routes() -> Dict[str, Set[str]]:
    """Inspect live FastAPI app.routes when running from frozen binary or loaded runtime."""
    api_mod = sys.modules.get("sololedger.api")
    if api_mod is None:
        try:
            api_mod = importlib.import_module("sololedger.api")
        except Exception:
            return {}
    app_obj = getattr(api_mod, "app", None)
    if app_obj is None:
        return {}

    route_calls: Dict[str, Set[str]] = {}
    for route in getattr(app_obj, "routes", []):
        methods = getattr(route, "methods", None)
        path = getattr(route, "path", None)
        endpoint = getattr(route, "endpoint", None)
        if not methods or not path:
            continue
        co_names = set(getattr(getattr(endpoint, "__code__", None), "co_names", ()) or ())
        for m in methods:
            key = f"{m.upper()} {path}"
            route_calls[key] = co_names
    return route_calls


def _parse_sqlite_schema_graph(schema_path: Path) -> Tuple[List[str], List[Dict[str, str]]]:
    if not schema_path.exists():
        return [], []
    sql_text = schema_path.read_text(encoding="utf-8")
    tables: List[str] = []
    fk_edges: List[Dict[str, str]] = []
    for match in _CREATE_TABLE_RE.finditer(sql_text):
        tbl_name, body = match.groups()
        tables.append(tbl_name)
        for fk_match in _FK_RE.finditer(body):
            fk_col, ref_tbl = fk_match.groups()
            fk_edges.append(
                {
                    "source": f"table:{tbl_name}",
                    "target": f"table:{ref_tbl}",
                    "type": "FK_REFERENCES",
                    "column": fk_col.strip(),
                }
            )
    return tables, fk_edges


def _collect_state_graph_topology() -> Dict[str, Any]:
    from sololedger.workflow_graph import (  # noqa: PLC0415
        END_NODE,
        INTERRUPT_NODE,
        build_tx_resolution_graph,
    )

    graph = build_tx_resolution_graph()
    registered_nodes = sorted(graph.nodes.keys())
    edges: List[Dict[str, str]] = []
    adj: Dict[str, Set[str]] = {n: set() for n in registered_nodes}

    for src, dst in graph.edges.items():
        edges.append({"source": f"state:{src}", "target": f"state:{dst}", "type": "DIRECT"})
        if dst not in (END_NODE, INTERRUPT_NODE):
            adj.setdefault(src, set()).add(dst)

    for src, router_fn in graph.conditional_edges.items():
        fn_name = getattr(router_fn, "__name__", "")
        targets = _STATE_ROUTER_EXPECTED_TARGETS.get(fn_name, ())
        for dst in targets:
            edges.append(
                {
                    "source": f"state:{src}",
                    "target": f"state:{dst}",
                    "type": "CONDITIONAL",
                    "router": fn_name,
                }
            )
            if dst not in (END_NODE, INTERRUPT_NODE):
                adj.setdefault(src, set()).add(dst)

    entry = graph.entry_point or ""
    reachable: Set[str] = set()
    if entry and entry in graph.nodes:
        queue = [entry]
        reachable.add(entry)
        while queue:
            cur = queue.pop(0)
            for nxt in adj.get(cur, ()):
                if nxt not in reachable:
                    reachable.add(nxt)
                    queue.append(nxt)

    return {
        "entry_point": entry,
        "nodes": registered_nodes,
        "edges": edges,
        "reachable_nodes": sorted(reachable),
        "unreachable_nodes": sorted(set(registered_nodes) - reachable),
    }


def _parse_bigquery_graph_sql(bq_sql_path: Path) -> Dict[str, Any]:
    if not bq_sql_path.exists():
        return {
            "exists": False,
            "tables": [],
            "property_graphs": [],
            "gql_queries": [],
        }
    sql_text = bq_sql_path.read_text(encoding="utf-8")
    return {
        "exists": True,
        "tables": _BQ_TABLE_RE.findall(sql_text),
        "property_graphs": _BQ_PROP_GRAPH_RE.findall(sql_text),
        "gql_queries": _GQL_QUERY_RE.findall(sql_text),
    }


def _render_architecture_mermaid(
    module_count: int,
    route_autowires: List[Tuple[str, str]],
    sqlite_tables: List[str],
) -> str:
    lines = [
        "flowchart LR",
        f'    MODS["SoloLedger Python Modules ({module_count})"]',
        '    WF["workflow_graph.py (StateGraph + Checkpointer)"]',
        '    AUD["audit_graph.py (Parallel Fan-out/Join)"]',
        '    BQ["bq_graph_exporter.py (BigQuery Property Graph)"]',
        f'    DB["SQLite Ledger ({len(sqlite_tables)} Tables)"]',
        "    MODS --> WF",
        "    MODS --> AUD",
        "    MODS --> BQ",
        "    WF --> DB",
        "    AUD --> DB",
    ]
    for idx, (route_key, sym) in enumerate(route_autowires, start=1):
        target_node = "AUD" if "audit" in sym else "WF"
        lines.append(f'    R{idx}["{route_key}"] -->|"{sym}"| {target_node}')
    return "\n".join(lines)


def build_engineering_graph(
    repo_root: Optional[Path] = None,
    src_dir: Optional[Path] = None,
    tests_dir: Optional[Path] = None,
    schema_path: Optional[Path] = None,
    bq_sql_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Build multi-layer code, API, SQLite schema, StateGraph & BigQuery dependency graph."""
    root = repo_root or PROJECT_ROOT
    resolved_src = src_dir if src_dir is not None else (root / "src" / "sololedger")
    if tests_dir is not None:
        resolved_tests: Optional[Path] = tests_dir
    elif src_dir is not None:
        resolved_tests = None
    else:
        resolved_tests = root / "tests"

    is_frozen = bool(getattr(sys, "frozen", False))
    has_source_tree = resolved_src.exists() and any(resolved_src.glob("*.py"))

    nodes: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []

    module_adj: Dict[str, Set[str]] = {}
    tested_direct: Set[str] = set()

    if has_source_tree:
        module_files = {
            f"sololedger.{p.stem}": p
            for p in sorted(resolved_src.glob("*.py"))
            if p.stem != "__init__"
        }
        valid_modules = set(module_files.keys())
        for mod_name in sorted(valid_modules):
            module_adj[mod_name] = set()
            nodes.append({"id": mod_name, "layer": "MODULE", "label": mod_name})

        for mod_name, mod_path in sorted(module_files.items()):
            tree = ast.parse(mod_path.read_text(encoding="utf-8"))
            imps = _extract_sololedger_imports(tree, valid_modules)
            imps.discard(mod_name)
            module_adj[mod_name] = imps
            for target_mod in sorted(imps):
                edges.append(
                    {"source": mod_name, "target": target_mod, "type": "IMPORTS"}
                )

        if resolved_tests is not None and resolved_tests.exists():
            for test_path in sorted(resolved_tests.glob("test_*.py")):
                test_id = f"test:{test_path.name}"
                nodes.append({"id": test_id, "layer": "TEST_MODULE", "label": test_path.name})
                t_tree = ast.parse(test_path.read_text(encoding="utf-8"))
                t_imps = _extract_sololedger_imports(t_tree, valid_modules)
                tested_direct.update(t_imps)
                for target_mod in sorted(t_imps):
                    edges.append(
                        {"source": test_id, "target": target_mod, "type": "TESTS_IMPORT"}
                    )
    else:
        for mod_name in sorted(sys.modules.keys()):
            if mod_name.startswith("sololedger.") and not mod_name.endswith(".__init__"):
                module_adj[mod_name] = set()
                nodes.append({"id": mod_name, "layer": "MODULE", "label": mod_name})

    cycles = _find_import_cycles(module_adj)

    reachable_from_tests: Set[str] = set(tested_direct)
    queue = list(tested_direct)
    while queue:
        cur = queue.pop(0)
        for nxt in module_adj.get(cur, ()):
            if nxt not in reachable_from_tests:
                reachable_from_tests.add(nxt)
                queue.append(nxt)

    untested_modules = (
        sorted(set(module_adj.keys()) - reachable_from_tests)
        if (has_source_tree and resolved_tests is not None and resolved_tests.exists())
        else []
    )

    api_py_path = resolved_src / "api.py"
    if has_source_tree and api_py_path.exists():
        route_calls = _parse_api_ast_routes(api_py_path)
    elif src_dir is None:
        route_calls = _collect_live_api_routes()
    else:
        route_calls = {}

    wired_pairs: List[Tuple[str, str]] = []
    for route_key, called in sorted(route_calls.items()):
        r_id = f"route:{route_key}"
        nodes.append({"id": r_id, "layer": "ROUTE", "label": route_key})
        for sym in sorted(called & TRACKED_GRAPH_SYMBOLS):
            edges.append({"source": r_id, "target": f"symbol:{sym}", "type": "CALLS"})
            wired_pairs.append((route_key, sym))

    resolved_schema = schema_path or (
        (root / "sql" / "schema.sql")
        if (repo_root is not None and (root / "sql" / "schema.sql").exists())
        else resolve_bundled_path("sql", "schema.sql")
    )
    sqlite_tables, fk_edges = _parse_sqlite_schema_graph(resolved_schema)
    for tbl in sqlite_tables:
        nodes.append({"id": f"table:{tbl}", "layer": "DB_TABLE", "label": tbl})
    edges.extend(fk_edges)

    state_topo = _collect_state_graph_topology()
    for st_node in state_topo["nodes"]:
        nodes.append({"id": f"state:{st_node}", "layer": "STATE_NODE", "label": st_node})
    edges.extend(state_topo["edges"])

    resolved_bq_sql = bq_sql_path or (
        (root / "sql" / "bigquery_property_graph.sql")
        if (
            repo_root is not None
            and (root / "sql" / "bigquery_property_graph.sql").exists()
        )
        else resolve_bundled_path("sql", "bigquery_property_graph.sql")
    )
    bq_info = _parse_bigquery_graph_sql(resolved_bq_sql)
    for bq_tbl in bq_info["tables"]:
        nodes.append({"id": f"bq_table:{bq_tbl}", "layer": "BQ_GRAPH", "label": bq_tbl})

    mermaid = _render_architecture_mermaid(
        module_count=len(module_adj),
        route_autowires=wired_pairs,
        sqlite_tables=sqlite_tables,
    )

    return {
        "mode": "frozen" if (is_frozen or not has_source_tree) else "source",
        "has_source_tree": has_source_tree,
        "nodes": nodes,
        "edges": edges,
        "module_count": len(module_adj),
        "module_edge_count": sum(len(v) for v in module_adj.values()),
        "cycles": cycles,
        "tested_direct_count": len(tested_direct),
        "untested_modules": untested_modules,
        "route_count": len(route_calls),
        "route_calls": {k: sorted(v & TRACKED_GRAPH_SYMBOLS) for k, v in route_calls.items()},
        "sqlite_tables": sqlite_tables,
        "fk_edges": fk_edges,
        "state_graph": state_topo,
        "bigquery_graph": bq_info,
        "mermaid": mermaid,
    }


def verify_engineering_graph(
    repo_root: Optional[Path] = None,
    src_dir: Optional[Path] = None,
    tests_dir: Optional[Path] = None,
    schema_path: Optional[Path] = None,
    bq_sql_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Verify all 6 multi-layer code, runtime auto-wiring, schema & graph invariants."""
    graph = build_engineering_graph(
        repo_root=repo_root,
        src_dir=src_dir,
        tests_dir=tests_dir,
        schema_path=schema_path,
        bq_sql_path=bq_sql_path,
    )
    errors: List[str] = []

    # 1. Circular imports
    for cyc in graph["cycles"]:
        errors.append(f"Circular module dependency detected: {' -> '.join(cyc)}")

    # 2. Test reachability (only when source tree and tests exist)
    for mod in graph["untested_modules"]:
        errors.append(f"Untested module (not reachable from tests/test_*.py): {mod}")

    # 3. Runtime auto-wiring on FastAPI routes (skip only for isolated synthetic src_dir without api.py)
    check_routes = bool(graph["route_calls"]) or src_dir is None
    if check_routes:
        for route_key, expected_sym in REQUIRED_RUNTIME_AUTOWIRES:
            called_syms = graph["route_calls"].get(route_key)
            if called_syms is None:
                errors.append(f"Missing required API route for graph auto-wiring: {route_key}")
            elif expected_sym not in called_syms:
                errors.append(
                    f"Route '{route_key}' is missing required auto-wire call '{expected_sym}'"
                )

    # 4. SQLite 5 core tables & valid FK targets
    tbl_set = set(graph["sqlite_tables"])
    for req_tbl in REQUIRED_SQLITE_TABLES:
        if req_tbl not in tbl_set:
            errors.append(f"Missing required SQLite schema table: {req_tbl}")
    for fk in graph["fk_edges"]:
        target_tbl = fk["target"].removeprefix("table:")
        if target_tbl not in tbl_set:
            errors.append(
                f"Foreign key from {fk['source']} points to missing table: {target_tbl}"
            )

    # 5. StateGraph topology & reachability
    st = graph["state_graph"]
    if not st["entry_point"]:
        errors.append("StateGraph has no entry_point configured.")
    for req_node in REQUIRED_STATE_GRAPH_NODES:
        if req_node not in st["nodes"]:
            errors.append(f"StateGraph missing required node: {req_node}")
    for unreach in st["unreachable_nodes"]:
        errors.append(f"StateGraph node unreachable from '{st['entry_point']}': {unreach}")

    # 6. BigQuery Property Graph SQL template completeness
    bq = graph["bigquery_graph"]
    if not bq["exists"]:
        errors.append("Missing sql/bigquery_property_graph.sql template.")
    else:
        bq_tbls = set(bq["tables"])
        for req_bq in REQUIRED_BQ_NODE_TABLES + REQUIRED_BQ_EDGE_TABLES:
            if req_bq not in bq_tbls:
                errors.append(f"BigQuery Property Graph missing base table: {req_bq}")
        if not bq["property_graphs"]:
            errors.append("BigQuery Property Graph DDL missing CREATE PROPERTY GRAPH statement.")
        if len(bq["gql_queries"]) < 3:
            errors.append(
                f"Expected at least 3 named @gql_query blocks, found {len(bq['gql_queries'])}"
            )

    summary = {
        "mode": graph["mode"],
        "module_count": graph["module_count"],
        "module_edge_count": graph["module_edge_count"],
        "cycle_count": len(graph["cycles"]),
        "untested_module_count": len(graph["untested_modules"]),
        "route_count": graph["route_count"],
        "autowire_verified_count": len(REQUIRED_RUNTIME_AUTOWIRES)
        if check_routes and not any("auto-wire" in e or "API route" in e for e in errors)
        else 0,
        "sqlite_table_count": len(graph["sqlite_tables"]),
        "fk_edge_count": len(graph["fk_edges"]),
        "state_graph_node_count": len(st["nodes"]),
        "bq_table_count": len(bq["tables"]),
        "gql_query_count": len(bq["gql_queries"]),
        "total_nodes": len(graph["nodes"]),
        "total_edges": len(graph["edges"]),
    }

    return {
        "passed": len(errors) == 0,
        "errors": errors,
        "summary": summary,
        "mermaid": graph["mermaid"],
        "graph": {
            "nodes": graph["nodes"],
            "edges": graph["edges"],
            "cycles": graph["cycles"],
            "untested_modules": graph["untested_modules"],
            "route_calls": graph["route_calls"],
            "state_graph": st,
            "bigquery_graph": bq,
        },
    }
