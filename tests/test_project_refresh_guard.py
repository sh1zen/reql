"""Verify the watcher-aware compile path through normal CLI parsing."""
from __future__ import annotations

from contextlib import redirect_stdout,redirect_stderr
from io import StringIO
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from memory.cli import main


class ProjectRefreshGuardTests(unittest.TestCase):
    """Keep watcher ownership and normal incremental compilation unchanged."""

    def setUp(self) -> None:
        scratch=Path('.tmp')/'regression-tests';scratch.mkdir(parents=True,exist_ok=True)
        self.temp=TemporaryDirectory(dir=scratch);self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        self.previous=Path.cwd();os.chdir(self.root);self.addCleanup(os.chdir,self.previous)

    def invoke(self,*args: str) -> tuple[int,str,str]:
        """Capture the public CLI's status and diagnostics."""
        stdout,stderr=StringIO(),StringIO()
        with redirect_stdout(stdout),redirect_stderr(stderr):
            code=main(list(args))
        return code,stdout.getvalue(),stderr.getvalue()

    def test_running_watcher_never_opens_or_creates_graph(self) -> None:
        with patch('memory.cli._project_watch_status',return_value={'status':'running'}), \
             patch('api.memory_graph.MemoryGraph.open',side_effect=AssertionError('opened graph')):
            code,out,error=self.invoke('project','compile','--if-stopped')
        self.assertEqual(code,0,error)
        self.assertIn('Watcher running',out)
        self.assertFalse((self.root/'.reql/memory.reql').exists())

    def test_uncertain_watcher_reports_error_without_graph_access(self) -> None:
        for state in ('stale','unknown'):
            with self.subTest(state=state), \
                 patch('memory.cli._project_watch_status',return_value={'status':state}), \
                 patch('api.memory_graph.MemoryGraph.open',side_effect=AssertionError('opened graph')):
                code,out,error=self.invoke('project','compile','--if-stopped')
            self.assertEqual(code,2)
            self.assertIn('uncertain',error)
            self.assertFalse((self.root/'.reql/memory.reql').exists())

    def test_stopped_watcher_uses_normal_incremental_compiler(self) -> None:
        (self.root/'app.py').write_text('def emit_event(value):\n    return value + 1\n',encoding='utf-8')
        first=self.invoke('project','compile','--if-stopped')
        second=self.invoke('project','compile','--if-stopped')
        self.assertEqual(first[0],0,first[2]);self.assertIn('Status: completed',first[1])
        self.assertEqual(second[0],0,second[2]);self.assertIn('Changed: 0',second[1])

    def test_guard_cannot_start_monitoring(self) -> None:
        with patch('api.memory_graph.MemoryGraph.open',side_effect=AssertionError('opened graph')):
            code,out,error=self.invoke('project','compile','--if-stopped','--watch')
        self.assertEqual(code,2)
        self.assertIn('cannot be combined',error)


if __name__=='__main__':unittest.main()
