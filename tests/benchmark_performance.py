"""Repeatable agent-session benchmarks; timing assertions are deliberately absent.

Run with PYTHONPATH pointing at the implementation to compare. Prepare fixtures
once, then run both implementations against the same paths and bytes. All stores,
profiles and outputs live in the explicitly selected (normally ignored) directory.
"""
from __future__ import annotations

import argparse
import cProfile
import hashlib
import json
import os
from pathlib import Path
import pstats
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable

from reql import MemoryGraph
from memory.agent import AgentWorkspace
from memory.domain.models import MemoryNode
from memory.services.coordination import CoordinationStore
from memory.storage import BlockGraphStore


def prepare(root: Path, modules: int, history: int) -> None:
    """Compile a connected source project and persist realistic shared history."""
    project = root / "project"
    project.mkdir(parents=True, exist_ok=True)
    package = project / "src" / "billing"
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    for index in range(modules):
        preceding = max(0, index - 1)
        imports = f"from .service_{preceding} import validate_payment as previous\n" if index else ""
        (package / f"service_{index}.py").write_text(
            imports + f'''"""Billing payment validation and invoice persistence shard {index}."""
def validate_payment(amount: int) -> int:
    """Validate an invoice before storing the payment."""
    if amount < 0:
        raise ValueError("negative payment")
    return amount

class InvoiceRepository:
    """Store validated billing invoices."""
    def save_invoice(self, amount: int) -> int:
        return validate_payment(amount)

def handle_payment(amount: int) -> int:
    """Handle a payment through its invoice repository."""
    return InvoiceRepository().save_invoice(amount)
''', encoding="utf-8")
    tests = project / "tests"
    tests.mkdir(exist_ok=True)
    (tests / "test_payment.py").write_text(
        "from billing.service_0 import validate_payment\ndef test_payment():\n    assert validate_payment(3) == 3\n",
        encoding="utf-8")
    (project / "README.md").write_text(
        "# Billing\nPayment validation calls `validate_payment` and `InvoiceRepository.save_invoice`.\n",
        encoding="utf-8")
    state = root / "seed"
    state.mkdir(exist_ok=True)
    graph = MemoryGraph.open(state / "memory.reql")
    try:
        graph.compile_project(project)
        graph.store.checkpoint_if_needed(wal_bytes_threshold=0)
    finally:
        graph.close()
    shutil.copy2(project / ".reql" / "artifact-cache.json", state / "artifact-cache.json")
    (state / "fixture-files.json").write_text(json.dumps({
        path.relative_to(project).as_posix(): path.stat().st_mtime_ns
        for path in project.rglob("*") if path.is_file() and ".reql" not in path.relative_to(project).parts
    }), encoding="utf-8")
    shared = CoordinationStore(state / "agent-dashboard.reql")
    goal = shared.put("goal", "Preserve payment invoice contracts", agent_id="agent:fixture", session_id="session:0")["node"]
    # Use the production write path for statuses, revisions and causal evidence.
    previous = goal["id"]
    for index in range(history):
        kind = "decision" if index % 7 == 0 else "task"
        status = "active" if kind == "decision" else ("done" if index % 3 == 0 else "open")
        item = shared.put(kind, f"Verify billing payment shard {index} contract",
            key=f"shard-{index}", agent_id=f"agent:{index % 4}", session_id=f"session:{index // 20}",
            status=status, rationale="Keep the invoice persistence contract", parent=goal["id"],
            files=[f"src/billing/service_{index % modules}.py"], workstream=f"billing-{index % 4}",
            depends_on=[previous] if kind == "task" and status == "open" else [goal["id"]] if status != "done" else [],
            supersedes=[previous] if kind == "decision" and index % 21 == 0 and index else [])['node']
        if index % 8 == 0:
            for revision in range(3):
                item = shared.put(kind, item["content"], record_id=item["id"], expected_revision=item["revision"],
                    agent_id="agent:fixture", session_id="session:revision", next_action=f"Check revision {revision}")["node"]
        previous = item["id"]
    with shared._open() as store:
        store.checkpoint_if_needed(wal_bytes_threshold=0)
    (root / "metadata.json").write_text(json.dumps({"modules": modules, "history": history}), encoding="utf-8")


