"""Real named-profile tests; no model calls, login changes, or provider API usage."""
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from tares_adapter import permission_args


@unittest.skipUnless(os.environ.get('FACTORY_CODEX_TESTS')=='1' and shutil.which('codex'), 'Enable FACTORY_CODEX_TESTS for the installed Codex sandbox')
class CodexBoundaries(unittest.TestCase):
    def test_exact_worker_and_reviewer_profiles_deny_sibling_reads_and_network(self):
        parent=ROOT.parent/'.factory-codex-tests';parent.mkdir(exist_ok=True)
        try:
            with tempfile.TemporaryDirectory(dir=parent) as folder:
                root=Path(folder);project=root/'source';project.mkdir();scratch=root/'scratch';scratch.mkdir()
                (project/'editable').write_text('original')
                sibling=root/'sibling';sibling.write_text('synthetic private marker')
                node=shutil.which('node')
                with socket.socket() as listener:
                    listener.bind(('127.0.0.1',0));listener.listen(1);port=listener.getsockname()[1]
                    for writable in (False,True):
                        argv=['codex','sandbox','-P','factory_agent','-C',str(project),*permission_args(project,scratch,writable=writable,source_root=project,allowed_paths=[project/'editable']),'--']
                        probe="""const fs=require('node:fs'),net=require('node:net');
const [sibling,scratch,project,writable,port]=process.argv.slice(1);
function denied(f){try{f()}catch{return}throw Error('access escaped')}
denied(()=>fs.readFileSync(sibling));denied(()=>fs.writeFileSync(sibling,'escape'));
fs.writeFileSync(scratch+'/probe','allowed');fs.unlinkSync(scratch+'/probe');
denied(()=>fs.writeFileSync(project+'/probe','escape'));denied(()=>fs.mkdirSync(project+'/.git'));
if(writable==='true'){fs.writeFileSync(project+'/editable','allowed')}else denied(()=>fs.writeFileSync(project+'/editable','escape'));
const s=net.createConnection({host:'127.0.0.1',port:Number(port)});s.setTimeout(1000,()=>s.destroy());s.on('error',()=>{});s.on('connect',()=>{s.destroy();process.exitCode=1});
"""
                        helper=subprocess.run(argv+[shutil.which('codex'),'--version'],capture_output=True,text=True,timeout=15)
                        self.assertEqual(helper.returncode,0,helper.stderr)
                        result=subprocess.run(argv+['/usr/bin/env','OPENSSL_CONF=/dev/null',node,'-e',probe,str(sibling),str(scratch),str(project),str(writable).lower(),str(port)],capture_output=True,text=True,timeout=15)
                        self.assertEqual(result.returncode,0,result.stderr)
                        self.assertEqual(sibling.read_text(),'synthetic private marker')
        finally:
            try:parent.rmdir()
            except OSError:pass


if __name__=='__main__':unittest.main()
