import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from zipfile import ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import runtime_backup
import stage_release
import require_workflows
import promote_release


class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(); self.addCleanup(self.folder.cleanup)
        self.base = Path(self.folder.name)

    def test_backup_restore_preserves_private_state_budgets_and_does_not_restore_process_authority(self):
        runtime = self.base / "runtime"; (runtime / "data").mkdir(parents=True)
        (runtime / "root-token").write_text("synthetic-test-only")
        (runtime / "pilot.json").write_text('{"tasks":{"T":{"status":"published","attempts":2}}}')
        (runtime / "daemon.json").write_text('{"pid":999999}')
        (runtime / "data/ledger").write_bytes(b"synthetic state")
        archive = self.base / "backup.zip"
        receipt = runtime_backup.backup(runtime, archive)
        self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
        restored = self.base / "restored"
        runtime_backup.restore(archive, restored, receipt["sha256"])
        self.assertEqual((restored / "root-token").read_text(), "synthetic-test-only")
        self.assertEqual((restored / "root-token").stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads((restored / "pilot.json").read_text())["tasks"]["T"]["attempts"], 2)
        self.assertFalse((restored / "daemon.json").exists())
        with self.assertRaises(ValueError):
            runtime_backup.restore(archive, self.base / "wrong", "0" * 64)
        self.assertFalse((self.base / "wrong").exists())
        with self.assertRaises(ValueError):
            runtime_backup.restore(archive, restored, receipt["sha256"])

    def test_live_or_unfinished_runtime_and_links_are_rejected(self):
        runtime = self.base / "runtime"; runtime.mkdir()
        (runtime / "daemon.json").write_text('{}')
        with patch.object(runtime_backup, "owns_process", return_value=True), self.assertRaisesRegex(ValueError, "Stop"):
            runtime_backup.backup(runtime, self.base / "live.zip")
        (runtime / "daemon.json").unlink()
        (runtime / "pilot.json").write_text('{"tasks":{"T":{"status":"coding","attempts":1}}}')
        with self.assertRaisesRegex(ValueError, "active tasks"):
            runtime_backup.backup(runtime, self.base / "active.zip")
        (runtime / "pilot.json").unlink(); (runtime / "link").symlink_to(ROOT / "README.md")
        with self.assertRaisesRegex(ValueError, "unsafe link"):
            runtime_backup.backup(runtime, self.base / "links.zip")

    def test_restore_and_distribution_reject_traversal_and_symlink_members_before_extraction(self):
        archive = self.base / "malicious.zip"
        data = b"unsafe"
        with ZipFile(archive, "w") as package:
            package.writestr("runtime/../escaped", data)
            package.writestr("manifest.json", json.dumps({"schema_version": 1, "files": {"../escaped": hashlib.sha256(data).hexdigest()}}))
        with self.assertRaisesRegex(ValueError, "Unsafe backup member"):
            runtime_backup.restore(archive, self.base / "destination", hashlib.sha256(archive.read_bytes()).hexdigest())
        self.assertFalse((self.base / "destination").exists()); self.assertFalse((self.base / "escaped").exists())
        for name, mode in [("software-factory/../outside", 0o100644), ("software-factory/link", 0o120777)]:
            memory = io.BytesIO()
            with ZipFile(memory, "w") as package:
                info = ZipInfo(name); info.external_attr = mode << 16; package.writestr(info, b"outside")
            with ZipFile(io.BytesIO(memory.getvalue())) as package, self.assertRaisesRegex(ValueError, "Unsafe distribution"):
                stage_release.members(package)

    def test_other_workflows_must_match_source_branch_and_complete_successfully(self):
        identity = {"factory_commit": "a" * 40}
        runs = [{"id": i, "path": ".github/workflows/" + name, "head_sha": "a" * 40,
                 "head_branch": "trunk", "status": "completed", "conclusion": "success"}
                for i, name in enumerate(("repository.yml", "agent-pilot.yml"), 1)]
        api = Mock(); api.request.return_value = {"workflow_runs": runs}
        self.assertEqual(len(require_workflows.require(api, identity, branch="trunk", timeout=0)), 2)
        runs[0]["conclusion"] = "skipped"
        with self.assertRaisesRegex(ValueError, "did not pass"):
            require_workflows.require(api, identity, branch="trunk", timeout=0)
        runs[0]["conclusion"] = "success"; runs[0]["head_sha"] = "b" * 40
        with self.assertRaises(TimeoutError):
            require_workflows.require(api, identity, branch="trunk", timeout=0)

    def test_release_refuses_unprotected_or_moved_source_before_download_or_write(self):
        env = {"GITHUB_REPOSITORY": "example-owner/factory", "GITHUB_REF": "refs/heads/main",
               "GITHUB_SHA": "a"*40, "GITHUB_RUN_ID": "1", "GITHUB_RUN_ATTEMPT": "1", "GH_TOKEN": "synthetic"}
        for branch in ({"protected": False, "commit": {"sha": "a"*40}}, {"protected": True, "commit": {"sha": "b"*40}}):
            api = Mock(); api.request.return_value = branch
            with patch.object(promote_release, "GitHub", return_value=api), patch.object(promote_release.subprocess, "run") as run:
                with self.assertRaisesRegex(ValueError, "current protected"):
                    promote_release.promote("ci-1-1-factory", "0.1.0", env, publish=True)
                run.assert_not_called()
                self.assertTrue(all(call.args[0] == "GET" for call in api.request.call_args_list))


if __name__ == "__main__":
    unittest.main()
