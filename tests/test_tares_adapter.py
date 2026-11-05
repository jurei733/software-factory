import copy
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import factory
import tares_adapter as adapter
from scripts import start_claude_crew


class TaresAdapterTests(unittest.TestCase):
    def test_workers_have_no_apps_keys_network_or_bypass(self):
        command = adapter.codex_command(Path('/work/repo'), Path('/runtime/answer'), writable=True)
        for value in ('default_permissions="factory_agent"', 'forced_login_method="chatgpt"',
                      'permissions.factory_agent.network.enabled=false', '--ignore-user-config', 'apps._default.enabled=false'):
            self.assertIn(value, command)
        self.assertNotIn('--dangerously-bypass-approvals-and-sandbox', command)
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'synthetic', 'GH_TOKEN': 'synthetic', 'ANTHROPIC_API_KEY': 'synthetic'}):
            for name in ('OPENAI_API_KEY', 'GH_TOKEN', 'ANTHROPIC_API_KEY'):
                self.assertNotIn(name, adapter.environment())

    def test_review_is_read_only_and_resume_uses_exact_session(self):
        command = adapter.codex_command(Path('/work/repo'), Path('/runtime/answer'), resume='session-id')
        self.assertIn('permissions.factory_agent.network.enabled=false', command)
        self.assertTrue(any('filesystem=' in value and '"/work/repo"="read"' in value for value in command))
        self.assertEqual(command[-3:], ['resume', 'session-id', '-'])

    def test_publisher_has_no_model_turn_or_shell_rpc(self):
        from connector_publisher import CodexConnector
        self.assertNotIn('turn/start', CodexConnector.METHODS)
        self.assertNotIn('command/exec', CodexConnector.METHODS)
        connector = CodexConnector.__new__(CodexConnector)
        for tool in ('github.update_file', 'github.update_ref', 'github.merge_pull_request', 'github.create_issue'):
            with self.assertRaises(factory.FactoryError): connector.call(tool, {})

    def test_scope_detects_untracked_changes_and_links(self):
        before = {'src/main.js': ('a', False)}
        self.assertEqual(adapter.check_scope(before, {'src/main.js': ('b', False), 'dist/out.js': ('c', False)}, ['src'], ['dist']), ['src/main.js'])
        for after in ({'src/main.js': ('b', False), 'secret.txt': ('c', False)}, {'src/main.js': ('symlink', '../outside')}):
            with self.assertRaises(factory.FactoryError):
                adapter.check_scope(before, after, ['src'])

    def test_config_rejects_upstream_writes_scope_escape_and_unbounded_attempts(self):
        original = factory.read_json(ROOT/'config/tares-pilot.json')
        adapter.config_at(ROOT/'config/tares-pilot.json')
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder)/'config.json'
            for field, value in (('repository', 'https://example.invalid/owner/project'), ('scope', '../outside'), ('attempts', 3)):
                config = copy.deepcopy(original)
                if field == 'repository': config['repository'] = value
                elif field == 'scope': config['tasks'][0]['allowed_paths'] = [value]
                else: config['tasks'][0]['max_attempts'] = value
                file.write_text(json.dumps(config))
                with self.assertRaises(factory.FactoryError): adapter.config_at(file)

    def test_tares_credentials_never_follow_external_urls_or_redirects(self):
        with tempfile.TemporaryDirectory() as folder:
            for url in ('https://example.invalid', 'http://127.0.0.1@example.invalid', 'http://localhost/?token=unsafe'):
                with self.assertRaises(factory.FactoryError): adapter.Tares(folder, url)
        with self.assertRaises(ValueError): adapter.NoRedirect().redirect_request(None)

    def test_authenticated_loopback_request_never_follows_a_redirect(self):
        paths = []
        class Redirect(BaseHTTPRequestHandler):
            def do_GET(self):
                paths.append(self.path)
                self.send_response(302)
                self.send_header('Location', '/token-must-not-reach-this')
                self.end_headers()
            def log_message(self, *args):
                pass
        with tempfile.TemporaryDirectory() as folder, HTTPServer(('127.0.0.1', 0), Redirect) as server:
            (Path(folder)/'root-token').write_text('synthetic-local-token')
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                api = adapter.Tares(folder, 'http://127.0.0.1:' + str(server.server_port))
                with self.assertRaisesRegex(ValueError, 'never follow a redirect'):
                    api.call('GET', '/health')
            finally:
                server.shutdown()
                thread.join(timeout=3)
        self.assertEqual(paths, ['/health'])

    def test_claude_profile_requires_own_auth_and_does_not_enable_release(self):
        config = factory.read_json(ROOT/'config/tares-pilot.json')
        with tempfile.TemporaryDirectory() as folder:
            api = Mock(url='http://127.0.0.1:18787')
            state = {'project_id':'project', 'config_sha256': factory.digest(config), 'tasks': {t['id']: {'ticket': t['id']} for t in config['tasks']}}
            with patch.object(adapter, 'initialize', return_value=state) as init:
                result = adapter.native_claude(config, folder, ROOT, api)
                init.assert_called_once_with(ROOT, config, Path(folder).resolve(), api)
            self.assertEqual(result['tickets'], 3)
            self.assertTrue((Path(folder)/'claude-crew/initialized.json').is_file())
            crew = factory.read_json(Path(result['crew_home'])/'crew.json')
            self.assertNotIn('authorized', crew)
            self.assertNotIn('releaser', crew['stations'])
            self.assertEqual(result['autonomy'], 'L3-review')
            self.assertFalse(result['releaser'])

    def test_publication_checks_exactly_one_pinned_parent(self):
        from connector_publisher import verify_commit
        good = {'sha':'a'*40, 'tree':{'sha':'b'*40}, 'parents':[{'sha':'c'*40}]}
        self.assertEqual(verify_commit(good, 'a'*40, 'b'*40, 'c'*40), ['c'*40])
        for parents in ([], [{'sha':'d'*40}], [{'sha':'c'*40}, {'sha':'d'*40}]):
            with self.assertRaises(factory.FactoryError): verify_commit(dict(good, parents=parents), 'a'*40, 'b'*40, 'c'*40)

    def test_pilot_publisher_cannot_target_main(self):
        config = factory.read_json(ROOT/'config/tares-pilot.json')
        with self.assertRaises(factory.FactoryError):
            adapter.publish_ticket(ROOT, config, ROOT/'unused', config['tasks'][0], None, 'main', 'a'*40)

    def test_publication_note_can_recover_without_another_github_write(self):
        config = factory.read_json(ROOT/'config/tares-pilot.json')
        task = config['tasks'][0]
        with tempfile.TemporaryDirectory() as folder:
            runtime = Path(folder)
            item = {'status': 'awaiting_human_merge', 'pr': 'https://github.com/example-owner/software-factory/pull/7',
                    'remote_commit': 'a'*40, 'remote_tree': 'b'*40, 'publication_attempts': 1}
            adapter.save(runtime/'pilot.json', {'project_id': 'project', 'repository':config['repository'], 'config_sha256':factory.digest(config), 'tasks': {task['id']: item}})
            api = Mock()
            api.call.side_effect = [[], {'id': 'note-id'}]
            with patch.object(adapter, 'agent', side_effect=AssertionError('Must not republish')):
                result = adapter.publish_ticket(ROOT, config, runtime, task, api, 'feat/pilot', 'c'*40)
                self.assertEqual(result['tares_note'], 'note-id')
                self.assertEqual(api.call.call_args.args[2]['kind'], 'note')
                api.reset_mock()
                adapter.publish_ticket(ROOT, config, runtime, task, api, 'feat/pilot', 'c'*40)
                api.call.assert_not_called()
            self.assertEqual(result['publication_attempts'], 1)

    def test_recovery_ignores_reinstalled_dependencies_and_handles_a_missing_worker_result(self):
        for operator_repair in (False, True):
            with self.subTest(operator_repair=operator_repair), tempfile.TemporaryDirectory() as folder:
                runtime = Path(folder)
                tree = runtime/'worktrees/RETRY-001'
                project = tree/'app'
                project.mkdir(parents=True)
                (project/'main.js').write_text('original')
                (tree/'.gitignore').write_text('app/node_modules/\n')
                subprocess.run(['git', 'init', '-q', str(tree)], check=True)
                subprocess.run(['git', '-C', str(tree), 'add', '.'], check=True)
                subprocess.run(['git', '-C', str(tree), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                                'commit', '-qm', 'baseline'], check=True)
                (project/'node_modules').mkdir()
                dependency = project/'node_modules/dependency.js'
                dependency.write_text('old dependency')
                task = {'id': 'RETRY-001', 'title': 'Fix', 'goal': 'Fix', 'max_attempts': 2, 'max_minutes': 10,
                        'allowed_paths': ['app/main.js'], 'acceptance': 'zero'}
                config = {'working_directory': 'app', 'repository': 'example-owner/software-factory'}
                adapter.save(runtime/'pilot.json', {'project_id': 'project', 'tasks': {
                    task['id']: {'ticket': 'ticket', 'attempts': 0, 'status': 'pending'}}})
                def checks(*args):
                    dependency.write_text('controller-installed new dependency')
                    if checks.fail:
                        checks.fail = False
                        raise OSError('synthetic failure after clean install')
                checks.fail = True
                def agent(cwd, prompt, directory, name, *args, **kwargs):
                    if name.startswith('worker'):
                        (project/'main.js').write_text('corrected')
                        return {'session': 'saved-session'}, 'done'
                    return {}, json.dumps({'approved': True, 'findings': []})
                with patch.object(adapter, 'initialize', side_effect=lambda *a: factory.read_json(runtime/'pilot.json')), \
                     patch.object(adapter, 'check_project', side_effect=checks) as checker, \
                     patch.object(adapter, 'acceptance', return_value=False), patch.object(adapter, 'agent', side_effect=agent):
                    with self.assertRaises(OSError):
                        adapter.run_ticket(ROOT, config, runtime, task, Mock())
                    result = adapter.run_ticket(ROOT, config, runtime, task, Mock(),
                                                verification_note='Operator repaired interrupted task' if operator_repair else None)
                self.assertEqual(result['status'], 'ready_for_publication')
                self.assertEqual(result['attempts'], 1 if operator_repair else 2)
                self.assertEqual(result['changed'], ['app/main.js'])
                self.assertEqual(checker.call_count, 2)

    def test_publication_revalidates_the_actual_combined_tree_before_any_github_call(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            repo = root/'repo'
            (repo/'app').mkdir(parents=True)
            (repo/'app/base.txt').write_text('changed integration base')
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
            subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                            'commit', '-qm', 'advanced base'], check=True)
            directory = root/'publication'
            directory.mkdir()
            expected = adapter.publication_tree(repo, 'HEAD', [], directory)
            with patch.object(adapter, 'check_project', side_effect=factory.FactoryError('Independent ticket acceptance failed')) as checker:
                with self.assertRaisesRegex(factory.FactoryError, 'Independent ticket acceptance failed'):
                    adapter.validate_publication(repo, {'working_directory': 'app'}, {'acceptance': 'zero'}, directory, expected,
                                                 factory.git_commit(repo))
            self.assertEqual((checker.call_args.args[0]/'base.txt').read_text(), 'changed integration base')
            self.assertFalse((directory/'base-validation.json').exists())

    def test_native_claude_start_does_not_forward_ambient_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime = Path(folder)
            (runtime/'claude-crew').mkdir()
            (runtime/'claude-crew/crew.json').write_text('{}')
            (runtime/'root-token').write_text('synthetic-local-token')
            adapter.save(runtime/'pilot.json', {'project_id':'project','config_sha256':'config','tasks':{'TASK':{'ticket':'ticket'}}})
            adapter.save(runtime/'claude-crew/initialized.json', {'project_id':'project','config_sha256':'config','tickets':{'TASK':'ticket'}})
            auth = Mock(stdout=json.dumps({'loggedIn': True, 'authMethod': 'claude.ai'}))
            secrets = {'GH_TOKEN': 'synthetic', 'AWS_SECRET_ACCESS_KEY': 'synthetic', 'ANTHROPIC_API_KEY': 'synthetic',
                       'UNRELATED_SERVICE_TOKEN': 'synthetic', 'CLAUDE_CONFIG_DIR': '/synthetic/claude'}
            with patch.dict(os.environ, secrets), patch.object(sys, 'argv', ['crew', '--runtime', str(runtime)]), \
                 patch.object(start_claude_crew, 'Tares') as api, \
                 patch.object(start_claude_crew.subprocess, 'run', return_value=auth) as run, \
                 patch.object(start_claude_crew.subprocess, 'call', return_value=0) as crew:
                api.return_value.call.side_effect = [{'tickets':[{'id':'ticket'}]}, {'autonomy': 'L3-review', 'releaser': False}]
                self.assertEqual(start_claude_crew.main(), 0)
            for invocation in [*run.call_args_list, crew.call_args]:
                if 'env' not in invocation.kwargs: continue
                env = invocation.kwargs['env']
                for name in secrets.keys()-{'CLAUDE_CONFIG_DIR'}:
                    self.assertNotIn(name, env)
                self.assertEqual(env['CLAUDE_CONFIG_DIR'], '/synthetic/claude')


if __name__ == '__main__': unittest.main()
