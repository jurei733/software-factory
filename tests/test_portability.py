import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import factory
import repository_factory as repository
sys.path.insert(0,str(ROOT/'scripts'))
import publish_delivery
import delivery_metrics


class PortabilityTests(unittest.TestCase):
    def test_factory_self_profile_binds_to_unrelated_owners_and_repository_names(self):
        config=factory.read_json(ROOT/'profiles/factory.json')
        for name in ('someone/project','another-team/monorepo','different-owner/a.b_c-d'):
            with self.subTest(repository=name):
                environment={'GITHUB_REPOSITORY':name}
                self.assertEqual(repository.ci_plan(ROOT,config,environment)['stack'],'python')
                self.assertEqual(repository.bind_repository(config,environment)['project'],name)
        with self.assertRaises(factory.FactoryError):repository.ci_plan(ROOT,config,{'GITHUB_REPOSITORY':'https://example.invalid/project'})

    def test_npm_pnpm_and_yarn_preserve_real_scripts_and_locked_installs(self):
        for manager,version,lock in [('npm','10.9.9','package-lock.json'),('pnpm','10.8.1','pnpm-lock.yaml'),('yarn','4.9.1','yarn.lock')]:
            with self.subTest(manager=manager),tempfile.TemporaryDirectory() as folder:
                root=Path(folder);(root/'README.md').write_text('Synthetic project')
                (root/'package.json').write_text(json.dumps({'packageManager':manager+'@'+version,'scripts':{'lint':'tool lint','test':'tool test','build':'tool build'}}))
                (root/lock).write_text('{}')
                profile=repository.draft_profile(root,'other-team/application')
                expected=['npm'] if manager=='npm' else ['corepack',manager]
                self.assertEqual(profile['steps'][0]['argv'],expected+['run','lint'])
                self.assertEqual(profile['ci']['install'][0]['argv'][:len(expected)],expected)
                self.assertFalse(profile['reviewed'])
                self.assertIn('ci' if manager=='npm' else '--frozen-lockfile' if manager=='pnpm' else '--immutable',profile['ci']['install'][0]['argv'])

    def test_custom_go_java_rust_and_empty_projects_fail_until_real_checks_are_configured(self):
        for manifest in ('go.mod','pom.xml','Cargo.toml','Makefile',None):
            with self.subTest(manifest=manifest),tempfile.TemporaryDirectory() as folder:
                root=Path(folder)
                if manifest:(root/manifest).write_text('synthetic inventory data')
                profile=repository.draft_profile(root,'third-owner/arbitrary-repository')
                self.assertEqual(profile['ci']['stack'],'custom');self.assertFalse(profile['reviewed'])
                self.assertTrue(all('needs-configuration.py' in step['argv'][-1] for step in profile['steps']))
                output=root.parent/(root.name+'-overlay')
                try:
                    repository.export_repository(profile,output)
                    result=subprocess.run([sys.executable,str(output/'.factory/needs-configuration.py')],capture_output=True,text=True)
                    self.assertNotEqual(result.returncode,0)
                    self.assertTrue((output/'.github/workflows/factory-quality.yml').is_file())
                finally:shutil.rmtree(output,ignore_errors=True)

    def test_delivery_identity_and_api_scope_come_from_the_current_repository(self):
        for repository_name in ('one-owner/first-project','other-team/different-project'):
            env={'GITHUB_REPOSITORY':repository_name,'FACTORY_DEFAULT_BRANCH':'trunk','GITHUB_REF':'refs/heads/trunk',
                 'GITHUB_SHA':'a'*40,'GITHUB_RUN_ID':'1','GITHUB_RUN_ATTEMPT':'1'}
            metadata=publish_delivery.context(env)
            self.assertEqual(metadata['factory_repository'],repository_name)
            self.assertEqual(metadata['run_url'],'https://github.com/'+repository_name+'/actions/runs/1')
            self.assertEqual(publish_delivery.GitHub('synthetic-never-sent',repository_name).repository,repository_name)

    def test_metrics_accept_any_explicit_application_id(self):
        report=delivery_metrics.calculate([],'unrelated-service-v2')
        self.assertEqual(report['application'],'unrelated-service-v2')
        self.assertIsNone(report['change_fail_rate'])
        with self.assertRaises(ValueError):delivery_metrics.calculate([],'../invalid')

    def test_generic_task_cli_does_not_execute_an_agent(self):
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/'packet'
            result=subprocess.run([sys.executable,str(ROOT/'factory.py'),'prepare','--config',str(ROOT/'profiles/factory.json'),
                '--task',str(ROOT/'tasks/pilot.json'),'--assistant','codex','--out',str(target)],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertFalse(factory.read_json(target/'handoff.json')['executes_agent'])
            self.assertEqual(factory.read_json(target/'task.json')['allowed_paths'],[])


if __name__=='__main__':unittest.main()
