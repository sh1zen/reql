"""Keep trusted C declarations and calls without guessing through broken syntax."""
from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from reql import MemoryGraph


class CSyntaxRecoveryTests(unittest.TestCase):
    """Exercise recovered C syntax through compilation, persistence and source context."""

    def test_export_macros_and_conditional_linkage_preserve_definitions_and_calls(self) -> None:
        """Unknown storage macros do not erase intact function signatures and bodies."""
        source = ("#ifdef __cplusplus\nextern \"C\" {\n#endif\n"
                  "#ifdef LOCAL_BUILD\n#define PUBLIC static\n#else\n#define PUBLIC extern\n#endif\n"
                  "typedef struct Token { int value; } Token;\n"
                  "static int helper(Token *tokens) { return tokens->value; }\n"
                  "PUBLIC int parse(Token *tokens, const unsigned int count) {\n"
                  "    return count ? helper(tokens) : 0;\n}\n"
                  "PUBLIC void init(Token *token) { token->value = 0; }\n"
                  "#ifdef __cplusplus\n}\n#endif\n")
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            root.mkdir()
            (root / "parser.h").write_text(source, encoding="utf-8")
            path = Path(temporary) / "memory.reql"
            graph = MemoryGraph.open(path)
            try:
                compiled = graph.compile_project(root)
                self.assertFalse(compiled.run.errors)
                functions = {n.properties["name"]: n for n in graph.store.find_nodes_by_types(["Function"])}
                self.assertEqual(set(functions), {"helper", "parse", "init"})
                parsed = functions["parse"]
                self.assertEqual(parsed.properties["args"], ["tokens", "count"])
                self.assertIsNone(parsed.properties["returns"])
                self.assertTrue(parsed.properties["metadata"]["syntax_recovered"])
                self.assertTrue(graph.store.get_edges(from_id=parsed.id, to_id=functions["helper"].id,
                                                      type_="CALLS", limit=1))
                module = graph.store.find_nodes_by_types(["Module"])[0]
                self.assertEqual(module.properties["metadata"]["parser_status"], "partial")
                self.assertEqual(graph.compile_project(root).run.files_changed, 0)
                context = graph.query_context_payload("parser.h", scopes=("code",), include_source=True)
                snippets = context["payload"]["snippets"]
                self.assertTrue(any("PUBLIC int parse" in item.get("text", "") for item in snippets))
            finally:
                graph.close()
            reopened = MemoryGraph.open(path, read_only=True)
            try:
                self.assertEqual({n.properties["name"] for n in reopened.store.find_nodes_by_types(["Function"])},
                                 {"helper", "parse", "init"})
            finally:
                reopened.close()

    def test_broken_declarators_and_bodies_do_not_leak_functions_or_calls(self) -> None:
        """Recovery must exclude damaged definitions, including their nested evidence."""
        # An unbalanced declarator can swallow later text into one ERROR node;
        # that text has no trustworthy function boundary to recover.
        cases = (("int broken( { return helper(); }\n", {"helper"}),
                 ("int broken(int , int value,,) { return helper(); }\n", {"helper", "intact"}),
                 ("int broken(void) { return helper( ; }\n", {"helper", "intact"}))
        for broken, expected in cases:
            with self.subTest(source=broken), TemporaryDirectory() as temporary:
                root = Path(temporary) / "project"
                root.mkdir()
                (root / "broken.c").write_text(
                    "int helper(void) { return 1; }\n" + broken + "int intact(void) { return 2; }\n", encoding="utf-8")
                graph = MemoryGraph.open(Path(temporary) / "memory.reql")
                try:
                    self.assertFalse(graph.compile_project(root).run.errors)
                    self.assertEqual({n.properties["name"] for n in graph.store.find_nodes_by_types(["Function"])},
                                     expected)
                    self.assertFalse(graph.store.get_edges(type_="CALLS", limit=10))
                finally:
                    graph.close()
