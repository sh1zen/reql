"""Typed project-pipeline projection models."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class PipelineSymbol:
    """One source symbol contributing to a pipeline component."""

    node_id: str
    node_type: str
    label: str
    path: str
    line_start: int | None = None
    line_end: int | None = None
    private: bool = False
    entrypoint: bool = False

    @property
    def location(self) -> str:
        if not self.path:
            return ""
        if self.line_start is None:
            return self.path
        if self.line_end is None or self.line_end == self.line_start:
            return f"{self.path}:{self.line_start}"
        return f"{self.path}:{self.line_start}-{self.line_end}"

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["location"] = self.location
        return payload


@dataclass(slots=True)
class PipelineComponent:
    """A high-level project component shared by one or more workflows."""

    id: str
    key: str
    name: str
    layer: str
    paths: list[str] = field(default_factory=list)
    symbols: list[PipelineSymbol] = field(default_factory=list)
    workflow_ids: list[str] = field(default_factory=list)
    cyclic: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "key": self.key,
            "name": self.name,
            "layer": self.layer,
            "paths": list(self.paths),
            "symbols": [symbol.to_dict() for symbol in self.symbols],
            "workflow_ids": list(self.workflow_ids),
            "cyclic": self.cyclic,
        }


@dataclass(frozen=True, slots=True)
class PipelineEdge:
    """An aggregated directed relation between pipeline components."""

    id: str
    from_component_id: str
    to_component_id: str
    relation_types: tuple[str, ...]
    workflow_ids: tuple[str, ...]
    cyclic: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "from_component_id": self.from_component_id,
            "to_component_id": self.to_component_id,
            "relation_types": list(self.relation_types),
            "workflow_ids": list(self.workflow_ids),
            "cyclic": self.cyclic,
        }


@dataclass(frozen=True, slots=True)
class PipelineOutcome:
    """An observed effect or terminal point for one workflow."""

    id: str
    workflow_id: str
    component_id: str
    kind: str
    label: str
    symbol_id: str
    observed_terminal: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class PipelineWorkflow:
    """One detected project flow from an entrypoint through shared components."""

    id: str
    name: str
    trigger: PipelineSymbol
    trigger_reason: str
    inferred: bool
    trigger_component_id: str
    component_ids: list[str] = field(default_factory=list)
    outcome_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "trigger": self.trigger.to_dict(),
            "trigger_reason": self.trigger_reason,
            "inferred": self.inferred,
            "trigger_component_id": self.trigger_component_id,
            "component_ids": list(self.component_ids),
            "outcome_ids": list(self.outcome_ids),
        }


@dataclass(slots=True)
class ProjectPipeline:
    """Read-only, deterministic high-level flow projection for a project."""

    project: dict[str, Any]
    summary: str
    basis: dict[str, Any]
    workflows: list[PipelineWorkflow] = field(default_factory=list)
    components: list[PipelineComponent] = field(default_factory=list)
    edges: list[PipelineEdge] = field(default_factory=list)
    outcomes: list[PipelineOutcome] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    schema_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "project": dict(self.project),
            "summary": self.summary,
            "basis": dict(self.basis),
            "workflows": [workflow.to_dict() for workflow in self.workflows],
            "components": [component.to_dict() for component in self.components],
            "edges": [edge.to_dict() for edge in self.edges],
            "outcomes": [outcome.to_dict() for outcome in self.outcomes],
            "warnings": list(self.warnings),
        }
