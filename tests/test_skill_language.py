"""Check measurable STE writing rules without claiming full dictionary validation."""
from __future__ import annotations

from pathlib import Path
import re
import unittest

from agents.install import _skill_generator, PLATFORMS_CONFIG


def prose_blocks(text: str) -> list[str]:
    """Extract prose while protecting literal commands, paths, and YAML controls."""
    text = re.sub(r"(?ms)^```.*?^```[^\n]*", "", text)
    text = re.sub(r"`[^`\n]+`", "TERM", text)
    text = re.sub(r"(?m)^#+ .*|^<!--.*|^---\s*$", "", text)
    lines = []
    for line in text.splitlines():
        if re.match(r"^\s*[\w-]+:", line):
            key, value = line.split(":", 1)
            if key.strip() not in {"description", "short_description", "default_prompt"}:
                if key.strip() in {"name", "policy", "interface", "display_name", "disable-model-invocation",
                                   "allow_implicit_invocation", "alwaysApply", "applyTo"}:
                    continue
            else:
                line = value.strip().strip('"')
        lines.append(line)
    text = "\n".join(lines)
    blocks = re.split(r"\n\s*\n|\n(?=\s*(?:\d+\.|[-*]) )", text)
    return [re.sub(r"^\s*(?:\d+\.|[-*])\s+", "", block).strip()
            for block in blocks if block.strip()]


class SkillLanguageTests(unittest.TestCase):
    """Keep generated prose short while retaining domain names and examples."""

    def test_generated_prose_has_short_sentences_and_paragraphs(self) -> None:
        generator = _skill_generator()
        for platform in PLATFORMS_CONFIG:
            options = dict(platform_name=platform, project=True, command_name="reql",
                           command_path=Path("reql.cmd"), fallback_command="python cli.py")
            documents = list(generator.skill_markdowns(**options))
            documents.extend((f"{skill}/{path}", content)
                             for skill, path, content in generator.skill_resources(**options))
            documents.append(("instructions", generator.instruction_section(
                **options, supported_clients=platform,
                section_start="<!-- REQL:START -->", section_end="<!-- REQL:END -->")))
            rule_options = {key: value for key, value in options.items() if key not in {"platform_name", "project"}}
            rule_options.update(section_start="<!-- REQL:START -->", section_end="<!-- REQL:END -->")
            documents.extend((name, render(**rule_options)) for name, render in (
                ("cursor-rule", generator.cursor_rule), ("copilot-rule", generator.vscode_copilot_rule)))
            documents.append(("markdown-rule", generator.markdown_rule(platform, **rule_options)))
            for path, text in documents:
                for block in prose_blocks(text):
                    sentences = [part for part in re.split(r"(?<=[.!?])\s+", block) if part.strip()]
                    with self.subTest(platform=platform, path=path, block=block):
                        self.assertLessEqual(len(sentences), 6)
                        self.assertNotIn(";", block, "Separate instructions with sentences, not semicolons")
                        self.assertNotRegex(block, r"(?i)\b(?:can't|won't|don't|doesn't|isn't|aren't|it's|you're|we're)\b")
                    for sentence in sentences:
                        # Check all prose against the stricter procedural limit.
                        # One placeholder protects each literal code expression.
                        words = sentence.split()
                        with self.subTest(platform=platform, path=path, sentence=sentence):
                            self.assertLessEqual(len(words), 20)

    def test_prose_checks_do_not_count_code_or_control_fields(self) -> None:
        text = '''---
name: a-technical-skill
description: Use the saved graph.
---
# Title
Use `python -m package --path file.py` first.

Caution: Read the current source.

```bash
this command has many technical arguments; keep them unchanged
```
policy:
  allow_implicit_invocation: false
'''
        self.assertEqual(prose_blocks(text), ["Use the saved graph.", "Use TERM first.", "Caution: Read the current source."])


if __name__ == "__main__":
    unittest.main()
