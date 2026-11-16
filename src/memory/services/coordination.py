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
from typing import Any, Iterable, Iterator

from memory.domain.ids import stable_id
from memory.domain.models import MemoryNode
from memory.domain.timeutils import parse_dt, utcnow_iso
from memory.storage import BlockGraphStore
from memory.extraction.normalization import tokenize

KINDS = frozenset({"goal", "task", "decision", "constraint", "failure", "question", "change", "observation", "checkpoint"})
STATUSES = frozenset({"active", "open", "in_progress", "blocked", "done", "resolved", "superseded", "invalidated", "abandoned"})
OBSOLETE_STATUSES = frozenset({"superseded", "invalidated", "abandoned"})
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
        self, kind: str | None = None, content: str | None = None, *, agent_id: str, session_id: str | None,
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

        Omitted fields, including kind/content, survive updates by id and revision.
        Creation requires kind/content. An explicit empty list clears a relation.
        Updating by id requires a revision, preventing silent cross-agent overwrite.
        """
        if (kind is not None and not isinstance(kind, str)) or (content is not None and not isinstance(content, str)):
            raise ValueError("Record kind and content must be strings")
        if not isinstance(agent_id, str) or not agent_id.strip():
            raise ValueError("Record needs a nonempty agent identity")
        if record_id is not None and (not isinstance(record_id, str) or not record_id.strip()):
            raise ValueError("Assigned record id must be a nonempty string")
        if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 0):
            raise ValueError("Expected revision must be a nonnegative integer")
        kind = kind.strip().casefold() if kind is not None else None
        content = content.strip() if content is not None else None
        if (kind is not None and kind not in KINDS) or (content is not None and (not content or len(content) > 4000)):
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
        partial = kind is None or content is None
        if record_id is None:
            if kind is None or content is None:
                raise ValueError("Creating work requires kind and content")
            implicit_key = (parent + "\n" if parent else "") + " ".join(content.casefold().split())
            identity = stable_id("work", kind, workstream or "", key or implicit_key)
        else:
            identity = record_id
        with self._open() as store, store.transaction():
            node = store.get_node(identity)
            if record_id and (node is None or node.type != RECORD_TYPE):
                raise ValueError(f"Work record not found: {record_id}")
            old = dict(node.properties) if node else {}
            if partial:
                kind = old.get("kind") if kind is None else kind
                content = old.get("content") if content is None else content
                if not isinstance(kind, str) or not isinstance(content, str):
                    raise ValueError("Stored work record has invalid kind or content")
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
            if kind == "task" and props["status"] in {"done", "resolved"}:
                # Completion depends on incoming conflicts as well as direct
                # links, so it still requires the complete consistent snapshot.
                records = {n.id: n.properties for n in store.find_nodes_by_types([RECORD_TYPE], clone=False)}
                records[item_id] = props
                execution = self._execution({**props, "id": item_id, "status": "open"}, records)
                if execution["execution_state"] in {"blocked", "needs_review"}:
                    raise ValueError("Cannot complete task with waiting, obsolete or conflicting dependencies")
            changed = {name: value for name, value in props.items() if name not in {"history", "revision", "agent_id", "session_id", "session_title", "activity_id"}}
            previous = {name: value for name, value in old.items() if name in changed}
            # Claiming active work transfers responsibility even when its text/status
            # are unchanged. Ordinary repeated records retain their idempotence.
            ownership_changed = record_id is not None and kind == "task" and status == "in_progress" and (
                old.get("agent_id") != agent_id or old.get("session_id") != session_id)
            if node and changed == previous and not ownership_changed:
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

    def find(self, record_id: str) -> dict[str, Any] | None:
        """Find one shared identity without scanning work or creating an absent store."""
        if self.path.exists() and self.path.stat().st_size:
            with self._open(read_only=True) as store:
                node = store.get_node(record_id)
                if node is not None and node.type == RECORD_TYPE:
                    return self._payload(node)
        return None

    def show(self, record_id: str) -> dict[str, Any]:
        """Retrieve a full record and its bounded revision evidence."""
        result = self.find(record_id)
        if result is not None:
            return result
        raise ValueError(f"Work record not found: {record_id}")

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
            if dependency is not None and dependency["status"] not in TERMINAL and conflicts_by_id.get(target):
                obsolete.append(target)
        conflicts = conflicts_by_id.get(item["id"], set())
        state = item["status"]
        if state not in TERMINAL:
            state = "needs_review" if obsolete or conflicts else ("blocked" if waiting or state == "blocked" else ("ready" if state == "open" else state))
        return {"execution_state": state, "waiting_on": waiting, "obsolete_dependencies": obsolete, "conflicts": sorted(conflicts)}

    def context(self, query: str = "", *, files: list[str] | None = None, workstream: str | None = None,
                record_id: str | None = None, limit: int = 8, include_history: bool = False) -> dict[str, Any]:
        """Prioritize current file contracts and causal prerequisites within bounded recovery."""
        if not 1 <= limit <= 40:
            raise ValueError("Context limit must be between 1 and 40")
        all_records = self.records()
        records = {item["id"]: item for item in all_records}
        conflicts_by_id = self._conflict_index(records)
        incoming: dict[str, dict[str, set[str]]] = {relation: defaultdict(set) for relation in RELATIONS}
        for item in all_records:
            for relation in RELATIONS:
                targets = [item[relation]] if relation == "parent" and item[relation] else ([] if relation == "parent" else item[relation])
                for target in targets:
                    incoming[relation][target].add(item["id"])
        if record_id and record_id not in records:
            raise ValueError(f"Work record not found: {record_id}")
        anchor = records.get(record_id) if include_history else self._current_replacement(records, record_id)
        anchor_id = anchor["id"] if anchor is not None else record_id
        tokens = _tokens(query)
        paths = _paths(files or [])
        # A known identity recovers linked work, not lexical matches in the backlog.
        eligible = [item for item in all_records
                    if (not record_id or item["id"] == anchor_id)
                    and (workstream is None or item["workstream"] == workstream)
                    and (include_history or item["status"] not in OBSOLETE_STATUSES)]
        words = {item["id"]: _tokens(" ".join([item["content"], item["rationale"], item["next_action"], item["workstream"], *item["files"]])) for item in eligible} if tokens and not record_id else {}
        frequency = Counter(term for terms in words.values() for term in terms)
        denominator = sum(1 / frequency.get(term, 1) for term in tokens) or 1
        now = parse_dt(utcnow_iso())
        scores: dict[str, float] = {}
        reasons: dict[str, list[str]] = {}
        for item in eligible:
            overlap = sum(1 / frequency[term] for term in tokens & words[item["id"]]) / denominator if words else 0.0
            path_match = any(path == evidence or path.startswith(evidence + "/") or evidence.startswith(path + "/") for path in paths for evidence in item["files"])
            anchors = []
            if overlap:
                anchors.append("text")
            if path_match:
                anchors.append("file")
            if anchor_id == item["id"]:
                anchors.append("task")
                if anchor_id != record_id:
                    anchors.append(f"replacement:{record_id}")
            if not tokens and not paths and not record_id:
                anchors.append("overview")
            if not anchors:
                continue
            age = max(0, (now.date() - parse_dt(item["updated_at"]).date()).days)
            priority = 0.18 if item["kind"] in {"decision", "constraint", "failure", "goal", "question"} else 0
            score = overlap * 2 + path_match * 3 + (anchor_id == item["id"]) * 4 + priority + item["importance"] * 0.05 + 0.1 / (1 + age)
            if item["status"] not in TERMINAL:
                # A current file-scoped obligation outranks even an exact lexical
                # match in a completed summary for that file. History stays available.
                score += 3 if path_match else 0.25
            scores[item["id"]], reasons[item["id"]] = score, anchors
        frontier = sorted(scores, key=lambda item_id: (-scores[item_id], item_id))[:4]
        seed_ids = set(frontier)
        expanded = set(frontier)
        omitted_ids: set[str] = set()
        expansion_truncated = False
        # Inspect dependents/children of direct matches, not every shared prerequisite
        # reached along the way: otherwise one contract broadcasts unrelated lanes.
        for depth in range(2):
            next_frontier = []
            for item_id in frontier:
                item = records[item_id]
                # Preconditions and conflicts must reach the handoff before the
                # potentially numerous consumers and historical summaries.
                related = {target: 0.9 for target in item["depends_on"] + item["contradicts"]}
                related.update({target: 0.9 for target in incoming["contradicts"].get(item_id, ())})
                related.update({target: 0.9 for target in incoming["supersedes"].get(item_id, ())})
                if item["parent"]:
                    related.setdefault(item["parent"], 0.75)
                for target in item["summarizes"]:
                    related.setdefault(target, 0.65)
                if item_id in seed_ids:
                    dependency_anchors = {item_id}
                    if item_id == anchor_id and record_id:
                        dependency_anchors.add(record_id)
                    for dependency_anchor in dependency_anchors:
                        for target in incoming["depends_on"].get(dependency_anchor, ()):
                            related.setdefault(target, 0.45)
                for target in incoming["summarizes"].get(item_id, ()):
                    related.setdefault(target, 0.35)
                if item_id in seed_ids and item["kind"] == "task":
                    for target in incoming["parent"].get(item_id, ()):
                        related.setdefault(target, 0.65)
                if include_history:
                    for target in item["supersedes"]:
                        related.setdefault(target, 0.65)
                for target in sorted(related, key=lambda target: (-related[target], target)):
                    relation_score = scores[item_id] * related[target]
                    candidate = records.get(target) if include_history else self._current_replacement(records, target)
                    if candidate is None:
                        continue
                    relation_reason = [f"related:{item_id}"]
                    if candidate["id"] != target:
                        relation_reason.append(f"replacement:{target}")
                    target = candidate["id"]
                    # Reserve half the traversal for second-hop assumptions and
                    # conflicts, rather than exhausting it on direct fanout.
                    if target not in expanded and len(expanded) >= 40 * (depth + 1):
                        expansion_truncated = True
                        if target not in scores:
                            omitted_ids.add(target)
                        continue
                    if target not in expanded:
                        expanded.add(target)
                        next_frontier.append(target)
                    if target not in scores:
                        scores[target], reasons[target] = relation_score, relation_reason
                    else:
                        scores[target] = max(scores[target], relation_score)
                        reasons[target].extend(reason for reason in relation_reason if reason not in reasons[target])
            if len(next_frontier) > 12:
                expansion_truncated = True
            frontier = sorted(next_frontier, key=lambda target: (-scores[target], target))[:12]
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
        total_matches = len(ranked) + len(omitted_ids - scores.keys())
        return {"format": "reql-work-context-v1", "revision": revision, "query": query, "records": selected,
                "total_matches": total_matches, "omitted": max(0, total_matches - limit),
                "expansion_truncated": expansion_truncated}

    @staticmethod
    def _current_replacement(records: dict[str, dict[str, Any]], record_id: str | None) -> dict[str, Any] | None:
        """Follow explicit replacements without reviving obsolete work or changing links."""
        visited: set[str] = set()
        while record_id and record_id not in visited:
            visited.add(record_id)
            item = records.get(record_id)
            if item is None or item["status"] not in OBSOLETE_STATUSES:
                return item
            record_id = item.get("replaced_by")
        return None

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


def render_work_context(payload: dict[str, Any], *, max_chars: int = 6000, record_id: str | None = None) -> str:
    """Render one independent response with bounded evidence and explicit omissions."""
    return _WorkContextRenderer(payload.get("records", [])).render(payload, max_chars=max_chars, record_id=record_id)


def render_work_overview(payload: dict[str, Any], *, max_chars_per_section: int = 1200) -> str:
    """Render section membership while sharing only evidence already emitted in this overview."""
    renderer = _WorkContextRenderer(item for items in payload["sections"].values() for item in items)
    sections = []
    for label, items in payload["sections"].items():
        title = label.title()
        count = payload["counts"][label]
        text = renderer.render({"records": items, "omitted": max(0, count - len(items))},
                               max_chars=max_chars_per_section, section=title, heading=False)
        sections.append(f"### {title} ({count})\n{text}")
    return "\n\n".join(sections)


class _WorkContextRenderer:
    """Own references to emitted text for one context response or composite overview."""

    def __init__(self, records: Iterable[dict[str, Any]]) -> None:
        """Start independent reference state without retaining prior responses."""
        self.texts: dict[str, tuple[str, str, int]] = {}
        self.records: dict[tuple[str, int], tuple[dict[str, Any], str, bool]] = {}
        owners: dict[str, set[tuple[str, int]]] = defaultdict(set)
        for item in records:
            owners[item["agent_id"]].add((item["id"], item["revision"]))
        self.owner_aliases: dict[str, str] = {}
        alias_number = 1
        for owner, identities in owners.items():
            alias = f"owner{alias_number}"
            while alias in owners:
                alias_number += 1
                alias = f"owner{alias_number}"
            # Count the declaration once and every reference, including the first.
            if len(identities) * len(owner) > len(owner) + 1 + len(identities) * len(alias):
                self.owner_aliases[owner] = alias
                alias_number += 1
        self.declared_owners: set[str] = set()

    def render(self, payload: dict[str, Any], *, max_chars: int = 6000, record_id: str | None = None,
               section: str | None = None, heading: bool = True) -> str:
        """Render full focused evidence when it fits, with explicit omissions and drill-down."""
        if type(max_chars) is not int or max_chars < 1:
            raise ValueError("Work context character budget must be a positive integer")
        rows = ["## Engineering context"] if heading else []
        records = list(payload.get("records", []))

        def focused(item: dict[str, Any]) -> bool:
            """Keep the requested outcome or its current replacement ahead of neighbors."""
            return bool(record_id and (item["id"] == record_id or f"replacement:{record_id}" in item.get("reasons", [])))

        records.sort(key=lambda item: not focused(item))
        detailed = bool(record_id)
        used = len(rows[0]) if rows else 0
        rendered = 0
        # Reserve one omission message before adding prose so it cannot be cut off.
        footer_budget = 200 if payload.get("expansion_truncated") else 100
        for item in records:
            remaining = max_chars - used - footer_budget - 1
            owner = item["agent_id"]
            alias = self.owner_aliases.get(owner)
            attribution = (alias if owner in self.declared_owners else f"{alias}={owner}") if alias else owner
            header = f"- [{item['kind']}/{item['execution_state']}] {item['id']} r{item['revision']} ({attribution})"
            identity = (item["id"], item["revision"])
            previous = self.records.get(identity)
            if previous is not None and previous[0] == item:
                line = f"- [{item['kind']}/{item['execution_state']}] {item['id']} r{item['revision']}; see {previous[1]} above"
                if previous[2]:
                    line += " (truncated there)"
                if len(line) > remaining:
                    break
                rows.append(line)
                used += len(line) + 1
                rendered += 1
                continue
            core = header
            links = [("waiting", item.get("waiting_on", [])),
                     ("reconcile", item.get("obsolete_dependencies", []) + item.get("conflicts", [])),
                     ("replaces", item["supersedes"])]
            if detailed:
                links.extend((("parent", [item["parent"]] if item.get("parent") else []),
                              ("depends", item.get("depends_on", []))))
            if item.get("replaced_by"):
                links.append(("replaced_by", [item["replaced_by"]]))
            replacements = [reason.removeprefix("replacement:") for reason in item.get("reasons", [])
                            if reason.startswith("replacement:")]
            if replacements:
                links.append(("replacement for", replacements))
            roles: dict[str, list[str]] = {}
            for label, targets in links:
                for target in dict.fromkeys(targets):
                    roles.setdefault(target, []).append(label)
            groups: dict[str, list[str]] = {}
            for target, labels in roles.items():
                groups.setdefault("+".join(labels), []).append(target)
            for label, targets in groups.items():
                core += f"; {label}: " + ", ".join(targets)

            truncated = False
            details = []
            available = dict(self.texts)
            location = f"{section} item {rendered + 1}" if section else f"item {rendered + 1}"
            fields = (("", "content", item["content"], 360), ("next", "next", item["next_action"], 200),
                      ("Why", "rationale", item["rationale"], 240),
                      ("Replacement reason", "replacement reason", item.get("replacement_reason", ""), 240))
            for label, field, value, cap in fields:
                if not value:
                    continue
                visible = len(value) if detailed else min(len(value), cap)
                incomplete = visible < len(value)
                display = value[:visible] + ("... [truncated]" if incomplete else "")
                reference = available.get(value)
                if reference is not None and reference[2] >= visible:
                    source_location, source_field, coverage = reference
                    pointer = f"same as {source_field} above" if source_location == location else f"same as {source_location} {source_field} above"
                    if coverage < len(value):
                        pointer += " [truncated]"
                    if len(pointer) < len(display):
                        display = pointer
                        incomplete = coverage < len(value)
                if reference is None or reference[2] < visible:
                    available[value] = (location, field, visible)
                truncated = truncated or incomplete
                details.append(f"{label}: {display}" if label else display)
            files = item["files"] if detailed else item["files"][:4]
            if files:
                details.append("files: " + ", ".join(files))
            if len(files) < len(item["files"]):
                details.append(f"{len(item['files']) - len(files)} files omitted [truncated]")
                truncated = True
            detail = "; ".join(details)
            hint = f"... [truncated; reql agent show {item['id']} --json]"
            suffix = " " + hint if truncated else ""
            if len(core) + len(hint) + 2 > remaining:
                # Never present a partially cut dependency/conflict identifier as complete.
                line = header + "; coordination links omitted " + hint
                if len(line) > remaining:
                    break
            elif len(core) + len(detail) + len(suffix) + 2 > remaining:
                line = core + "; " + detail[:remaining - len(core) - len(hint) - 2] + hint
            else:
                line = core + "; " + detail + suffix
                # References become valid only after every referenced field has
                # actually been emitted. Budget-cut rows cannot seed later pointers.
                self.texts = available
                if section is not None:
                    self.records[identity] = (item, section, truncated)
            rows.append(line)
            # Only emitted headers can establish attribution for later rows.
            if alias:
                self.declared_owners.add(owner)
            used += len(line) + 1
            rendered += 1
        omitted = len(records) - rendered + int(payload.get("omitted", 0))
        if omitted:
            rows.append(f"- {omitted} matching record(s) omitted; refine context or increase --limit/budget.")
        elif not records:
            rows.append("No matching engineering records.")
        if payload.get("expansion_truncated"):
            rows.append("Causal expansion limited; inspect linked work with reql project context --task ID.")
        text = "\n".join(rows)
        return text if len(text) <= max_chars else "Work context truncated; increase budget."[:max_chars]
