"""Manage identified loopback services. Status is read-only and checks daemon plus MCP."""
import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
from urllib.request import Request, build_opener
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime_safety import runtime_path, process_identity, capture_identity, owns_process, read_identity, stop_owned
from tares_adapter import Tares, NoRedirect, environment, save


def endpoints(root):
    path = root / 'services.json'
    return json.loads(path.read_text()) if path.exists() else {'api_port': 18787, 'mcp_port': 18788}


def mcp_health(root, port):
    request = Request('http://127.0.0.1:' + str(port) + '/mcp', method='POST',
                      data=json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
                          'protocolVersion': '2025-03-26', 'capabilities': {},
                          'clientInfo': {'name': 'factory-health', 'version': '1'}}}).encode(),
                      headers={'Authorization': 'Bearer ' + (root/'root-token').read_text().strip(),
                               'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream'})
    with build_opener(NoRedirect()).open(request, timeout=3) as response:
        text = response.read(65536).decode()
        messages = [json.loads(line[6:]) for line in text.splitlines() if line.startswith('data: ')] if text.startswith('event:') else [json.loads(text)]
        return any(m.get('id') == 1 and m.get('result', {}).get('protocolVersion') and m['result'].get('serverInfo') for m in messages)


def status(root, ports):
    result = {'daemon': False, 'mcp': False, 'api_healthy': False, 'mcp_healthy': False}
    for name in ('daemon', 'mcp'):
        result[name] = bool(owns_process(read_identity(root/(name+'.pid')), root))
    if (root/'root-token').is_file():
        try:
            health = Tares(root, 'http://127.0.0.1:' + str(ports['api_port'])).call('GET', '/health')
            result['api_healthy'] = health.get('status') in ('ok', 'healthy')
        except (ValueError, OSError):
            pass
        try:
            result['mcp_healthy'] = bool(mcp_health(root, ports['mcp_port']))
        except (ValueError, OSError):
            pass
    result['status'] = 'ok' if all(result.values()) else 'unhealthy'
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('start', 'stop', 'status'))
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--tares', default='tares')
    parser.add_argument('--api-port', type=int, default=18787)
    parser.add_argument('--mcp-port', type=int, default=18788)
    args = parser.parse_args()
    root = runtime_path(args.runtime)  # Before mkdir, chmod, token, logs or pidfiles.
    ports = endpoints(root)
    if args.command == 'status':
        report = status(root, ports)
        print(json.dumps(report))
        return 0 if report['status'] == 'ok' else 1
    if args.command == 'stop':
        for name in ('mcp', 'daemon'):
            path = root/(name+'.pid')
            stop_owned(read_identity(path), root)
            path.unlink(missing_ok=True)
        print('Identified pilot services stopped; stale identities discarded')
        return 0
    if not all(1024 <= p <= 65535 for p in (args.api_port, args.mcp_port)) or args.api_port == args.mcp_port:
        raise ValueError('Select distinct unprivileged loopback ports')
    selected = {'api_port': args.api_port, 'mcp_port': args.mcp_port}
    if (root/'services.json').exists() and ports != selected:
        raise ValueError('Runtime ports are pinned; stop and use a new runtime for other ports')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    token = root/'root-token'
    if not token.exists():
        fd = os.open(token, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(secrets.token_urlsafe(32))
    save(root/'services.json', selected)
    commands = [('daemon', ['up', '--host', '127.0.0.1', '--port', str(args.api_port), '--data-dir', str(root/'data')]),
                ('mcp', ['mcp', '--host', '127.0.0.1', '--port', str(args.mcp_port), '--taresd', 'http://127.0.0.1:' + str(args.api_port)])]
    created = []
    try:
        for name, command in commands:
            pidfile = root/(name+'.pid')
            if not owns_process(read_identity(pidfile), root):
                env = environment()
                env.update(TARES_HOME=str(root/'data'), TARES_AUTH_TOKEN=token.read_text().strip(), OTEL_SDK_DISABLED='true')
                executable = str(Path(args.tares).resolve()) if '/' in args.tares else args.tares
                with (root/(name+'.log')).open('a') as log:
                    os.chmod(log.name, 0o600)
                    proc = subprocess.Popen([executable, *command], cwd=root, env=env, stdin=subprocess.DEVNULL,
                                            stdout=log, stderr=log, start_new_session=True)
                created.append((proc, pidfile))
                identity = capture_identity(proc.pid)
                if not identity or identity['pgid'] != proc.pid:
                    raise RuntimeError('Cannot record isolated service identity')
                save(pidfile, dict(identity, runtime=str(root)))
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                health = status(root, selected)
                if health['daemon'] and health['api_healthy'] and (name == 'daemon' or health['status'] == 'ok'):
                    break
                time.sleep(0.25)
            else:
                raise RuntimeError(name + ' failed readiness checks; inspect its private runtime log')
        print(json.dumps(status(root, selected)))
        return 0
    except BaseException:
        for proc, path in reversed(created):
            # We still own the Popen handle even if persisting the identity failed.
            import factory
            factory.terminate_check(proc)
            path.unlink(missing_ok=True)
        raise


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as error:
        print('ERROR: ' + str(error), file=sys.stderr)
        sys.exit(1)
