from __future__ import annotations

import unittest

from memory.code_analysis import calls_by_caller, imported_modules
from memory.code_analysis.models import CodeCall, CodeImport


class CodeAnalysisGraphUtilsTests(unittest.TestCase):
    def test_imported_modules_prefers_module_and_falls_back_to_name(self) -> None:
        imports = [
            CodeImport(id="i1", artifact_id="a", module="os.path", name="join", alias=None),
            CodeImport(id="i2", artifact_id="a", module=None, name="typing", alias=None),
            CodeImport(id="i3", artifact_id="a", module="", name="collections", alias=None),
            CodeImport(id="i4", artifact_id="a", module=None, name=None, alias=None),
        ]

        self.assertEqual(imported_modules(imports), {"os.path", "typing", "collections"})

    def test_calls_by_caller_groups_calls_preserving_call_order(self) -> None:
        first = CodeCall(id="c1", artifact_id="a", caller="main", target="load")
        second = CodeCall(id="c2", artifact_id="a", caller=None, target="bootstrap")
        third = CodeCall(id="c3", artifact_id="a", caller="main", target="save")

        grouped = calls_by_caller([first, second, third])

        self.assertEqual(grouped, {"main": [first, third], None: [second]})


if __name__ == "__main__":
    unittest.main()
