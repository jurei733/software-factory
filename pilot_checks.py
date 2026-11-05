"""Trusted pilot checks: verified dependency data, then offline disposable containers only."""
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import uuid
from urllib.request import Request, build_opener, HTTPRedirectHandler

import factory
from isolated_runner import container_command, file_manifest
from runtime_safety import runtime_path

ROOT = Path(__file__).resolve().parent
INTEGRITIES = {
    '1.1.0': 'sha512-TQ92mBOW0l3LeMeyLV6mzy/kWr8lkd/hp3mTg7wYK7zJhuBStmGMBG0BdeDZS/dZx1IukaX6Bk11zcln25o1Aw==',
    '1.1.1': 'sha512-xceH2snhtb5M9liqDsmEw56le376mTZkEX/jEb/RxNFyegNul7eNslCXP9FDj/Lcu0X8KEyMceP2ntpaHrDEVA=='}
ACCEPTANCE = {
    'zero': "import{summary}from'./src/summary.js';if(summary(0).completed!==0||summary(3).completed!==3)process.exit(1);for(const x of [-1,1.5,null,'0']){let threw=false;try{summary(x)}catch{threw=true}if(!threw)process.exit(1)}",
    'json': "import{spawnSync}from'node:child_process';const env={...process.env,NO_COLOR:'1'};delete env.FORCE_COLOR;for(const [args,want]of [[[],'Completed: 3; status: ready\\n'],[['--json'],'{\"completed\":3,\"status\":\"ready\"}\\n']]){const r=spawnSync(process.execPath,['src/cli.js',...args],{encoding:'utf8',env});if(r.status!==0||r.stderr!==''||r.stdout!==want)process.exit(1)}",
    'dependency': "import{readFileSync as r}from'node:fs';const p=JSON.parse(r('package.json'));const l=JSON.parse(r('package-lock.json'));if(p.dependencies.picocolors!=='1.1.1'||l.packages['node_modules/picocolors'].version!=='1.1.1')process.exit(1)"}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('Approved package and advisory requests never follow redirects')


def tarball(version):
    factory.require(version in INTEGRITIES, 'Unapproved dependency version')
    return 'https://registry.npmjs.org/picocolors/-/picocolors-' + version + '.tgz'


def verify_tarball(version, data):
    actual = 'sha512-' + base64.b64encode(hashlib.sha512(data).digest()).decode()
    factory.require(actual == INTEGRITIES[version], 'Dependency tarball integrity mismatch')


def dependency_contract(project):
    """Data only, before npm or any worker-controlled code executes, even in Docker."""
    project = Path(project)
    for name in ('package.json', 'package-lock.json'):
        factory.require((project/name).is_file() and not (project/name).is_symlink(), 'Package metadata must be regular files')
    package = factory.read_json(project/'package.json')
    lock = factory.read_json(project/'package-lock.json')
    factory.require(set(package) <= {'name', 'version', 'private', 'type', 'scripts', 'dependencies'}, 'Unexpected package metadata')
    version = package.get('dependencies', {}).get('picocolors')
    factory.require(version in INTEGRITIES, 'Only the two pinned pilot dependencies are approved')
    factory.require(package.get('dependencies') == {'picocolors': version} and
                    not any(package.get(k) for k in ('devDependencies', 'optionalDependencies', 'peerDependencies', 'overrides', 'workspaces')),
                    'Unexpected dependency metadata')
    factory.require(lock.get('lockfileVersion') == 3 and set(lock.get('packages', {})) == {'', 'node_modules/picocolors'}, 'Unexpected lockfile graph')
    root = lock['packages']['']
    factory.require(root.get('dependencies') == {'picocolors': version} and
                    not any(root.get(k) for k in ('devDependencies', 'optionalDependencies', 'peerDependencies')),
                    'Lockfile root differs from approved dependency graph')
    entry = lock['packages']['node_modules/picocolors']
    factory.require(set(entry) == {'version', 'resolved', 'integrity', 'license'} and entry.get('license') == 'ISC' and
                    entry.get('version') == version and entry.get('resolved') == tarball(version) and
                    entry.get('integrity') == INTEGRITIES[version], 'Unapproved dependency URL, integrity or package metadata')
    factory.require(not any(lock.get(k) for k in ('dependencies', 'overrides', 'workspaces')), 'Unexpected legacy dependency metadata')
    return version


