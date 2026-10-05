"""Durable engineering work, separate from compiled facts and private scratch.

One mutable record owns each outcome or decision. Relations refer to stable ids;
bounded revisions retain the reasons for changes without an append-only log.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
import re
import hashlib
import json
from typing import Any, Iterator

from memory.domain.ids import stable_id
from memory.domain.models import MemoryNode
from memory.domain.timeutils import parse_dt, utcnow_iso
from memory.storage import BlockGraphStore
from memory.extraction.normalization import tokenize

KINDS = frozenset({"goal", "task", "decision", "constraint", "failure", "question", "change", "observation", "checkpoint"})
STATUSES = frozenset({"active", "open", "in_progress", "blocked", "done", "resolved", "superseded", "invalidated", "abandoned"})
TERMINAL = frozenset({"done", "resolved", "superseded", "invalidated", "abandoned"})
RELATIONS = ("parent", "depends_on", "contradicts", "supersedes", "summarizes")
RECORD_TYPE = "work_record"
MAX_REVISIONS = 8
MAX_TERMINAL_RECORDS = 200


def _tokens(text: str) -> set[str]:
    """Normalize identifiers, paths and prose for deterministic matching."""
    return set(tokenize(re.sub(r"[\\/._-]", " ", text)))


def _paths(paths: list[str]) -> list[str]:
    """Accept only repository-relative evidence paths; never open them here."""
    if not isinstance(paths, list) or any(not isinstance(value, str) for value in paths):
        raise ValueError("Evidence paths must be a list of strings")
    if len(paths) > 64:
        raise ValueError("At most 64 evidence paths per record or query")
    result = []
    for value in paths:
        path = PurePosixPath(value.replace("\\", "/"))
        if not value.strip() or path.is_absolute() or ".." in path.parts or ":" in value:
            raise ValueError(f"Evidence path must be repository-relative: {value}")
        normalized = path.as_posix()
        if normalized != "." and normalized not in result:
            result.append(normalized)
    return result


class CoordinationStore:
    """Serialize shared updates and project compact, causal engineering context."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().absolute()
        if not self.path.resolve().is_relative_to(self.path.parent.resolve()):
            raise ValueError("Engineering store must remain inside its storage directory")

    @contextmanager
    def _open(self, *, read_only: bool = False) -> Iterator[BlockGraphStore]:
        """Use the existing transactional block adapter and its writer lease."""
        store = BlockGraphStore(self.path, create=not read_only, read_only=read_only,
                                defer_lexical_index=True, lock_timeout_seconds=10)
        try:
            yield store
        finally:
            store.close()

    def records(self) -> list[dict[str, Any]]:
        """Read shared state without creating an absent store."""
        if not self.path.exists() or self.path.stat().st_size == 0:
            return []
        with self._open(read_only=True) as store:
            # These nodes belong to this short-lived read-only store. Its payloads
            # can escape after close without copying a second, inaccessible owner.
            return [self._payload(node) for node in store.find_nodes_by_types([RECORD_TYPE], clone=False)]

    @staticmethod
    def _payload(node: MemoryNode) -> dict[str, Any]:
        """Expose operational status without reusing graph salience semantics."""
        return {"summarizes": [], **node.properties, "id": node.id, "type": node.properties["kind"],
                "created_at": node.created_at, "updated_at": node.updated_at}

    def put(
        self, kind: str, content: str, *, agent_id: str, session_id: str | None,
        session_title: str = "", key: str | None = None, record_id: str | None = None,
        expected_revision: int | None = None, status: str | None = None,
        rationale: str | None = None, files: list[str] | None = None,
        workstream: str | None = None, parent: str | None = None,
        depends_on: list[str] | None = None, contradicts: list[str] | None = None,
        supersedes: list[str] | None = None, next_action: str | None = None,
        summarizes: list[str] | None = None,
        importance: int | None = None,
    ) -> dict[str, Any]:
        """Upsert one identity atomically; reject stale revisions and invalid links.

        Omitted fields survive updates. An explicit empty list clears a relation.
        Updating by id requires a revision, preventing silent cross-agent overwrite.
        """
        if not isinstance(kind, str) or not isinstance(content, str):
            raise ValueError("Record kind and content must be strings")
        if not isinstance(agent_id, str) or not agent_id.strip():
            raise ValueError("Record needs a nonempty agent identity")
        if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 0):
            raise ValueError("Expected revision must be a nonnegative integer")
        kind, content = kind.strip().casefold(), content.strip()
        if kind not in KINDS or not content or len(content) > 4000:
            raise ValueError("Record needs a supported kind and 1-4000 characters of content")
        if record_id and expected_revision is None:
            raise ValueError("Updating by id requires expected_revision; read agent show first")
        if status is not None and status not in STATUSES:
            raise ValueError(f"Unsupported work status: {status}")
        if importance is not None and (type(importance) is not int or importance not in {1, 2, 3}):
            raise ValueError("Importance must be 1, 2 or 3")
        for value in (rationale, next_action, workstream, key):
            if value is not None and (not isinstance(value, str) or len(value) > 4000):
                raise ValueError("Record fields must be strings of at most 4000 characters")
        if parent is not None and not isinstance(parent, str):
            raise ValueError("Parent must be a record id")
        if files is not None:
            files = _paths(files)
        for relation, values in (("depends_on", depends_on), ("contradicts", contradicts), ("supersedes", supersedes), ("summarizes", summarizes)):
            if values is not None and (not isinstance(values, list) or any(not isinstance(value, str) or not value for value in values)):
                raise ValueError(f"{relation} must be a list of record ids")
        depends_on = list(dict.fromkeys(depends_on)) if depends_on is not None else None
        contradicts = list(dict.fromkeys(contradicts)) if contradicts is not None else None
        supersedes = list(dict.fromkeys(supersedes)) if supersedes is not None else None
        summarizes = list(dict.fromkeys(summarizes)) if summarizes is not None else None
        with self._open() as store, store.transaction():
            implicit_key = (parent + "\n" if parent else "") + " ".join(content.casefold().split())
            identity = stable_id("work", kind, workstream or "", key or implicit_key)
            node = store.get_node(record_id or identity)
            if record_id and (node is None or node.type != RECORD_TYPE):
                raise ValueError(f"Work record not found: {record_id}")
            old = dict(node.properties) if node else {}
            if old:
                old.setdefault("summarizes", [])
            if old and old["kind"] != kind:
                raise ValueError("A record's kind cannot change")
            if expected_revision is not None and expected_revision != old.get("revision", 0):
                raise ValueError("Work revision changed; read agent show and reconcile before updating")
            now = utcnow_iso()
            props = {"kind": kind, "content": content, "title": content.splitlines()[0][:120],
                     "status": "open" if kind in {"task", "question"} else "active",
                     "rationale": "", "files": [], "workstream": "", "parent": "",
                     "depends_on": [], "contradicts": [], "supersedes": [], "summarizes": [], "next_action": "",
                     "importance": 2, **old}
            props.update(content=content, title=content.splitlines()[0][:120])
            supplied = {"status": status, "rationale": rationale, "files": files, "workstream": workstream,
                        "parent": parent, "depends_on": depends_on, "contradicts": contradicts,
                        "supersedes": supersedes, "summarizes": summarizes, "next_action": next_action, "importance": importance}
            props.update({name: value for name, value in supplied.items() if value is not None})
            item_id = node.id if node else identity
            if props["status"] != old.get("status") and props["status"] in {"blocked", "superseded", "invalidated", "abandoned"} and not rationale:
                raise ValueError("Blocked or obsolete work needs a rationale")
            if old.get("status") in TERMINAL and props["status"] not in TERMINAL and not rationale:
                raise ValueError("Reopening completed or obsolete work needs a rationale")
            if (props["supersedes"] != old.get("supersedes", []) or kind in {"decision", "failure"}) and not props["rationale"]:
                raise ValueError("Decisions, failures and replacement need a rationale")
            records: dict[str, dict[str, Any]] = {}
            for relation in RELATIONS:
                targets = [props[relation]] if relation == "parent" and props[relation] else ([] if relation == "parent" else props[relation])
                if len(targets) > 32:
                    raise ValueError("At most 32 links per relation")
                for target in targets:
                    target_node = store.get_node(target, clone=False) if target != item_id else None
                    if target_node is None or target_node.type != RECORD_TYPE:
                        raise ValueError(f"Invalid {relation} target: {target}")
                    records[target] = target_node.properties
                    if relation == "supersedes" and target not in old.get("supersedes", []) and records[target].get("replaced_by"):
                        raise ValueError(f"Already superseded: {target}; reconcile its current replacement {records[target]['replaced_by']}")
            records[item_id] = props
            self._validate_cycles(records, item_id, store=store)
            if kind == "task" and props["status"] == "done":
                # Completion depends on incoming conflicts as well as direct
                # links, so it still requires the complete consistent snapshot.
                records = {n.id: n.properties for n in store.find_nodes_by_types([RECORD_TYPE], clone=False)}
                records[item_id] = props
                execution = self._execution({**props, "id": item_id, "status": "open"}, records)
                if execution["execution_state"] in {"blocked", "needs_review"}:
                    raise ValueError("Cannot complete task with waiting, obsolete or conflicting dependencies")
            changed = {name: value for name, value in props.items() if name not in {"history", "revision", "agent_id", "session_id", "session_title", "activity_id"}}
            previous = {name: value for name, value in old.items() if name in changed}
            if node and changed == previous:
                return {"created": False, "changed": False, "node": self._payload(node)}
            if node and expected_revision is None:
                raise ValueError(f"Record already exists: {node.id}; update with its revision")
            history = list(old.get("history", []))
            if node:
                history.append({name: value for name, value in self._payload(node).items() if name != "history"})
            props.update(revision=int(old.get("revision", 0)) + 1, history=history[-MAX_REVISIONS:],
                         agent_id=agent_id, session_id=session_id, session_title=session_title,
                         origin_agent_id=old.get("origin_agent_id", agent_id))
            for target in props["supersedes"]:
                if target in old.get("supersedes", []):
                    continue
                replaced = store.get_node(target)
                if replaced is None:
                    raise ValueError(f"Replacement target disappeared: {target}")
                replacement_props = dict(replaced.properties)
                revision_history = list(replacement_props.get("history", []))
                revision_history.append({k: v for k, v in self._payload(replaced).items() if k != "history"})
                replacement_props.update(status="superseded", replaced_by=item_id, replacement_reason=props["rationale"],
                                         replacement_agent_id=agent_id, replacement_session_id=session_id,
                                         revision=replacement_props["revision"] + 1, history=revision_history[-MAX_REVISIONS:])
                store.update_node_fields(target, properties=replacement_props, updated_at=now)
            stored, created = store.upsert_node(MemoryNode(
                id=item_id, type=RECORD_TYPE, canonical_key=item_id, label=props["title"], text=content,
                properties=props, created_at=node.created_at if node else now, updated_at=now))
            return {"created": created, "changed": True, "node": self._payload(stored)}

    @staticmethod
    def _validate_cycles(records: dict[str, dict[str, Any]], start: str,
                         *, store: BlockGraphStore) -> None:
        """Reject dependency, hierarchy and replacement cycles before any write."""
        for relation in ("parent", "depends_on", "supersedes"):
            visiting: set[str] = set()
            visited: set[str] = set()

            def visit(item_id: str) -> None:
                if item_id in visiting:
                    raise ValueError(f"Cycle in {relation}")
                if item_id in visited:
                    return
                visiting.add(item_id)
                if item_id not in records:
                    node = store.get_node(item_id, clone=False)
                    if node is None or node.type != RECORD_TYPE:
                        raise KeyError(item_id)
                    records[item_id] = node.properties
                item = records[item_id]
                targets = [item[relation]] if relation == "parent" and item[relation] else ([] if relation == "parent" else item[relation])
                for target in targets:
                    visit(target)
                visiting.remove(item_id)
                visited.add(item_id)
            visit(start)

    def show(self, record_id: str) -> dict[str, Any]:
        """Retrieve a full record and its bounded revision evidence."""
        if self.path.exists() and self.path.stat().st_size:
            with self._open(read_only=True) as store:
                node = store.get_node(record_id)
                if node is not None and node.type == RECORD_TYPE:
                    return self._payload(node)
        raise ValueError(f"Work record not found: {record_id}")

    @classmethod
    def migrate(cls, target: BlockGraphStore, nodes: list[MemoryNode], agent_id: str) -> list[str]:
        """Move legacy operational records, preserving ids and their provenance."""
        moved = []
        with target.transaction():
            for node in nodes:
                kind = {"rejected_approach": "failure", "finding": "observation"}.get(node.type, node.type)
                if kind not in KINDS:
                    continue
                if target.get_node(node.id) is None:
                    metadata = node.properties.get("metadata") or {}
                    props = {"kind": kind, "content": node.properties.get("content") or node.text or node.label,
                             "title": node.label, "status": node.status, "rationale": metadata.get("reason", ""),
                             "files": [], "workstream": "", "parent": "", "depends_on": [], "contradicts": [],
                             "supersedes": [], "summarizes": [], "next_action": "", "importance": 2, "revision": 1, "history": [],
                             "agent_id": agent_id, "origin_agent_id": agent_id,
                             "session_id": node.properties.get("session_id"), "session_title": node.properties.get("session_title"),
                             "legacy": True}
                    if node.type == "rejected_approach":
                        props["status"] = "active"
                    if node.status == "done":
                        props["rationale"] = node.properties.get("completion_message", "")
                    target.upsert_node(MemoryNode(id=node.id, type=RECORD_TYPE, canonical_key=node.id,
                                                 label=node.label, text=node.text, properties=props,
                                                 created_at=node.created_at, updated_at=node.updated_at))
                moved.append(node.id)
        return moved

    @staticmethod
    def _conflict_index(records: dict[str, dict[str, Any]]) -> dict[str, set[str]]:
        """Index current conflict endpoints once per consistent record snapshot."""
        conflicts: dict[str, set[str]] = defaultdict(set)
        for item_id, item in records.items():
            for target in item["contradicts"]:
                if target in records and records[target]["status"] not in TERMINAL:
                    conflicts[item_id].add(target)
                if item["status"] not in TERMINAL:
                    conflicts[target].add(item_id)
        return conflicts

    @staticmethod
    def _execution(item: dict[str, Any], records: dict[str, dict[str, Any]],
                   conflicts_by_id: dict[str, set[str]] | None = None) -> dict[str, Any]:
        """Derive readiness, invalid assumptions and conflicts from current links."""
        if conflicts_by_id is None:
            conflicts_by_id = CoordinationStore._conflict_index(records)
        waiting, obsolete = [], []
        for target in item["depends_on"]:
            dependency = records.get(target)
            if dependency is None or dependency["status"] in {"superseded", "invalidated", "abandoned"}:
                obsolete.append(target)
            elif dependency["kind"] in {"task", "goal", "question"} and dependency["status"] not in {"done", "resolved"}:
                waiting.append(target)
            if dependency is not None and dependency["status"] not in TERMINAL:
                if conflicts_by_id.get(target):
                    obsolete.append(target)
        conflicts = conflicts_by_id.get(item["id"], set())
        state = item["status"]
        if state not in TERMINAL:
            state = "needs_review" if obsolete or conflicts else ("blocked" if waiting or state == "blocked" else ("ready" if state == "open" else state))
        return {"execution_state": state, "waiting_on": waiting, "obsolete_dependencies": obsolete, "conflicts": sorted(conflicts)}

    def context(self, query: str = "", *, files: list[str] | None = None, workstream: str | None = None,
                record_id: str | None = None, limit: int = 8, include_history: bool = False) -> dict[str, Any]:
        """Rank lexical/file anchors, then expand two causal hops under a hard budget."""
        if not 1 <= limit <= 40:
            raise ValueError("Context limit must be between 1 and 40")
        all_records = self.records()
        records = {item["id"]: item for item in all_records}
        conflicts_by_id = self._conflict_index(records)
        if record_id and record_id not in records:
            raise ValueError(f"Work record not found: {record_id}")
        tokens = _tokens(query)
        paths = _paths(files or [])
        words = {item["id"]: _tokens(" ".join([item["content"], item["rationale"], item["next_action"], item["workstream"], *item["files"]])) for item in all_records}
        frequency = Counter(term for terms in words.values() for term in terms)
        denominator = sum(1 / frequency.get(term, 1) for term in tokens) or 1
        now = parse_dt(utcnow_iso())
        scores: dict[str, float] = {}
        reasons: dict[str, list[str]] = {}
        for item in all_records:
            if workstream is not None and item["workstream"] != workstream:
                continue
            if not include_history and item["status"] in {"superseded", "invalidated", "abandoned"}:
                continue
            overlap = sum(1 / frequency[term] for term in tokens & words[item["id"]]) / denominator
            path_match = any(path == evidence or path.startswith(evidence + "/") or evidence.startswith(path + "/") for path in paths for evidence in item["files"])
            anchors = []
            if overlap:
                anchors.append("text")
            if path_match:
                anchors.append("file")
            if record_id == item["id"]:
                anchors.append("task")
            if not tokens and not paths and not record_id:
                anchors.append("overview")
            if not anchors:
                continue
            age = max(0, (now.date() - parse_dt(item["updated_at"]).date()).days)
            priority = 0.18 if item["kind"] in {"decision", "constraint", "failure", "goal", "question"} else 0
            score = overlap * 2 + path_match * 3 + (record_id == item["id"]) * 4 + priority + item["importance"] * 0.05 + 0.1 / (1 + age)
            if item["status"] not in TERMINAL:
                score += 0.25
            scores[item["id"]], reasons[item["id"]] = score, anchors
        frontier = sorted(scores, key=lambda item_id: (-scores[item_id], item_id))[:4]
        # Parents/goals orient a task but do not fan out into all sibling work.
        for _ in range(2):
            next_frontier = []
            for item_id in frontier:
                item = records[item_id]
                related = set(item["depends_on"] + item["contradicts"] + item["summarizes"] + ([item["parent"]] if item["parent"] else []))
                related.update(other["id"] for other in all_records if item_id in other["depends_on"] or item_id in other["contradicts"])
                related.update(other["id"] for other in all_records if item_id in other["summarizes"])
                related.update(other["id"] for other in all_records if item_id in other["supersedes"])
                if item["kind"] == "task":
                    related.update(other["id"] for other in all_records if other["parent"] == item_id)
                if include_history:
                    related.update(item["supersedes"])
                for target in sorted(related):
                    if len(scores) >= 80:
                        break
                    candidate = records.get(target)
                    if candidate is None or (not include_history and candidate["status"] in {"superseded", "invalidated", "abandoned"}):
                        continue
                    if target not in scores:
                        scores[target], reasons[target] = scores[item_id] * 0.65, [f"related:{item_id}"]
                        next_frontier.append(target)
            frontier = next_frontier[:12]
        ranked = sorted(scores, key=lambda item_id: (-scores[item_id], records[item_id]["updated_at"], item_id))
        selected = []
        for item_id in ranked[:limit]:
            item = records[item_id]
            compact = {k: v for k, v in item.items() if k != "history"}
            if include_history and record_id == item_id:
                compact["history"] = item["history"]
            compact.update(self._execution(item, records, conflicts_by_id), score=round(scores[item_id], 4), reasons=reasons[item_id])
            selected.append(compact)
        revision = hashlib.sha256(json.dumps(selected, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
        return {"format": "reql-work-context-v1", "revision": revision, "query": query, "records": selected,
                "total_matches": len(ranked), "omitted": max(0, len(ranked) - limit)}

    def overview(self, *, limit: int = 5) -> dict[str, Any]:
        """Project direction and execution from authoritative current records."""
        if not 1 <= limit <= 20:
            raise ValueError("Overview limit must be between 1 and 20")
        records = self.records()
        by_id = {item["id"]: item for item in records}
        conflicts_by_id = self._conflict_index(by_id)
        sections: dict[str, list[dict[str, Any]]] = {name: [] for name in ("goals", "direction", "active", "completed", "blockers", "failures", "next", "checkpoints")}
        workstreams: dict[str, Counter[str]] = {}
        for item in sorted(records, key=lambda item: (item["updated_at"], item["id"]), reverse=True):
            projected = {k: v for k, v in item.items() if k != "history"}
            projected.update(self._execution(item, by_id, conflicts_by_id))
            state = projected["execution_state"]
            if item["status"] in {"superseded", "invalidated", "abandoned"}:
                continue
            if item["kind"] == "goal" and item["status"] not in TERMINAL:
                sections["goals"].append(projected)
            if item["kind"] in {"decision", "constraint"} and item["status"] not in TERMINAL:
                sections["direction"].append(projected)
            if state in {"blocked", "needs_review"} or (item["kind"] == "question" and item["status"] not in TERMINAL):
                sections["blockers"].append(projected)
            if item["kind"] == "task":
                workstreams.setdefault(item["workstream"] or "default", Counter())[state] += 1
                sections["completed" if item["status"] in {"done", "resolved"} else "active"].append(projected)
                if state == "ready":
                    sections["next"].append(projected)
            if item["kind"] == "change" and item["status"] in {"done", "resolved"}:
                sections["completed"].append(projected)
            if item["kind"] == "checkpoint":
                sections["checkpoints"].append(projected)
            if item["kind"] == "failure" and item["status"] not in TERMINAL:
                sections["failures"].append(projected)
        return {"format": "reql-work-overview-v1", "sections": {k: v[:limit] for k, v in sections.items()},
                "counts": {k: len(v) for k, v in sections.items()}, "workstreams": {k: dict(v) for k, v in sorted(workstreams.items())}}

    def prune(self) -> int:
        """Bound unreferenced terminal outcomes while retaining live causal evidence."""
        if not self.path.exists():
            return 0
        with self._open() as store, store.transaction():
            nodes = store.find_nodes_by_types([RECORD_TYPE])
            referenced = set()
            for node in nodes:
                props = node.properties
                referenced.update(props["depends_on"] + props["contradicts"] + props["supersedes"] + props.get("summarizes", []))
                if props["parent"]:
                    referenced.add(props["parent"])
                if props.get("replaced_by"):
                    referenced.add(props["replaced_by"])
            terminal = sorted((node for node in nodes if node.properties["status"] in TERMINAL and node.id not in referenced), key=lambda node: (node.updated_at, node.id), reverse=True)
            expired = terminal[MAX_TERMINAL_RECORDS:]
            for node in expired:
                store.remove_node(node.id)
            if expired:
                store.compact_storage()
            return len(expired)


def render_work_context(payload: dict[str, Any], *, max_chars: int = 6000) -> str:
    """Render bounded work context with ids for evidence/history drill-down."""
    rows = ["## Engineering context"]
    for item in payload.get("records", []):
        line = f"- [{item['kind']}/{item['execution_state']}] {item['content'][:360]} ({item['id']}, r{item['revision']}, {item['agent_id']})"
        if item["rationale"]:
            line += f" — Why: {item['rationale'][:240]}"
        if item["files"]:
            line += "; files: " + ", ".join(item["files"][:4])
        if item["next_action"]:
            line += f"; next: {item['next_action'][:200]}"
        if item.get("waiting_on"):
            line += "; waiting: " + ", ".join(item["waiting_on"])
        if item.get("obsolete_dependencies") or item.get("conflicts"):
            line += "; reconcile: " + ", ".join(item.get("obsolete_dependencies", []) + item.get("conflicts", []))
        if item["supersedes"]:
            line += "; replaces: " + ", ".join(item["supersedes"])
        if sum(len(row) + 1 for row in rows) + len(line) > max_chars:
            rows.append("- More evidence available via agent show or project context --json.")
            break
        rows.append(line)
    if not payload.get("records"):
        rows.append("No matching engineering records.")
    return "\n".join(rows)[:max_chars]
