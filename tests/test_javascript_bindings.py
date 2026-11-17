"""Function assignments must retain their callable binding and ownership."""
from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from reql import MemoryGraph


class JavaScriptBindingTests(unittest.TestCase):
    """Exercise the shared JavaScript/TypeScript walker through persisted graphs."""

    def test_assigned_functions_keep_bindings_calls_and_incremental_provenance(self) -> None:
        """Static assignments are symbols; computed receivers do not establish bindings."""
        source = ("function save(value) { return value; }\n"
                  "const local = (value) => save(value);\n"
                  "app.handle = function handle(req) { return save(req); };\n"
                  "exports.settle = async (value) => save(value);\n"
                  "items[index] = function computed(value) { return value; };\n"
                  "factory().handle = function dynamic(value) { return value; };\n")
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            root.mkdir()
            for suffix in ("js", "ts", "tsx"):
                (root / f"billing.{suffix}").write_text(source, encoding="utf-8")
            graph = MemoryGraph.open(Path(temporary) / "memory.reql")
            try:
                compiled = graph.compile_project(root)
                self.assertFalse(compiled.run.errors)
                for suffix in ("js", "ts", "tsx"):
                    nodes = {n.properties["name"]: n for n in graph.store.find_nodes_by_types(["Function"], clone=False)
                             if n.properties["relative_path"] == f"billing.{suffix}"}
                    self.assertEqual(set(nodes), {"save", "local", "app.handle", "exports.settle"})
                    self.assertEqual(nodes["app.handle"].properties["args"], ["req"])
                    self.assertEqual(nodes["exports.settle"].properties["kind"], "async_function")
                    for name in ("local", "app.handle", "exports.settle"):
                        self.assertTrue(graph.store.get_edges(from_id=nodes[name].id, to_id=nodes["save"].id,
                                                              type_="CALLS", limit=1), (suffix, name))
                self.assertEqual(graph.compile_project(root).run.files_changed, 0)
            finally:
                graph.close()


if __name__ == "__main__":
    unittest.main()
