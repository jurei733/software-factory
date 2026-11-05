"""Real offline Node-container acceptance tests, enabled explicitly in the isolation CI job."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import pilot_checks
import factory
from isolated_runner import file_manifest


@unittest.skipUnless(os.environ.get('FACTORY_DOCKER_TESTS')=='1','Set FACTORY_DOCKER_TESTS=1 for real Docker boundaries')
class PilotIsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from runtime_safety import runtime_path
        cls.parent=runtime_path(ROOT.parent/'.factory-test-runtime')
        cls.parent.mkdir(exist_ok=True,mode=0o700)
        cls.temporary=tempfile.TemporaryDirectory(prefix='pilot-boundaries-',dir=cls.parent)
        cls.runtime=Path(cls.temporary.name)
        cls.image=pilot_checks.ensure_image(cls.runtime)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()
        try:cls.parent.rmdir()
        except OSError:pass

    def project(self,name='tares-pilot'):
        directory=Path(tempfile.mkdtemp(dir=self.runtime))/'app'
        shutil.copytree(ROOT/('examples/'+name),directory,ignore=shutil.ignore_patterns('node_modules','dist'))
        return directory

    def run_checks(self,project,name,full=False):
        with patch.object(pilot_checks,'ensure_image',return_value=self.image):
            return pilot_checks.run_project(project,name,self.runtime,self.runtime/'checks.log',full=full)

    def test_fresh_versioned_baseline_is_unsolved_and_completed_reference_passes(self):
        baseline=self.project('tares-baseline/v1');reference=self.project()
        config=factory.read_json(ROOT/'config/tares-pilot.json')
        self.assertEqual(factory.digest(file_manifest(baseline)),config['baseline_sha256'])
        for name in ('zero','json','dependency'):
            self.assertFalse(self.run_checks(baseline,name)['passed'])
            self.assertTrue(self.run_checks(reference,name,full=name=='zero')['passed'])

    def test_changed_acceptance_module_cannot_write_host_files(self):
        project=self.project();marker=self.runtime/'host-marker';marker.write_text('unchanged')
        summary=project/'src/summary.js'
        summary.write_text("import{writeFileSync}from'node:fs';try{writeFileSync("+json.dumps(str(marker))+",'escaped')}catch{}\n"+summary.read_text())
        self.assertTrue(self.run_checks(project,'zero')['passed'])
        self.assertEqual(marker.read_text(),'unchanged')

    def test_worker_controlled_build_has_no_host_credentials_network_socket_or_source_writes(self):
        project=self.project();marker=self.runtime/'host-private';marker.write_text('synthetic')
        script="""import fs from 'node:fs';import net from 'node:net';
function denied(f){try{f()}catch{return}throw Error('boundary escaped')}
denied(()=>fs.readFileSync(HOST));denied(()=>fs.writeFileSync(HOST,'escape'));
denied(()=>fs.writeFileSync('src/summary.js','escape'));
if(fs.existsSync('/var/run/docker.sock')||process.env.FACTORY_SYNTHETIC_SECRET)throw Error('host context leaked');
await new Promise((resolve,reject)=>{const s=net.createConnection({host:'192.0.2.1',port:80});s.setTimeout(500,()=>{s.destroy();resolve()});s.on('error',resolve);s.on('connect',()=>{s.destroy();reject(Error('network enabled'))})});
fs.writeFileSync('dist/safe.txt','scratch works');
""".replace('HOST',json.dumps(str(marker)))
        (project/'scripts/boundary.js').write_text(script)
        p=json.loads((project/'package.json').read_text());p['scripts']['build']='node scripts/boundary.js';(project/'package.json').write_text(json.dumps(p))
        original=(project/'src/summary.js').read_text()
        with patch.dict(os.environ,{'FACTORY_SYNTHETIC_SECRET':'synthetic'}):
            result=self.run_checks(project,'zero',full=True)
        self.assertTrue(result['passed']);self.assertEqual(result['network'],'none');self.assertFalse(result['host_credentials'])
        self.assertEqual(marker.read_text(),'synthetic');self.assertEqual((project/'src/summary.js').read_text(),original)
        self.assertFalse((project/'node_modules').exists());self.assertFalse((project/'dist').exists())

    def test_json_only_cli_fails_independent_default_mode_acceptance(self):
        project=self.project();(project/'src/cli.js').write_text("console.log(JSON.stringify({completed:3,status:'ready'}));\n")
        self.assertFalse(self.run_checks(project,'json')['passed'])

    def test_project_failure_removes_its_container(self):
        project=self.project();p=json.loads((project/'package.json').read_text());p['scripts']['lint']='exit 7';(project/'package.json').write_text(json.dumps(p))
        def containers():return subprocess.check_output(['docker','ps','-aq','--filter','name=factory-pilot-'],text=True).split()
        before=containers()
        with self.assertRaises(factory.FactoryError):self.run_checks(project,'zero',full=True)
        self.assertEqual(containers(),before)


if __name__=='__main__':unittest.main()
