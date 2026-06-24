"""Dockerfile Tree-sitter extraction."""
from __future__ import annotations

from .generic import COMMON_CONTROL_CALLS, COMMON_IMPORTS, AstProfile, GenericProfileTreeSitterExtractor


class DockerfileTreeSitterExtractor(GenericProfileTreeSitterExtractor):
    language_key = "dockerfile"
    profile = AstProfile(
        name="dockerfile",
        languages=frozenset({"dockerfile"}),
        class_nodes=frozenset({"stage"}),
        variable_nodes=frozenset({"env_instruction", "label_instruction"}),
        import_nodes=COMMON_IMPORTS | frozenset({"copy_instruction", "from_instruction"}),
        call_nodes=frozenset({"run_instruction"}),
        assignment_nodes=frozenset({"env_instruction", "label_instruction"}),
        builtin_call_names=COMMON_CONTROL_CALLS | frozenset({"COPY", "ENTRYPOINT", "FROM", "RUN"}),
    )
