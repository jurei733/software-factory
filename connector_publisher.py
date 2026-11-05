"""Deterministic publication via the existing Codex GitHub connector; never starts a model turn."""
import json
import os
from pathlib import Path
import re
import select
import subprocess
import time
from urllib.parse import quote

import factory
from runtime_safety import runtime_path

GITHUB_APP = 'connector_76869538009648d5b282a4bb21c3d157'


class ConnectorError(RuntimeError):
    pass


def save(path, data):
    temporary = path.with_suffix('.new')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2)+'\n')
    temporary.replace(path)


class CodexConnector:
    """Only stdio MCP calls. No turn/start, shell tool, credential export, or project prompt."""
    METHODS = {'initialize', 'thread/start', 'mcpServerStatus/list', 'mcpServer/tool/call'}

    def __init__(self, directory, env):
        self.directory = runtime_path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.sequence = 0
        self.buffer = b''
        configured = subprocess.run(["codex", "mcp", "list", "--json"], env=env, capture_output=True, text=True, check=True, timeout=20)
        names = [entry["name"] for entry in json.loads(configured.stdout) if entry.get("enabled")]
        factory.require(all(re.fullmatch(r"[A-Za-z0-9_-]+", name) for name in names), "Unsupported configured MCP server name")
        overrides = [value for name in names for value in ("-c", 'mcp_servers.'+name+'={command="/usr/bin/false",enabled=false}')]
        self.stderr = (self.directory/'connector.stderr').open('a')
        self.process = subprocess.Popen(['codex', 'app-server', '--listen', 'stdio://', '--disable', 'plugins',
            '--disable', 'multi_agent', '--enable', 'apps',
            '-c', 'apps._default.enabled=false', '-c', 'apps.'+GITHUB_APP+'.enabled=true', *overrides],
            cwd=self.directory, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=self.stderr, start_new_session=True)
        try:
            self.rpc('initialize', {'clientInfo': {'name': 'factory-deterministic-publisher', 'version': '2'},
                                    'capabilities': {'experimentalApi': True}})
            self.send({'method': 'initialized'})
            thread = self.rpc('thread/start', {'cwd': str(self.directory), 'ephemeral': True,
                'approvalPolicy': 'on-request', 'permissions': ':read-only', 'environments': []})
            self.thread_id = thread['thread']['id']
            servers = self.rpc('mcpServerStatus/list', {})['data']
            factory.require(all(server['name'] == 'codex_apps' or not server.get('tools') for server in servers),
                            'An unrelated MCP server remained enabled; publisher stopped')
        except BaseException:
            self.close()
            raise

    def send(self, data):
        self.process.stdin.write((json.dumps(data)+'\n').encode())
        self.process.stdin.flush()

    def rpc(self, method, params):
        factory.require(method in self.METHODS, 'Publisher RPC method is not permitted')
        self.sequence += 1
        request_id = self.sequence
        self.send({'id': request_id, 'method': method, 'params': params})
        deadline = time.monotonic()+90
        while time.monotonic() < deadline:
            while b'\n' in self.buffer:
                line, self.buffer = self.buffer.split(b'\n', 1)
                message = json.loads(line)
                if message.get('id') == request_id and 'method' not in message:
                    if 'error' in message:
                        raise ConnectorError(str(message['error']))
                    return message['result']
                if 'id' in message and 'method' in message:
                    # Do not bypass interactive approvals, automatic rejection, or unexpected server requests.
                    self.send({'id': message['id'], 'error': {'code': -32601, 'message': 'Interactive request requires operator review'}})
                    raise ConnectorError('Connector requested interactive review: '+message['method'])
            if select.select([self.process.stdout], [], [], min(1, max(0, deadline-time.monotonic())))[0]:
                data = os.read(self.process.stdout.fileno(), 65536)
                if not data:
                    raise ConnectorError('Connector closed before its receipt')
                self.buffer += data
        raise ConnectorError('Connector deadline exceeded; preserve the publication journal')

    def call(self, tool, arguments):
        factory.require(tool in ('github.fetch', 'github.create_tree', 'github.create_commit',
                                  'github.create_branch', 'github.create_pull_request'), 'Publisher tool is not permitted')
        result = self.rpc('mcpServer/tool/call', {'threadId': self.thread_id, 'server': 'codex_apps',
                          'tool': tool, 'arguments': arguments})
        if result.get('isError'):
            raise ConnectorError(json.dumps(result))
        data = result.get('structuredContent')
        if isinstance(data, dict) and isinstance(data.get('content'), str):
            return json.loads(data['content'])
        factory.require(data is not None, 'Missing structured connector receipt')
        return data

    def close(self):
        factory.terminate_check(self.process)
        for stream in (self.process.stdin, self.process.stdout, self.stderr):
            stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def verify_commit(commit, expected_sha, tree, parent):
    factory.require(commit.get('sha') == expected_sha and commit.get('tree', {}).get('sha') == tree,
                    'Published commit/tree identity mismatch')
    parents = [entry.get('sha') for entry in commit.get('parents', [])]
    factory.require(parents == [parent], 'Published commit must have exactly the pinned base as its single parent')
    return parents


