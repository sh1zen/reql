"""Business-oriented repository explanation models."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class CodeEvidence:
    """One source-backed fact used by the business abstraction."""

    node_id: str
    node_type: str
    label: str
    path: str
    line_start: int | None = None
    line_end: int | None = None
    reason: str = ""
    score: float | None = None

    @property
    def location(self) -> str:
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
class BusinessCapability:
    """A cohesive repository responsibility inferred from code ownership."""

    id: str
    name: str
    layer: str
    purpose: str
    responsibilities: list[str] = field(default_factory=list)
    primary_paths: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    owners: list[CodeEvidence] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)
    score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "layer": self.layer,
            "purpose": self.purpose,
            "responsibilities": list(self.responsibilities),
            "primary_paths": list(self.primary_paths),
            "dependencies": list(self.dependencies),
            "owners": [item.to_dict() for item in self.owners],
            "tests": list(self.tests),
            "score": round(self.score, 4),
        }


@dataclass(slots=True)
class ArchitectureLayer:
    """An architectural role containing one or more business capabilities."""

    name: str
    purpose: str
    capability_ids: list[str] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class WorkflowParticipant:
    """One explicit relation from a semantic workflow to implementing code."""

    relation: str
    role: str
    target: CodeEvidence

    def to_dict(self) -> dict[str, Any]:
        return {
            "relation": self.relation,
            "role": self.role,
            "target": self.target.to_dict(),
        }


@dataclass(slots=True)
class BusinessWorkflow:
    """A semantic use-case projection supported by multiple graph facts."""

    id: str
    name: str
    intent: str
    trigger: str
    inputs: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    invariants: list[str] = field(default_factory=list)
    participants: list[WorkflowParticipant] = field(default_factory=list)
    evidence: list[CodeEvidence] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "intent": self.intent,
            "trigger": self.trigger,
            "inputs": list(self.inputs),
            "outputs": list(self.outputs),
            "invariants": list(self.invariants),
            "participants": [participant.to_dict() for participant in self.participants],
            "evidence": [item.to_dict() for item in self.evidence],
        }


@dataclass(slots=True)
class ChangeGuide:
    """Evidence-backed starting points for a requested repository change."""

    focus: str | None
    rationale: str
    capability_ids: list[str] = field(default_factory=list)
    start_here: list[CodeEvidence] = field(default_factory=list)
    verify_with: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "focus": self.focus,
            "rationale": self.rationale,
            "capability_ids": list(self.capability_ids),
            "start_here": [item.to_dict() for item in self.start_here],
            "verify_with": list(self.verify_with),
        }


@dataclass(slots=True)
class RepositoryExplanation:
    """Read-only business projection of a compiled repository graph."""

    project: dict[str, Any]
    summary: str
    basis: dict[str, Any]
    layers: list[ArchitectureLayer] = field(default_factory=list)
    capabilities: list[BusinessCapability] = field(default_factory=list)
    workflows: list[BusinessWorkflow] = field(default_factory=list)
    change_guide: ChangeGuide | None = None
    schema_version: int = 2

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "project": dict(self.project),
            "summary": self.summary,
            "basis": dict(self.basis),
            "layers": [layer.to_dict() for layer in self.layers],
            "capabilities": [capability.to_dict() for capability in self.capabilities],
            "workflows": [workflow.to_dict() for workflow in self.workflows],
            "change_guide": self.change_guide.to_dict() if self.change_guide else None,
        }

    def to_markdown(self) -> str:
        """Render a compact human-readable explanation with source locations."""

        project_name = str(self.project.get("name") or self.project.get("label") or "Repository")
        lines = [f"# Repository explanation: {project_name}", "", self.summary, ""]
        if self.layers:
            lines.extend(["## Architecture", ""])
            capability_names = {item.id: item.name for item in self.capabilities}
            for layer in self.layers:
                names = [capability_names[item] for item in layer.capability_ids if item in capability_names]
                lines.append(f"- **{layer.name.title()}**: {layer.purpose}")
                if names:
                    lines.append(f"  Capabilities: {', '.join(names)}")
            lines.append("")

        if self.capabilities:
            lines.extend(["## Business capabilities", ""])
            for capability in self.capabilities:
                lines.append(f"### {capability.name}")
                lines.append("")
                lines.append(capability.purpose)
                lines.append("")
                lines.append(f"- Layer: {capability.layer}")
                if capability.responsibilities:
                    lines.append(f"- Responsibilities: {', '.join(capability.responsibilities)}")
                if capability.primary_paths:
                    lines.append(f"- Primary paths: {', '.join(capability.primary_paths)}")
                if capability.dependencies:
                    lines.append(f"- Depends on: {', '.join(capability.dependencies)}")
                if capability.owners:
                    owners = ", ".join(f"{item.label} ({item.location})" for item in capability.owners)
                    lines.append(f"- Code owners: {owners}")
                if capability.tests:
                    lines.append(f"- Verification: {', '.join(capability.tests)}")
                lines.append("")

        if self.workflows:
            lines.extend(["## Semantic workflows", ""])
            for workflow in self.workflows:
                lines.extend([f"### {workflow.name}", "", workflow.intent, ""])
                lines.append(f"- Trigger: {workflow.trigger}")
                if workflow.inputs:
                    lines.append(f"- Inputs: {', '.join(workflow.inputs)}")
                if workflow.outputs:
                    lines.append(f"- Outputs: {', '.join(workflow.outputs)}")
                if workflow.invariants:
                    lines.append(f"- Invariants: {'; '.join(workflow.invariants)}")
                if workflow.participants:
                    participants = ", ".join(
                        f"{item.target.label} ({item.target.location}; {item.role})"
                        for item in workflow.participants
                    )
                    lines.append(f"- Implemented by: {participants}")
                if workflow.evidence:
                    evidence = ", ".join(
                        f"{item.label} ({item.location}; {item.reason})"
                        for item in workflow.evidence
                    )
                    lines.append(f"- Evidence: {evidence}")
                lines.append("")

        if self.change_guide:
            lines.extend(["## Change guidance", "", self.change_guide.rationale, ""])
            for item in self.change_guide.start_here:
                reason = f" - {item.reason}" if item.reason else ""
                lines.append(f"- Start at `{item.location}` ({item.label}){reason}")
            if self.change_guide.verify_with:
                lines.append(f"- Verify with: {', '.join(self.change_guide.verify_with)}")
            lines.append("")
        return "\n".join(lines).rstrip()
