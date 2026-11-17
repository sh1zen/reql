"""Exercise complete assigned-work and batch flows through existing work owners."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from agents.install import install_agent_files
from memory.agent import AgentBatchError, AgentWorkspace
from memory.services.coordination import CoordinationStore


class AgentWorkBatchTests(unittest.TestCase):
    """Retain revision, dependency, provenance and recovery semantics with fewer calls."""

    def setUp(self) -> None:
        scratch=Path('.tmp')/'regression-tests';scratch.mkdir(parents=True,exist_ok=True)
        self.temp=TemporaryDirectory(dir=scratch);self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()/'project';self.root.mkdir()
        self.storage=self.root/'.reql/memory.reql';self.storage.parent.mkdir()
        self.storage.write_bytes(b'Canonical graph must not be opened by work operations')
        self.work=CoordinationStore(self.storage.with_name('agent-dashboard.reql'))

    def agent(self,name: str) -> AgentWorkspace:
        """Use explicit independent identities and activities."""
        return AgentWorkspace(self.storage,agent_id=f'agent:{name}',activity_id=name)

    def plan(self) -> list[dict]:
        """Build a linked plan without hardcoded storage ids."""
        return [
            dict(ref='goal',kind='goal',content='Keep wire compatibility',key='wire-goal'),
            dict(ref='contract',kind='decision',content='Retain schema',rationale='Existing consumer',parent={'ref':'goal'}),
            dict(ref='producer',kind='task',content='Update producer',parent={'ref':'goal'},depends_on=[{'ref':'contract'}],next_action='Run producer tests'),
            dict(ref='consumer',kind='task',content='Update consumer',parent={'ref':'goal'},depends_on=[{'ref':'contract'}]),
            dict(ref='integration',kind='task',content='Verify wire integration',parent={'ref':'goal'},depends_on=[{'ref':'producer'},{'ref':'consumer'}]),
        ]

    def cli(self,*args: str,input_text: str | None=None,expected: int=0) -> subprocess.CompletedProcess:
        """Run the real entrypoint with a local project and no provider access."""
        env=dict(os.environ,PYTHONIOENCODING='utf-8',REQL_AGENT_ID='agent:cli',REQL_AGENT_ACTIVITY_ID='cli')
        result=subprocess.run([sys.executable,str(Path(__file__).resolve().parents[1]/'cli.py'),*args],
                              cwd=self.root,env=env,input=input_text,capture_output=True,text=True,encoding='utf-8',timeout=40)
        self.assertEqual(result.returncode,expected,result.stdout+result.stderr)
        return result

    def test_batch_plan_claim_complete_and_dependency_guard(self) -> None:
        coordinator=self.agent('coordinator')
        result=coordinator.record_many(self.plan(),name='Plan wire',finish_summary='Contract agreed; workers remain')
        nodes={item['ref']:item['node'] for item in result['records']}
        self.assertFalse(coordinator.exists())
        self.assertEqual(nodes['producer']['depends_on'],[nodes['contract']['id']])
        workers=[]
        for name in ('producer','consumer'):
            worker=self.agent(name);workers.append(worker)
            claimed=worker.init(name,task_id=nodes[name]['id'],expected_revision=1)
            self.assertEqual(claimed['claimed_task']['revision'],2)
            self.assertIn(nodes['contract']['id'],{row['id'] for row in claimed['work_context']['records']})
        integration=coordinator.init('Integrate',task_id=nodes['integration']['id'],expected_revision=1)
        with self.assertRaisesRegex(ValueError,'waiting'):
            coordinator.finish('Cannot finish early',task_id=nodes['integration']['id'],expected_revision=2)
        self.assertTrue(coordinator.exists())
        self.assertEqual(self.work.show(nodes['integration']['id'])['status'],'in_progress')
        for name,worker in zip(('producer','consumer'),workers):
            finished=worker.finish('Owned contract tests pass',task_id=nodes[name]['id'],expected_revision=2)
            self.assertEqual(finished['completed_task']['revision'],3)
            self.assertEqual(finished['completed_task']['next_action'],'')
            self.assertEqual(finished['completed_task']['origin_agent_id'],'agent:coordinator')
            self.assertFalse(worker.exists())
        result=coordinator.finish('Integration passed',task_id=nodes['integration']['id'],expected_revision=2)
        self.assertEqual(result['completed_task']['status'],'done')
        self.assertTrue({'agent_id', 'status', 'closed_session_id', 'context', 'retention', 'completed_task'}.issubset(result))
        checkpoint = self.work.show(result['checkpoint']['id'])
        self.assertEqual(result['checkpoint'], {key: checkpoint[key] for key in ('id', 'revision', 'status')})
        self.assertIn(nodes['integration']['id'], checkpoint['summarizes'])
        self.assertFalse(coordinator.exists())
        self.assertEqual(self.storage.read_bytes(),b'Canonical graph must not be opened by work operations')

    def test_coordinator_accepts_managed_lanes_without_worker_sessions(self) -> None:
        """Accept worker evidence in dependency order without duplicating private sessions."""
        coordinator=self.agent('coordinator')
        plan=self.plan()
        for entry in plan:
            if entry.get('ref') in {'producer','consumer'}:
                entry['status']='in_progress'
        result=coordinator.record_many(plan,name='Dispatch',finish_summary='Managed workers assigned')
        nodes={item['ref']:item['node'] for item in result['records']}
        integration=nodes['integration']
        early=dict(kind='task',record_id=integration['id'],expected_revision=1,
                   content=integration['content'],status='done',rationale='Cannot accept early',next_action='')
        with self.assertRaises(AgentBatchError) as caught:
            coordinator.record_many([early],name='Review',finish_summary='Must not finish')
        self.assertIn('waiting',str(caught.exception))
        self.assertEqual(caught.exception.records,[])
        self.assertTrue(coordinator.exists())
        entries=[]
        for name in ('producer','consumer','integration','goal'):
            node=nodes[name]
            entries.append(dict(record_id=node['id'],
                                expected_revision=node['revision'],status='done',next_action='',
                                rationale=f'Accepted {name} checks and source review'))
        accepted=coordinator.record_many(entries,finish_summary='Reviewed worker outcomes and integration checks')
        self.assertEqual(len(accepted['records']),4)
        self.assertFalse(coordinator.exists())
        for name in ('producer','consumer','integration','goal'):
            node=self.work.show(nodes[name]['id'])
            self.assertEqual(node['status'],'done')
            self.assertEqual(node['revision'],2)
            self.assertEqual(node['next_action'],'')
            self.assertEqual(node['agent_id'],'agent:coordinator')
            self.assertIn('Accepted',node['rationale'])
        self.assertEqual({item['agent_id'] for item in self.work.records()},{'agent:coordinator'})
        self.assertEqual(self.storage.read_bytes(),b'Canonical graph must not be opened by work operations')

    def test_partial_cli_updates_preserve_text_and_reject_stale_revisions(self) -> None:
        """Update outcomes without resending stored contracts or bypassing revision checks."""
        created=json.loads(self.cli('agent','batch','--name','Create','--json',
            input_text=json.dumps([dict(ref='task',kind='task',content='Preserve this detailed contract',
                                        workstream='wire',files=['src/codec.py'],next_action='Run checks')])).stdout)
        node=created['records'][0]
        entries=[dict(record_id=node['id'],expected_revision=1,status='done',
                      rationale='Owned regression checks passed',next_action='')]
        updated=json.loads(self.cli('agent','batch','--json',input_text=json.dumps(entries)).stdout)
        self.assertEqual(updated['records'][0]['revision'],2)
        saved=self.work.show(node['id'])
        self.assertEqual(saved['kind'],'task')
        self.assertEqual(saved['content'],'Preserve this detailed contract')
        self.assertEqual(saved['workstream'],'wire')
        self.assertEqual(saved['files'],['src/codec.py'])
        self.assertEqual(saved['next_action'],'')
        stale=self.cli('agent','batch','--json',input_text=json.dumps(entries),expected=2)
        self.assertIn('revision changed',json.loads(stale.stdout)['error']['message'])
        self.assertEqual(self.work.show(node['id'])['revision'],2)

    def test_parallel_claim_accepts_one_owner_for_open_and_in_progress_work(self) -> None:
        """Both claim states retain exclusive ownership and revision checks."""
        planner = self.agent('planner')
        planner.init('Plan')
        for state in ('open', 'in_progress'):
            with self.subTest(state=state):
                task = planner.add_task(f'Claim serializer work: {state}', status=state)['node']

                def claim(name: str) -> str:
                    try:
                        self.agent(name).init('Claim work', task_id=task['id'], expected_revision=1)
                        return 'accepted'
                    except ValueError as exc:
                        self.assertIn('revision changed', str(exc))
                        return 'rejected'

                with ThreadPoolExecutor(max_workers=2) as pool:
                    results = list(pool.map(claim, ('one', 'two')))
                self.assertCountEqual(results, ['accepted', 'rejected'])
                saved = self.work.show(task['id'])
                self.assertEqual(saved['revision'], 2)
                self.assertIn(saved['agent_id'], {'agent:one', 'agent:two'})

    def test_partial_single_updates_preserve_validation_and_dependency_guards(self) -> None:
        """Optional positional text must not bypass creation, CAS or completion rules."""
        self.cli('agent', 'init', '--name', 'Guard partial updates')
        question = json.loads(self.cli('agent', 'record', 'question', 'Which wire version?', '--json').stdout)['node']
        task = json.loads(self.cli('agent', 'record', 'task', 'Verify consumer', '--depends-on',
                                   question['id'], '--json').stdout)['node']
        before = self.work.records()
        cases = [
            (('--id', task['id'], '--revision', '1', '--status', 'done'), 'waiting'),
            (('--id', task['id'], '--revision', '1', '--status', 'resolved'), 'waiting'),
            (('--id', task['id'], '--status', 'in_progress'), 'expected_revision'),
            (('decision', '--id', task['id'], '--revision', '1'), 'kind'),
            ((), 'kind and content'),
            (('task',), 'kind and content'),
        ]
        for arguments, message in cases:
            with self.subTest(arguments=arguments):
                rejected = self.cli('agent', 'record', *arguments, '--json', expected=2)
                self.assertIn(message, json.loads(rejected.stdout)['error']['message'])
                self.assertEqual(self.work.records(), before)
        self.cli('agent', 'finish', 'Question and dependent work remain')

    def test_stdin_batch_returns_checkpoint_for_current_linked_recovery(self) -> None:
        """Recover a finished plan directly from its receipt, with current dependency state."""
        payload = json.loads(self.cli('agent', 'batch', '--name', 'Plan', '--finish', 'Workers remain',
                                      '--json', input_text=json.dumps(self.plan())).stdout)
        checkpoint = payload['finish']['checkpoint']
        saved = self.work.show(checkpoint['id'])
        self.assertEqual(checkpoint, {k: saved[k] for k in ('id', 'revision', 'status')})
        self.assertEqual(saved['kind'], 'checkpoint')
        ids = {r['ref']: r['id'] for r in payload['records']}
        self.assertEqual(set(saved['summarizes']), set(ids.values()))
        self.assertFalse(self.agent('cli').exists())
        recovered = json.loads(self.cli('project', 'context', '--task', checkpoint['id'], '--json').stdout)
        self.assertTrue(set(ids.values()).issubset({r['id'] for r in recovered['records']}))
        self.cli('agent', 'batch', '--name', 'Reconcile', '--finish', 'Producer blocked', input_text=json.dumps([
            dict(record_id=ids['producer'], expected_revision=1, status='blocked', rationale='Await consumer decision')]))
        recovered = json.loads(self.cli('project', 'context', '--task', checkpoint['id'], '--json').stdout)
        current = next(r for r in recovered['records'] if r['id'] == ids['producer'])
        self.assertEqual((current['revision'], current['status']), (2, 'blocked'))
        self.assertEqual(self.storage.read_bytes(), b'Canonical graph must not be opened by work operations')

    def test_cli_partial_receipts_and_input_boundary(self) -> None:
        """Failed batches preserve committed receipts and reject untrusted inputs."""
        for invalid, message in (
            (dict(kind='decision', content='Missing why'), 'rationale'),
            (dict(kind='task', content='Must not create this task', record_id=0, expected_revision=0), 'record id'),
        ):
            with self.subTest(invalid=invalid):
                result = self.cli('agent', 'batch', '--name', 'Partial', '--finish', 'Not reached',
                                  '--json', expected=2, input_text=json.dumps([
                                      dict(kind='goal', content='Keep committed goal'), invalid]))
                payload = json.loads(result.stdout)
                self.assertEqual(payload['error']['failed_entry'], 2)
                self.assertIn(message, payload['error']['message'])
                self.assertEqual(len(payload['records']), 1)
                self.assertNotIn('finish', payload)
                self.assertEqual(self.work.show(payload['records'][0]['id'])['content'], 'Keep committed goal')
                self.assertEqual([item['kind'] for item in self.work.records()], ['goal'])
                self.assertTrue(self.agent('cli').exists())
        outside = self.root.parent / 'external.json'
        outside.write_text('[]')
        result = self.cli('agent', 'batch', '--file', str(outside), '--json', expected=2)
        self.assertIn('inside the project', result.stdout)

    @unittest.skipUnless(os.name=='nt' and shutil.which('powershell'),'Needs native Windows PowerShell')
    def test_native_powershell_shim_can_clear_next_without_empty_argument(self) -> None:
        installed=install_agent_files(('codex',),project=True,project_dir=self.root,command_dir=self.root/'bin',hooks=False)
        command=next(item.path for item in installed.actions if item.kind=='command')
        self.cli('agent','init','--name','Question')
        question=json.loads(self.cli('agent','record','question','Budget includes first send?','--next','Ask product','--json').stdout)['node']
        quote=lambda value:"'"+str(value).replace("'","''")+"'"
        script=f"$env:REQL_AGENT_ID='agent:cli'; $env:REQL_AGENT_ACTIVITY_ID='cli'; & {quote(command)} agent record question 'Budget includes first send?' --id {quote(question['id'])} --revision 1 --status resolved --why 'Confirmed total budget' --next=; exit $LASTEXITCODE"
        result=subprocess.run([shutil.which('powershell'),'-NoProfile','-Command',script],cwd=self.root,
                              capture_output=True,text=True,timeout=40)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(self.work.show(question['id'])['next_action'],'')


if __name__=='__main__':unittest.main()