class Publisher:
    """The immutable plan defines every allowed repository, ref, path and mutation argument."""
    def __init__(self, transport, plan, directory):
        self.transport, self.plan = transport, plan
        self.directory = Path(directory)
        self.path = self.directory/'publisher-journal.json'
        self.journal = json.loads(self.path.read_text()) if self.path.exists() else {'plan': plan, 'receipts': []}
        factory.require(self.journal['plan'] == plan, 'Cannot rebind an existing publication transaction')
        factory.require(not self.journal.get('invalid_receipt'), 'Unexpected connector receipt requires operator inspection')
        factory.require(all(re.fullmatch(r'[0-9a-f]{40}', plan.get(key, '')) for key in ('base_sha', 'base_tree', 'tree')), 'Pin valid Git object identities')
        factory.require(len({file['path'] for file in plan['files']}) == len(plan['files']) and plan['files'], 'Empty or duplicate publication paths')
        repo = plan['repository']
        factory.require(re.fullmatch(r'[A-Za-z0-9-]+/[A-Za-z0-9_.-]+', repo), 'Unsafe publication repository')
        factory.require(re.fullmatch(r'feat/[a-z0-9-]+', plan['base_ref']) and
                        re.fullmatch(r'factory/[a-z0-9-]+', plan['branch']), 'Unsafe publication refs')
        for file in plan['files']:
            factory.require(factory.relative_path(file['path']) and
                            any(file['path'] == p or file['path'].startswith(p.rstrip('/')+'/') for p in plan['allowed_paths']),
                            'Publication path outside reviewed scope')
            content = file['content']
            if content is not None:
                raw = content.encode()
                import hashlib
                actual = hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()  # nosec B324 -- Git object identity
                factory.require(actual == file['git_blob_sha'], 'Publication content differs from reviewed blob')
        self.prefix = 'https://api.github.com/repos/'+repo

    def fetch(self, suffix):
        allowed = {'/branches/'+quote(self.plan['base_ref'], safe='/'), '/branches/'+quote(self.plan['branch'], safe='/')}
        if self.journal.get('commit'):
            allowed.add('/git/commits/'+self.journal['commit'])
        allowed.add('/pulls?state=all&head='+quote(self.plan['repository'].split('/')[0]+':'+self.plan['branch'], safe=':')+'&base='+quote(self.plan['base_ref'], safe=''))
        factory.require(suffix in allowed, 'Unplanned publisher read')
        return self.transport.call('github.fetch', {'url': self.prefix+suffix})

    def guard_base(self):
        base = self.fetch('/branches/'+quote(self.plan['base_ref'], safe='/'))
        factory.require(base['commit']['sha'] == self.plan['base_sha'] and
                        base['commit']['commit']['tree']['sha'] == self.plan['base_tree'],
                        'Publication base moved; revalidate against the new base before retrying')

    def mutation(self, step, tool, arguments):
        p, repo = self.plan, self.plan['repository']
        entries = [{'path': f['path'], 'mode': '100644', 'type': 'blob',
                    **({'sha': None} if f['content'] is None else {'content': f['content']})} for f in p['files']]
        expected = {
            'tree': ('github.create_tree', {'repository_full_name': repo, 'base_tree_sha': p['base_tree'], 'tree_elements': entries}),
            'commit': ('github.create_commit', {'repository_full_name': repo, 'tree_sha': p['tree'], 'parent_sha': p['base_sha'], 'message': p['title']}),
            'branch': ('github.create_branch', {'repository_full_name': repo, 'branch_name': p['branch'], 'sha': self.journal.get('commit')}),
            'pr': ('github.create_pull_request', {'repository_full_name': repo, 'base': p['base_ref'], 'head': p['branch'],
                                                'title': p['title'], 'body': p['body'], 'draft': False, 'maintainer_can_modify': False})}
        factory.require(step in expected and (tool, arguments) == expected[step], 'Unplanned publisher mutation/tool/arguments')
        order = ('tree', 'commit', 'branch', 'pr')
        factory.require(all(self.journal.get(key) for key in order[:order.index(step)]) and not self.journal.get(step),
                        'Publication mutation is out of sequence')
        if step in ('branch', 'pr'):
            factory.require(self.journal.get('verified_commit') == {'sha': self.journal.get('commit'), 'tree': p['tree'], 'parents': [p['base_sha']]}, 'Verify the single pinned parent before exposing the commit')
        if step == 'pr':
            head = self.branch_or_none()
            factory.require(head and head['commit']['sha'] == self.journal['commit'], 'Published branch moved before PR creation')
        self.guard_base()  # Every write is preceded by a fresh base check.
        self.journal['pending'] = {'step': step, 'tool': tool, 'arguments': arguments}
        save(self.path, self.journal)  # No write without a durable intent.
        receipt = self.transport.call(tool, arguments)
        self.journal['receipts'].append({'step': step, 'tool': tool, 'arguments': arguments, 'result': receipt})
        self.journal.pop('pending', None)
        save(self.path, self.journal)  # Retain raw receipts even when postflight rejects their identity.
        if step in ('tree', 'commit'):
            if not re.fullmatch(r'[0-9a-f]{40}', receipt.get('sha', '')) or (step == 'tree' and receipt['sha'] != p['tree']):
                self.journal['invalid_receipt'] = step
                save(self.path, self.journal)
                raise factory.FactoryError('Created Git object differs from approved identity; inspect retained receipt')
            self.journal[step] = receipt['sha']
        else:
            self.journal[step] = receipt
        save(self.path, self.journal)
        return receipt

    def branch_or_none(self):
        try:
            return self.fetch('/branches/'+quote(self.plan['branch'], safe='/'))
        except ConnectorError as error:
            if '404' in str(error) or 'not_found' in str(error).lower() or 'not found' in str(error).lower():
                return None
            raise

    def run(self):
        p = self.plan
        self.guard_base()
        existing = self.branch_or_none()
        if existing:
            factory.require(self.journal.get('commit') == existing['commit']['sha'], 'Existing branch is not this transaction; never overwrite')
            self.journal['branch'] = existing
            save(self.path, self.journal)
        if not self.journal.get('tree'):
            self.mutation('tree', 'github.create_tree', {'repository_full_name': p['repository'], 'base_tree_sha': p['base_tree'],
                'tree_elements': [{'path': f['path'], 'mode': '100644', 'type': 'blob',
                    **({'sha': None} if f['content'] is None else {'content': f['content']})} for f in p['files']]})
        if not self.journal.get('commit'):
            self.mutation('commit', 'github.create_commit', {'repository_full_name': p['repository'], 'tree_sha': p['tree'],
                'parent_sha': p['base_sha'], 'message': p['title']})
        commit = self.fetch('/git/commits/'+self.journal['commit'])
        parents = verify_commit(commit, self.journal['commit'], p['tree'], p['base_sha'])
        self.journal['verified_commit'] = {'sha': self.journal['commit'], 'tree': p['tree'], 'parents': parents}
        save(self.path, self.journal)
        if not self.journal.get('branch'):
            # Reconcile an interrupted branch call before ever attempting another creation.
            existing = self.branch_or_none()
            if existing:
                factory.require(existing['commit']['sha'] == self.journal['commit'], 'Branch collision; never overwrite')
                self.journal['branch'] = existing
                save(self.path, self.journal)
            else:
                self.mutation('branch', 'github.create_branch', {'repository_full_name': p['repository'],
                    'branch_name': p['branch'], 'sha': self.journal['commit']})
        if not self.journal.get('pr'):
            suffix = '/pulls?state=all&head='+quote(p['repository'].split('/')[0]+':'+p['branch'], safe=':')+'&base='+quote(p['base_ref'], safe='')
            existing_prs = self.fetch(suffix)
            factory.require(isinstance(existing_prs, list) and len(existing_prs) <= 1, 'Ambiguous existing pull request')
            if existing_prs:
                pr = existing_prs[0]
                factory.require(pr.get('head', {}).get('repo', {}).get('full_name') == p['repository'] and pr.get('base', {}).get('repo', {}).get('full_name') == p['repository'], 'Existing PR belongs to a different repository')
                self.journal['pr'] = {'url': pr['html_url'], 'base': pr['base']['ref'], 'head': pr['head']['ref'],
                                       'head_sha': pr['head']['sha']}
                save(self.path, self.journal)
            else:
                self.mutation('pr', 'github.create_pull_request', {'repository_full_name': p['repository'], 'base': p['base_ref'],
                    'head': p['branch'], 'title': p['title'], 'body': p['body'], 'draft': False, 'maintainer_can_modify': False})
        remote = self.branch_or_none()
        factory.require(remote and remote['commit']['sha'] == self.journal['commit'], 'Published head moved')
        verify_commit(remote['commit'], self.journal['commit'], p['tree'], p['base_sha']) if 'tree' in remote['commit'] else verify_commit(
            self.fetch('/git/commits/'+self.journal['commit']), self.journal['commit'], p['tree'], p['base_sha'])
        self.guard_base()
        pr = self.journal['pr']
        factory.require(pr.get('base') == p['base_ref'] and pr.get('head') == p['branch'] and pr.get('head_sha') == self.journal['commit'] and
            re.fullmatch(r'https://github.com/'+re.escape(p['repository'])+r'/pull/[0-9]+', pr.get('url', '')), 'PR identity mismatch')
        receipt = {'url': pr['url'], 'head': self.journal['commit'], 'tree': p['tree'], 'parents': parents,
                   'base': p['base_sha'], 'base_ref': p['base_ref'], 'branch': p['branch'], 'publication_verified': True,
                   'source': 'deterministic GitHub MCP calls; no model turn', 'merge_allowed': False,
                   'freshness': 'Base checked before every write and after publication; recheck CI/base before human merge'}
        save(self.directory/'github-receipt.json', receipt)
        return receipt
