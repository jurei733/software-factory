"""Regression tests for the reviewed host, publisher, recovery and runtime failures."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import factory
import tares_adapter as adapter
import pilot_checks
from connector_publisher import Publisher, ConnectorError
from runtime_safety import runtime_path, process_identity, capture_identity, owns_process, stop_owned
from scripts import tares_runtime, start_claude_crew
# collect_factory_metrics imports its sibling as a CLI module.
sys.path.insert(0, str(ROOT/'scripts'))
from scripts.collect_factory_metrics import ci_metrics


def git(repo, *args):
    return subprocess.check_output(['git','-C',str(repo),*args], text=True).strip()


def commit(repo):
    subprocess.run(['git','init','-q',str(repo)],check=True)
    git(repo,'add','.')
    git(repo,'-c','user.name=Test','-c','user.email=test@example.invalid','commit','-qm','test baseline')
    return git(repo,'rev-parse','HEAD')


class BoundaryTests(unittest.TestCase):
    def test_dependency_contract_checks_url_integrity_and_extra_metadata_before_execution(self):
        with tempfile.TemporaryDirectory() as folder:
            project=Path(folder)/'app';shutil.copytree(ROOT/'examples/tares-pilot',project,ignore=shutil.ignore_patterns('node_modules','dist'))
            self.assertEqual(pilot_checks.dependency_contract(project),'1.1.1')
            original=json.loads((project/'package-lock.json').read_text())
            for key,value in [('resolved','https://example.invalid/payload.tgz'),('integrity','sha512-FAKE'),('hasInstallScript',True),('license','unapproved')]:
                lock=copy.deepcopy(original);lock['packages']['node_modules/picocolors'][key]=value
                (project/'package-lock.json').write_text(json.dumps(lock))
                with patch.object(pilot_checks,'ensure_image') as image, self.assertRaises(factory.FactoryError):
                    pilot_checks.run_project(project,'dependency',Path(folder),Path(folder)/'log')
                image.assert_not_called()
            with self.assertRaises(factory.FactoryError):pilot_checks.verify_tarball('1.1.1',b'synthetic corrupt archive')

    def test_runtime_rejected_before_any_mutation_even_via_symlink_and_nonexistent_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);repo=root/'repo';repo.mkdir();(repo/'source').write_text('safe');commit(repo)
            alias=root/'alias';alias.symlink_to(repo, target_is_directory=True)
            for path in (repo,repo/'new/runtime',alias/'nested/runtime',ROOT/'dist/runtime'):
                with self.assertRaises(ValueError):runtime_path(path)
                with patch.object(sys,'argv',['runtime','start','--runtime',str(path)]), self.assertRaises(ValueError):tares_runtime.main()
                self.assertFalse((path/'root-token').exists())
            empty=root/'absent'
            with patch.object(sys,'argv',['runtime','status','--runtime',str(empty)]):self.assertEqual(tares_runtime.main(),1)
            self.assertFalse(empty.exists())

    def test_status_requires_both_processes_api_and_mcp(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'root-token').write_text('synthetic')
            with patch.object(tares_runtime,'owns_process',side_effect=[True,False]), patch.object(tares_runtime,'Tares') as api, patch.object(tares_runtime,'mcp_health',return_value=True):
                api.return_value.call.return_value={'status':'ok'}
                self.assertEqual(tares_runtime.status(root,{'api_port':18787,'mcp_port':18788})['status'],'unhealthy')
            with patch.object(tares_runtime,'owns_process',return_value=True), patch.object(tares_runtime,'Tares') as api, patch.object(tares_runtime,'mcp_health',return_value=False):
                api.return_value.call.return_value={'status':'ok'}
                self.assertEqual(tares_runtime.status(root,{'api_port':18787,'mcp_port':18788})['status'],'unhealthy')

    def test_stale_pid_cannot_signal_another_live_process(self):
        process=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],start_new_session=True)
        try:
            with tempfile.TemporaryDirectory() as folder:
                root=Path(folder);identity=capture_identity(process.pid)
                self.assertIsNotNone(identity)
                for change in ({'runtime':str(root/'other')},{'started':'stale'},{'argv':'other tares up'},{'pgid':process.pid+1}):
                    record=dict(identity,runtime=str(root));record.update(change)
                    with patch('runtime_safety.os.killpg') as kill:
                        self.assertFalse(stop_owned(record,root));kill.assert_not_called()
                (root/'daemon.pid').write_text(str(process.pid))
                with patch.object(sys,'argv',['runtime','stop','--runtime',str(root)]):self.assertEqual(tares_runtime.main(),0)
                self.assertIsNone(process.poll());self.assertFalse((root/'daemon.pid').exists())
                record=dict(identity,runtime=str(root));self.assertTrue(owns_process(record,root))
                self.assertTrue(stop_owned(record,root));self.assertIsNotNone(process.poll())
        finally:
            if process.poll() is None:os.killpg(process.pid,signal.SIGKILL);process.wait()

    def test_state_or_log_failure_always_reaps_spawned_agent(self):
        for where in ('state','log'):
            with self.subTest(where=where), tempfile.TemporaryDirectory() as folder:
                root=Path(folder);seen=[]
                def state(values):
                    if 'pid' in values:seen.append(values['pid'])
                    if where=='state':raise OSError('synthetic disk full')
                real_open=Path.open
                class BrokenLog:
                    def __enter__(self):return self
                    def __exit__(self,*args):pass
                    def write(self,*args):raise OSError('synthetic log disk full')
                def opened(path,*args,**kwargs):
                    return BrokenLog() if where=='log' and path.suffix=='.jsonl' else real_open(path,*args,**kwargs)
                command=[sys.executable,'-c',"import time;print('{\"type\":\"thread.started\",\"thread_id\":\"test\"}',flush=True);time.sleep(30)"]
                with patch.object(adapter,'codex_command',return_value=command), patch.object(Path,'open',opened), self.assertRaisesRegex(OSError,'disk full'):
                    adapter.agent(root,'test',root,'worker',10,on_state=state)
                self.assertTrue(seen);self.assertIsNone(process_identity(seen[0]))
                result=json.loads((root/'worker.result.json').read_text());self.assertFalse(result['completed'])

    def test_partial_stdout_cannot_disable_agent_deadline(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            command=[sys.executable,'-c',"import sys,time;sys.stdout.write('partial');sys.stdout.flush();time.sleep(30)"]
            with patch.object(adapter,'codex_command',return_value=command), self.assertRaises(subprocess.TimeoutExpired):
                adapter.agent(root,'test',root,'worker',1)

    def test_ci_denominator_excludes_skipped_and_cancelled(self):
        runs=[{'status':'completed','conclusion':x} for x in ('success','skipped','cancelled')]
        self.assertEqual(ci_metrics(runs),{'completed_ci_runs':1,'skipped_ci_runs':1,'cancelled_ci_runs':1,'ci_success_rate':1})
        runs.append({'status':'completed','conclusion':'failure'})
        self.assertEqual(ci_metrics(runs)['ci_success_rate'],0.5)
        self.assertIsNone(ci_metrics(runs[1:3])['ci_success_rate'])

    def test_claude_start_rejects_missing_preparation_before_auth_or_spawn(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(sys,'argv',['crew','--runtime',folder]), patch.object(start_claude_crew.subprocess,'run') as run:
            with self.assertRaisesRegex(ValueError,'Initialize project'):start_claude_crew.main()
            self.assertFalse(any(call.args[0][0] == "claude" for call in run.call_args_list))

    def test_finalization_retries_only_ledger_after_commit_failure(self):
        for failing_endpoint in ('checks','stage'):
            with self.subTest(endpoint=failing_endpoint), tempfile.TemporaryDirectory() as folder:
                root=Path(folder);tree=root/'worktrees/TASK';tree.mkdir(parents=True);(tree/'app').mkdir();(tree/'app/source').write_text('before')
                parent=commit(tree);(tree/'app/source').write_text('validated');git(tree,'add','.')
                config={'working_directory':'app'};task={'id':'TASK','title':'Fix','max_attempts':2}
                item={'status':'validated','attempts':2,'ticket':'ticket','validated_parent':parent,
                      'validated_tree':git(tree,'write-tree'),'validated_manifest':adapter.source_digest(tree,config)}
                state={'project_id':'project','tasks':{'TASK':item}};adapter.save(root/'pilot.json',state)
                api=Mock()
                def call(method,path,data):
                    if path.endswith('/'+failing_endpoint):raise OSError('synthetic ledger outage')
                api.call.side_effect=call
                with self.assertRaises(OSError):adapter.finalize_ticket(root,config,task,state,api)
                head=git(tree,'rev-parse','HEAD');self.assertNotEqual(head,parent)
                saved=factory.read_json(root/'pilot.json');self.assertEqual(saved['tasks']['TASK']['status'],'finalization_pending')
                api.call.side_effect=None
                with patch.object(adapter,'initialize',return_value=saved), patch.object(adapter,'agent',side_effect=AssertionError('No new coding turn')), patch.object(adapter,'check_project',side_effect=AssertionError('No rechecks')):
                    result=adapter.run_ticket(ROOT,config,root,task,api)
                self.assertEqual(result['status'],'ready_for_publication');self.assertEqual(result['attempts'],2)
                self.assertEqual(git(tree,'rev-parse','HEAD'),head)

    def test_already_solved_pilot_stops_before_spending_attempt(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);tree=root/'worktrees/TASK';(tree/'app').mkdir(parents=True);(tree/'app/source').write_text('solved');commit(tree)
            state={'project_id':'project','tasks':{'TASK':{'ticket':'ticket','status':'pending','attempts':0}}}
            config={'working_directory':'app'};task={'id':'TASK','title':'Fix','max_attempts':2,'max_minutes':10,'acceptance':'zero'}
            with patch.object(adapter,'initialize',return_value=state), patch.object(adapter,'acceptance',return_value=True), patch.object(adapter,'agent') as agent:
                with self.assertRaisesRegex(factory.FactoryError,'already solved'):adapter.run_ticket(ROOT,config,root,task,Mock())
                agent.assert_not_called()
            self.assertEqual(factory.read_json(root/'pilot.json')['tasks']['TASK']['attempts'],0)


class RawGitTests(unittest.TestCase):
    def test_host_worktree_staging_commit_and_publication_never_execute_git_hooks_or_filters(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);repo=root/'repo';(repo/'app').mkdir(parents=True)
            (repo/'app/source.js').write_text('original')
            (repo/'.gitattributes').write_text('* filter=unsafe\n')
            base=commit(repo)
            marker=root/'host-executed';script=root/'unsafe.sh'
            script.write_text('#!/bin/sh\nprintf escaped > '+str(marker)+'\ncat\n');script.chmod(0o755)
            git(repo,'config','filter.unsafe.clean',str(script));git(repo,'config','filter.unsafe.smudge',str(script))
            hooks=repo/'.git/hooks'
            for name in ('post-checkout','pre-commit','post-index-change','reference-transaction'):
                hook=hooks/name;hook.write_text('#!/bin/sh\nprintf escaped > '+str(marker)+'\n');hook.chmod(0o755)
            runtime=root/'runtime';runtime.mkdir()
            task={'id':'TASK','title':'Fix','goal':'Fix','max_attempts':2,'max_minutes':10,'allowed_paths':['app/source.js'],'acceptance':'zero'}
            config={'working_directory':'app','repository':'example-owner/software-factory'}
            state={'project_id':'project','base_local_commit':base,'tasks':{'TASK':{'ticket':'ticket','status':'pending','attempts':0}}}
            def agent(cwd,prompt,directory,name,*args,**kwargs):
                if name.startswith('worker'):
                    (cwd/'source.js').write_text('corrected')
                    return {'session':'test'},'done'
                return {},json.dumps({'approved':True,'findings':[]})
            with patch.object(adapter,'initialize',return_value=state),patch.object(adapter,'acceptance',return_value=False),patch.object(adapter,'agent',side_effect=agent),patch.object(adapter,'check_project',return_value={'passed':True}):
                result=adapter.run_ticket(repo,config,runtime,task,Mock())
                self.assertEqual(result['status'],'ready_for_publication')
                packet=factory.read_json(runtime/'tasks/TASK/publication.json')
                publication=runtime/'publication';publication.mkdir()
                expected=adapter.publication_tree(repo,base,packet['files'],publication)
                adapter.validate_publication(repo,config,task,publication,expected,base)
            self.assertFalse(marker.exists(),'A Git hook or clean/smudge filter executed on the host')
            self.assertEqual(git(repo,'show',result['local_commit']+':app/source.js'),'corrected')

    def test_nested_git_metadata_is_rejected_before_checks_or_reviewer(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime=Path(folder);tree=runtime/'worktrees/TASK';project=tree/'app';project.mkdir(parents=True)
            (project/'source.js').write_text('before');commit(tree)
            task={'id':'TASK','title':'Fix','goal':'Fix','max_attempts':2,'max_minutes':10,'allowed_paths':['app/source.js'],'acceptance':'zero'}
            config={'working_directory':'app'}
            state={'project_id':'project','tasks':{'TASK':{'ticket':'ticket','status':'pending','attempts':0}}}
            def worker(*args,**kwargs):
                (project/'.git').mkdir();(project/'source.js').write_text('after')
                return {'session':'test'},'done'
            with patch.object(adapter,'initialize',return_value=state),patch.object(adapter,'acceptance',return_value=False),patch.object(adapter,'agent',side_effect=worker) as agent,patch.object(adapter,'check_project') as checks:
                with self.assertRaisesRegex(factory.FactoryError,'Nested Git metadata'):adapter.run_ticket(ROOT,config,runtime,task,Mock())
                checks.assert_not_called();self.assertEqual(agent.call_count,1)

    def test_permission_roots_are_pinned_instead_of_derived_from_nested_git(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);repo=root/'repo';project=repo/'app';project.mkdir(parents=True);(project/'source.js').write_text('synthetic');commit(repo)
            private=root/'private';private.mkdir();scratch=root/'scratch';scratch.mkdir()
            subprocess.run(['git','-C',str(project),'init','-q'],check=True)
            git(project,'config','core.worktree',str(private))
            args=adapter.permission_args(project,scratch,writable=True,source_root=repo,allowed_paths=[project/'source.js'])
            self.assertNotIn(str(private),' '.join(args))
            self.assertIn('"'+str((project/'source.js').resolve())+'"="write"',' '.join(args))
            self.assertIn('"'+str(project.resolve())+'"="read"',' '.join(args))

    def test_raw_snapshots_ignore_export_attributes_and_preserve_exact_blob_bytes(self):
        from isolated_runner import copy_git_tree
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);repo=root/'repo';repo.mkdir()
            (repo/'.gitattributes').write_text('hidden.txt export-ignore\nsubstituted.txt export-subst\n')
            (repo/'hidden.txt').write_text('must stay present')
            (repo/'substituted.txt').write_text('$Format:%H$')
            commit(repo);target=root/'snapshot';target.mkdir()
            copy_git_tree(repo,'HEAD',target)
            self.assertEqual((target/'hidden.txt').read_text(),'must stay present')
            self.assertEqual((target/'substituted.txt').read_text(),'$Format:%H$')


class FakeConnector:
    def __init__(self,plan):self.plan=plan;self.writes=[];self.branch=None;self.pr=None;self.bad_parent=False;self.base_moved=False
    def call(self,tool,args):
        p=self.plan
        if tool=='github.fetch':
            url=args['url']
            if '/branches/'+p['base_ref'] in url:return {'commit':{'sha':'f'*40 if self.base_moved else p['base_sha'],'commit':{'tree':{'sha':p['base_tree']}}}}
            if '/branches/'+p['branch'] in url:
                if not self.branch:raise ConnectorError('404 not found')
                return {'commit':{'sha':'d'*40,'commit':{'tree':{'sha':p['tree']}}}}
            if '/git/commits/' in url:return {'sha':'d'*40,'tree':{'sha':p['tree']},'parents':[{'sha':'e'*40 if self.bad_parent else p['base_sha']}]}
            if '/pulls?' in url:return [] if not self.pr else [{'html_url':self.pr['url'],'head':{'ref':p['branch'],'sha':'d'*40,'repo':{'full_name':p['repository']}},'base':{'ref':p['base_ref'],'repo':{'full_name':p['repository']}}}]
            raise AssertionError(url)
        self.writes.append((tool,args))
        if tool=='github.create_tree':return {'sha':p['tree']}
        if tool=='github.create_commit':return {'sha':'d'*40}
        if tool=='github.create_branch':self.branch=True;return {'ref':'refs/heads/'+p['branch']}
        if tool=='github.create_pull_request':
            self.pr={'url':'https://github.com/'+p['repository']+'/pull/10','head':p['branch'],'base':p['base_ref'],'head_sha':'d'*40};return self.pr
        raise AssertionError(tool)


class PublisherTests(unittest.TestCase):
    def plan(self):
        content='synthetic change\n';raw=content.encode()
        return {'repository':'example-owner/software-factory','base_ref':'feat/pilot','base_sha':'a'*40,'base_tree':'b'*40,'tree':'c'*40,
                'branch':'factory/test-001','title':'Test','body':'Checked. Human merge required.','allowed_paths':['app/file'],
                'files':[{'path':'app/file','content':content,'git_blob_sha':hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()}]}

    def test_exact_sequence_and_receipts_are_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            p=self.plan();transport=FakeConnector(p);root=Path(folder)
            receipt=Publisher(transport,p,root).run()
            self.assertEqual(receipt['parents'],['a'*40]);self.assertFalse(receipt['merge_allowed'])
            self.assertEqual([t for t,a in transport.writes],['github.create_tree','github.create_commit','github.create_branch','github.create_pull_request'])
            Publisher(transport,p,root).run();self.assertEqual(len(transport.writes),4)

    def test_extra_write_wrong_repository_path_ref_or_args_never_dispatches(self):
        with tempfile.TemporaryDirectory() as folder:
            p=self.plan();transport=FakeConnector(p);pub=Publisher(transport,p,Path(folder))
            for tool,args in [('github.update_file',{'repository_full_name':p['repository']}),('github.create_tree',{'repository_full_name':'sample-owner/sample-web'}),('github.create_branch',{'branch_name':'main'})]:
                with self.assertRaises(factory.FactoryError):pub.mutation('tree',tool,args)
            self.assertEqual(transport.writes,[])
            for changed in ({'repository':'https://example.invalid/owner/repository'},{'branch':'main'},{'files':[dict(p['files'][0],path='../outside')]}):
                with self.assertRaises(factory.FactoryError):Publisher(transport,dict(p,**changed),Path(folder))

    def test_wrong_parent_and_moving_base_stop_before_exposing_pr(self):
        for fault in ('bad_parent','base_moved'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as folder:
                p=self.plan();transport=FakeConnector(p);setattr(transport,fault,True)
                with self.assertRaises(factory.FactoryError):Publisher(transport,p,Path(folder)).run()
                self.assertNotIn('github.create_pull_request',[t for t,a in transport.writes])
                self.assertNotIn('github.create_branch',[t for t,a in transport.writes])

    def test_interrupted_branch_and_pr_calls_reconcile_without_duplicate_mutations(self):
        for endpoint in ('github.create_branch','github.create_pull_request'):
            with self.subTest(endpoint=endpoint),tempfile.TemporaryDirectory() as folder:
                p=self.plan();transport=FakeConnector(p);original=transport.call;failed=[False]
                def call(tool,args):
                    result=original(tool,args)
                    if tool==endpoint and not failed[0]:failed[0]=True;raise ConnectorError('receipt lost after server success')
                    return result
                transport.call=call
                with self.assertRaises(ConnectorError):Publisher(transport,p,Path(folder)).run()
                receipt=Publisher(transport,p,Path(folder)).run()
                self.assertTrue(receipt['publication_verified']);self.assertEqual([t for t,a in transport.writes].count(endpoint),1)


if __name__=='__main__':unittest.main()