def semantic(value: Any) -> Any:
    """Ignore only runtime identities/timestamps in cross-run comparisons."""
    if hasattr(value, "to_dict"):
        value = value.to_dict()
    if isinstance(value, dict):
        return {key: semantic(item) for key, item in value.items()
                if key not in {"trace_id", "created_at", "updated_at", "last_used_at", "last_activated_at",
                               "compiled_at", "checked_at", "timestamp", "duration_ms", "duration_seconds"}}
    if isinstance(value, (tuple, list)):
        return [semantic(item) for item in value]
    if isinstance(value, str):
        return re.sub(r"(?m)^- trace_id: retrieval:[a-f0-9]+$", "- trace_id: <runtime>", value)
    return value


def run(root: Path, repeats: int, profile: bool, cli: Path) -> dict[str, Any]:
    """Time complete operations, and optionally profile them in a separate pass."""
    active = root / "project" / ".reql"
    active.mkdir(exist_ok=True)
    for source in (root / "seed").iterdir():
        if source.is_file():
            shutil.copy2(source, active / source.name)
    for relative, mtime_ns in json.loads((root / "seed" / "fixture-files.json").read_text()).items():
        path = (root / "project" / relative).resolve()
        if not path.is_relative_to((root / "project").resolve()):
            raise ValueError(f"Fixture source path escapes its project: {relative}")
        os.utime(path, ns=(path.stat().st_atime_ns, mtime_ns))
    for suffix in ("memory.reql.wal", "memory.reql.usage.jsonl", "agent-dashboard.reql.wal"):
        if not (root / "seed" / suffix).exists():
            (active / suffix).unlink(missing_ok=True)
    storage = active / "memory.reql"
    shared = CoordinationStore(active / "agent-dashboard.reql")
    metrics: dict[str, Any] = {}
    results: dict[str, Any] = {}

    def measure(name: str, operation: Callable[[], Any], count: int = repeats) -> Any:
        samples = []
        for _ in range(count):
            start = time.perf_counter()
            value = operation()
            samples.append((time.perf_counter() - start) * 1000)
        metrics[name] = {"median_ms": statistics.median(samples), "samples_ms": samples}
        if profile:
            profiler = cProfile.Profile()
            profiler.runcall(operation)
            profiler.dump_stats(str(root / f"{name}.prof"))
            metrics[name]["profile_calls"] = {
                f"{Path(filename).name}:{function}": {"calls": calls, "cumulative_ms": cumulative * 1000}
                for (filename, _line, function), (_primitive, calls, _self, cumulative, _callers)
                in pstats.Stats(profiler).stats.items()
                if function in {"get_node", "find_nodes_by_types", "find_nodes_by_property", "_read_record_at",
                                "_node_match_metrics", "_tokens", "_execution", "_ensure_lexical_index_loaded",
                                "lexical_search", "_expand_and_rank_candidates", "canonicalize"}
            }
            with (root / f"{name}.profile.txt").open("w", encoding="utf-8") as out:
                pstats.Stats(profiler, stream=out).strip_dirs().sort_stats("cumulative").print_stats(25)
        return value

    def initialize() -> dict[str, int]:
        graph = MemoryGraph.open(storage, read_only=True)
        try:
            return {"nodes": graph.store.count_nodes(), "edges": graph.store.count_edges()}
        finally:
            graph.close()

    results["initialize"] = measure("initialize", initialize)

    def initial_compile() -> None:
        with tempfile.TemporaryDirectory(dir=active) as temporary:
            fresh = MemoryGraph.open(Path(temporary) / "memory.reql")
            try:
                fresh.compile_project(root / "project", cache_enabled=False)
            finally:
                fresh.close()

    measure("initial_compile", initial_compile, count=1)
    graph = MemoryGraph.open(storage)
    try:
        counter = 0

        def write() -> dict[str, Any]:
            nonlocal counter
            counter += 1
            node, _ = graph.store.upsert_node(MemoryNode(id=f"benchmark:{counter}", type="Concept",
                canonical_key=f"benchmark:{counter}", label="Agent measurement", text="Payment validation evidence"))
            return {"type": node.type, "text": node.text}

        results["write"] = measure("individual_write", write)
        results["update"] = measure("individual_update", lambda: graph.store.update_node_fields("benchmark:1", utility=0.75).utility)
        results["duplicate"] = measure("duplicate_write", lambda: graph.store.upsert_node(graph.store.get_node("benchmark:1"))[1])
        measure("repeated_writes", lambda: [write() for _ in range(10)])
        measure("repeated_updates", lambda: [graph.store.increment_node("benchmark:1", utility=0.01) for _ in range(10)])
        results["focused"] = measure("focused_retrieval", lambda: graph.query_context_payload("src/billing/service_0.py", scopes=("code",)))
        results["broad"] = measure("broad_retrieval", lambda: graph.query_context_payload("payment invoice validation", scopes=("code",)))
        results["relationships"] = measure("relationship_retrieval", lambda: graph.query_explore("validate_payment"))
        results["tabular"] = measure("filtered_query", lambda: graph.query("FIND nodes WHERE type = 'Function' LIMIT 20").to_dict())
        measure("serialization", lambda: json.loads(json.dumps(results["broad"], ensure_ascii=False)))
        results["explain"] = measure("source_overview", lambda: graph.explain_project(root / "project").to_dict())
        measure("unchanged_compile", lambda: graph.compile_project(root / "project"), count=1)
        modified = root / "project" / "src" / "billing" / "service_0.py"
        original = modified.read_text(encoding="utf-8")
        original_stat = modified.stat()
        compile_counter = 0

        def incremental_compile() -> None:
            nonlocal compile_counter
            compile_counter += 1
            modified.write_text(original + f"\ndef invoice_total_{compile_counter}(amount: int) -> int:\n    return validate_payment(amount)\n", encoding="utf-8")
            graph.compile_project(root / "project")

        try:
            measure("incremental_compile", incremental_compile, count=1)
        finally:
            modified.write_text(original, encoding="utf-8")
            os.utime(modified, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    finally:
        graph.close()

    item = shared.records()[-1]
    results["work_show"] = measure("work_show", lambda: shared.show(item["id"]))
    results["work_focused"] = measure("work_focused", lambda: shared.context("payment shard 0", files=["src/billing/service_0.py"]))
    results["work_broad"] = measure("work_broad", lambda: shared.context("billing payment"))
    results["work_history"] = measure("work_history", lambda: shared.context(record_id=item["id"], include_history=True))
    results["work_overview"] = measure("work_overview", shared.overview)
    counter = 0

    def work_write() -> dict[str, Any]:
        nonlocal counter
        counter += 1
        return shared.put("observation", f"Measured payment inspection {counter}", agent_id="agent:benchmark", session_id="session:benchmark")

    measure("work_write", work_write)
    updated = item

    def work_update() -> dict[str, Any]:
        nonlocal updated
        updated = shared.put(updated["kind"], updated["content"], record_id=updated["id"],
            expected_revision=updated["revision"], agent_id="agent:benchmark", session_id="session:benchmark",
            next_action=f"Inspect revision {updated['revision']}")["node"]
        return updated

    measure("work_update", work_update)
    results["work_duplicate"] = measure("work_duplicate", lambda: shared.put(updated["kind"], updated["content"], record_id=updated["id"],
        expected_revision=updated["revision"], agent_id="agent:benchmark", session_id="session:benchmark")["changed"])

    def session() -> dict[str, Any]:
        graph = MemoryGraph.open(storage, read_only=True)
        try:
            return {"focused": graph.query_context_payload("src/billing/service_0.py", scopes=("code",)),
                    "broader": graph.query_context_payload("payment validation", scopes=("code",)),
                    "overview": AgentWorkspace.project_operational_overview(storage),
                    "work": shared.context("payment", files=["src/billing/service_0.py"])}
        finally:
            graph.close()

    measure("agent_session", session)
    from mcp.tools import query_context as mcp_query_context
    measure("mcp_context", lambda: mcp_query_context(storage_path=str(storage), query="payment validation", code=True))
    environment = {**os.environ, "PYTHONPATH": str(cli.parent / "src"), "PYTHONHASHSEED": os.environ.get("PYTHONHASHSEED", "0")}

    def cli_call(*arguments: str) -> Any:
        completed = subprocess.run([sys.executable, str(cli), *arguments],
            cwd=root / "project", env=environment, capture_output=True, text=True, check=True)
        return json.loads(completed.stdout)

    measure("cli_startup", lambda: cli_call("stats", "--json"))
    measure("cli_context", lambda: cli_call("query_context", "--query", "payment validation", "--code", "--json"))
    measure("cli_overview", lambda: cli_call("project", "overview", "--json"))
    cleaned = semantic(results)
    return {"metadata": json.loads((root / "metadata.json").read_text()), "python": sys.version,
            "metrics": metrics, "results": cleaned,
            "digest": hashlib.sha256(json.dumps(cleaned, sort_keys=True).encode()).hexdigest()}


def run_existing(root: Path, project: Path, repeats: int, profile: bool) -> dict[str, Any]:
    """Measure a copy of a real graph without compiling or altering its source."""
    metrics, results = {}, {}
    graph = MemoryGraph.open(root / "memory.reql", read_only=True)
    try:
        metadata = {"nodes": graph.store.count_nodes(), "edges": graph.store.count_edges()}
        for name, operation in (
            ("source_overview", lambda: graph.explain_project(project).to_dict()),
            ("source_retrieval", lambda: graph.query_context_payload("storage retrieval", scopes=("code",))),
        ):
            samples = []
            for _ in range(repeats):
                start = time.perf_counter()
                results[name] = operation()
                samples.append((time.perf_counter() - start) * 1000)
            metrics[name] = {"median_ms": statistics.median(samples), "samples_ms": samples}
            if profile:
                profiler = cProfile.Profile()
                profiler.runcall(operation)
                with (root / f"{name}.profile.txt").open("w", encoding="utf-8") as out:
                    pstats.Stats(profiler, stream=out).strip_dirs().sort_stats("cumulative").print_stats(25)
    finally:
        graph.close()
    return {"metadata": metadata, "python": sys.version, "metrics": metrics, "results": semantic(results)}


def main() -> None:
    """Prepare or measure explicit fixtures without touching the user's graph."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--modules", type=int, default=24)
    parser.add_argument("--history", type=int, default=80)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--profile", action="store_true")
    parser.add_argument("--cli", type=Path, default=Path(__file__).resolve().parents[1] / "cli.py")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--compare", type=Path, help="Require semantic equality with an earlier result")
    parser.add_argument("--existing-project", type=Path, help="Measure a copied existing graph for this project")
    args = parser.parse_args()
    root = args.root.resolve()
    if args.prepare:
        prepare(root, args.modules, args.history)
    else:
        output = (run_existing(root, args.existing_project.resolve(), args.repeats, args.profile)
                  if args.existing_project else run(root, args.repeats, args.profile, args.cli.resolve()))
        if args.compare:
            previous = semantic(json.loads(args.compare.read_text(encoding="utf-8"))["results"])
            mismatches = [key for key in previous.keys() | output["results"].keys()
                          if previous.get(key) != output["results"].get(key)]
            if mismatches:
                raise AssertionError(f"Behavior differs in: {', '.join(sorted(mismatches))}")
        if args.output:
            args.output.write_text(json.dumps(output, indent=2), encoding="utf-8")
        print(json.dumps({name: round(metric["median_ms"], 3) for name, metric in output["metrics"].items()}, indent=2))


if __name__ == "__main__":
    main()