def ensure_image(runtime):
    root = runtime_path(runtime)/'toolchain'
    root.mkdir(parents=True, exist_ok=True)
    dockerfile = (ROOT/'Dockerfile.tares-checks').read_bytes()
    digest = factory.digest({'dockerfile': hashlib.sha256(dockerfile).hexdigest(), 'packages': INTEGRITIES})
    receipt = root/'image.json'
    if receipt.exists():
        saved = factory.read_json(receipt)
        if saved.get('spec_sha256') == digest:
            subprocess.run(['docker', 'image', 'inspect', saved['image']], capture_output=True, check=True, timeout=10)
            return saved['image']
    with tempfile.TemporaryDirectory(prefix='build-', dir=root) as folder:
        context = Path(folder)
        (context/'Dockerfile').write_bytes(dockerfile)
        for version in INTEGRITIES:
            request = Request(tarball(version), headers={'User-Agent': 'factory-pilot-checks'})
            with build_opener(NoRedirect()).open(request, timeout=30) as response:
                data = response.read(1024*1024+1)
            factory.require(len(data) <= 1024*1024, 'Package exceeds pilot size limit')
            verify_tarball(version, data)
            (context/('picocolors-'+version+'.tgz')).write_bytes(data)
        with (root/'build.log').open('a') as log:
            subprocess.run(['docker', 'build', '--network=none', '--iidfile', str(context/'iid'), str(context)],
                           stdout=log, stderr=log, check=True, timeout=180)
        image = (context/'iid').read_text().strip()
    factory.require(image.startswith('sha256:') and len(image) == 71, 'Missing immutable image identity')
    temporary = receipt.with_suffix('.new')
    temporary.write_text(json.dumps({'spec_sha256': digest, 'image': image})+'\n')
    temporary.replace(receipt)
    return image


def advisory_check(version):
    request = Request('https://registry.npmjs.org/-/npm/v1/security/advisories/bulk',
                      data=json.dumps({'picocolors': [version]}).encode(),
                      headers={'Content-Type': 'application/json', 'User-Agent': 'factory-pilot-checks'}, method='POST')
    with build_opener(NoRedirect()).open(request, timeout=20) as response:
        data = json.loads(response.read(1024*1024))
    factory.require(isinstance(data, dict), 'Invalid advisory response')
    factory.require(not any(item.get('severity') in ('high', 'critical') for items in data.values() for item in items),
                    'High/critical dependency advisory')
    return data


def run_project(project, name, runtime, log_path, seconds=240, *, full=True):
    """No project command, import, hook or dependency install ever runs on the host."""
    runtime = runtime_path(runtime)
    version = dependency_contract(project)
    image = ensure_image(runtime)
    project, log_path = Path(project).resolve(), Path(log_path)
    deadline = time.monotonic() + max(1, seconds)
    name_in_docker = 'factory-pilot-' + uuid.uuid4().hex
    manifest = file_manifest(project)
    with tempfile.TemporaryDirectory(prefix='pilot-check-', dir=runtime) as folder, log_path.open('a') as log:
        source = Path(folder)/'source'
        source.mkdir()
        for path, entry in manifest.items():
            if Path(path).parts[0] in ('node_modules', 'dist', '.git'):
                continue
            factory.require(entry[0] != 'symlink', 'Pilot checks reject source symlinks')
            target = source/path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(project/path, target)
            target.chmod(0o644)
        # Check the actual copied metadata as well; the controller holds the worker lock.
        dependency_contract(source)
        writable = [source/'node_modules', source/'dist']
        for path in writable:
            path.mkdir()
        def execute(argv, limit=120):
            remaining = deadline-time.monotonic()
            factory.require(remaining > 0, 'Isolated check budget exhausted')
            return subprocess.run(['docker', 'exec', name_in_docker, *argv], stdout=log, stderr=log,
                                  timeout=min(limit, remaining), check=False).returncode
        started = False
        try:
            subprocess.run(container_command(name_in_docker, image, source, writable),
                           stdout=log, stderr=log, timeout=min(30, max(1, deadline-time.monotonic())), check=True)
            started = True
            factory.require(execute(['/bin/sh', '-c', 'cp -R /opt/factory-deps/npm-cache /tmp/npm-cache && touch /tmp/npm-global-empty']) == 0, 'Cannot prepare isolated cache')
            npm = ['env', 'NPM_CONFIG_USERCONFIG=/dev/null', 'NPM_CONFIG_GLOBALCONFIG=/tmp/npm-global-empty',
                   'NPM_CONFIG_CACHE=/tmp/npm-cache', 'npm']
            commands = [npm+['ci', '--offline', '--ignore-scripts', '--no-audit', '--no-fund']]
            if full:
                commands += [npm+['run', 'lint'], npm+['test'], npm+['run', 'build']]
            for argv in commands:
                factory.require(execute(argv) == 0, 'Isolated project check failed; inspect '+str(log_path))
            passed = execute(['node', '--input-type=module', '-e', ACCEPTANCE[name]], 30) == 0
        finally:
            cleanup = subprocess.run(['docker', 'rm', '--force', name_in_docker], capture_output=True, text=True, timeout=30)
            if started:
                factory.require(cleanup.returncode == 0, 'Isolated check container cleanup failed')
    advisories = advisory_check(version) if full else None
    return {'passed': passed, 'image': image, 'network': 'none', 'host_credentials': False,
            'dependency': {'version': version, 'resolved': tarball(version), 'integrity': INTEGRITIES[version]},
            'advisories': advisories}
