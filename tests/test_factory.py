import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import factory


class FactoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / "README.md").write_text("Demo-Regeln", encoding="utf-8")
        self.config = {
            "schema_version": 1, "project": "demo", "reviewed": True,
            "rules_files": ["README.md"],
            "steps": [{"id": category, "category": category, "argv": [sys.executable, "-c", "pass"],
                       "cwd": ".", "timeout_seconds": 5} for category in ("lint", "test", "build")]
        }
        self.task = {
            "id": "TEST-1", "title": "Prüfen", "goal": "Demo prüfen", "acceptance": ["Prüfung grün"],
            "out_of_scope": ["Deployment"], "allowed_paths": ["src"], "risk": "low",
            "data_class": "synthetic", "max_attempts": 1, "max_minutes": 5
        }

    def run_report(self):
        report = self.root / "report.json"
        code = factory._run_host_checks(self.root, self.config, self.task, report)
        return code, json.loads(report.read_text())

    def test_failed_command_stops_following_commands(self):
        self.config["steps"][0]["argv"] = [sys.executable, "-c", "raise SystemExit(7)"]
        self.config["steps"][1]["argv"] = [sys.executable, "-c", "open('unexpected', 'w').close()"]
        code, report = self.run_report()
        self.assertEqual(code, 1)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["steps"][0]["returncode"], 7)
        self.assertEqual(report["steps"][1]["status"], "skipped")
        self.assertFalse((self.root / "unexpected").exists())

    def test_success_report_binds_task_and_configuration(self):
        code, report = self.run_report()
        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["config_sha256"], factory.digest(self.config))
        self.assertEqual(report["task_sha256"], factory.digest(self.task))
        self.assertTrue(all(x["status"] == "passed" for x in report["steps"]))

    def test_missing_executable_is_failed_not_passed(self):
        self.config["steps"][0]["argv"] = [str(self.root / "absent-program")]
        code, report = self.run_report()
        self.assertEqual(code, 1)
        self.assertEqual(report["steps"][0]["status"], "error")
        self.assertEqual(report["steps"][1]["status"], "skipped")

    def test_timeout_is_failed_and_skips_following_checks(self):
        self.config["steps"][0].update(argv=[sys.executable, "-c", "import time; time.sleep(5)"], timeout_seconds=1)
        code, report = self.run_report()
        self.assertEqual(code, 1)
        self.assertEqual(report["steps"][0]["status"], "timeout")
        self.assertEqual(report["steps"][1]["status"], "skipped")

    def test_interrupt_terminates_child_and_writes_cancelled_report(self):
        process = Mock(pid=12345)
        process.poll.return_value = None
        process.wait.side_effect = [KeyboardInterrupt, 0]
        with patch("factory.git_commit", return_value=None), patch("factory.subprocess.Popen", return_value=process), \
                patch("factory.os.killpg") as killpg:
            code, report = self.run_report()
        self.assertEqual(code, 130)
        self.assertEqual(report["status"], "cancelled")
        self.assertEqual(report["steps"][0]["status"], "cancelled")
        self.assertEqual(report["steps"][1]["status"], "skipped")
        if factory.os.name == "posix":
            killpg.assert_called_once_with(process.pid, factory.signal.SIGKILL)
        self.assertEqual(process.wait.call_count, 2)

    def test_existing_report_prevents_execution(self):
        report = self.root / "report.json"
        report.write_text("original", encoding="utf-8")
        self.config["steps"][0]["argv"] = [sys.executable, "-c", "open('unexpected', 'w').close()"]
        with self.assertRaises(FileExistsError):
            factory._run_host_checks(self.root, self.config, self.task, report)
        self.assertEqual(report.read_text(), "original")
        self.assertFalse((self.root / "unexpected").exists())

    def test_unreviewed_configuration_cannot_execute(self):
        self.config["reviewed"] = False
        with self.assertRaises(factory.FactoryError):
            self.run_report()
        self.assertFalse((self.root / "report.json").exists())

    def test_missing_npm_script_is_detected_before_execution(self):
        (self.root / "package.json").write_text('{"scripts": {"lint": "eslint"}}')
        self.config["steps"][0]["argv"] = ["npm", "run", "absent"]
        with self.assertRaisesRegex(factory.FactoryError, "npm-Script fehlt"):
            self.run_report()

    def test_cwd_cannot_escape_repository(self):
        self.config["steps"][0]["cwd"] = "../other"
        with self.assertRaises(factory.FactoryError):
            self.run_report()

    def test_symlink_cwd_cannot_escape_repository(self):
        with tempfile.TemporaryDirectory() as outside:
            (self.root / "outside").symlink_to(outside, target_is_directory=True)
            self.config["steps"][0]["cwd"] = "outside"
            with self.assertRaises(factory.FactoryError):
                self.run_report()

    def test_incomplete_or_restricted_task_is_rejected(self):
        for field, value in (("acceptance", []), ("data_class", "restricted"), ("allowed_paths", ["../private"]),
                             ("allowed_paths", ["."]), ("allowed_paths", ["./"]), ("allowed_paths", [".git"])):
            with self.subTest(field=field):
                task = copy.deepcopy(self.task)
                task[field] = value
                with self.assertRaises(factory.FactoryError):
                    factory.prepare(self.config, task, self.root / "packet")
                self.assertFalse((self.root / "packet").exists())

    def test_prepare_creates_reusable_packet_and_refuses_overwrite(self):
        output = self.root / "packet"
        factory.prepare(self.config, self.task, output)
        self.assertEqual(json.loads((output / "task.json").read_text()), self.task)
        self.assertIn(factory.digest(self.config), (output / "BRIEF.md").read_text())
        with self.assertRaises(FileExistsError):
            factory.prepare(self.config, self.task, output)

    def test_inspection_does_not_execute_manifest_scripts(self):
        (self.root / "package.json").write_text(json.dumps({"scripts": {"postinstall": "touch unexpected"}}))
        inventory = factory.inspect_repo(self.root)
        self.assertEqual(inventory["manifests"][0]["scripts"]["postinstall"], "touch unexpected")
        self.assertFalse((self.root / "unexpected").exists())

    def test_unreviewed_profile_can_generate_provider_specific_handoff(self):
        self.config["reviewed"] = False
        packet = self.root / "packet"
        factory.prepare(self.config, self.task, packet, assistant="claude")
        handoff = json.loads((packet / "handoff.json").read_text())
        self.assertEqual(handoff["assistant"], "claude")
        self.assertFalse(handoff["executes_agent"])
        with self.assertRaises(factory.FactoryError):
            self.run_report()

    def test_exported_tool_does_not_advertise_unavailable_stage_command(self):
        import repository_factory
        config=repository_factory.draft_profile(self.root, "example-owner/example-repository", "custom")
        output=self.root/"overlay"
        factory.stage(None, output, config=config)
        tool=output/".factory/factory.py"
        result=subprocess.run([sys.executable,str(tool),"--help"],capture_output=True,text=True)
        self.assertEqual(result.returncode,0)
        self.assertNotIn("stage",result.stdout)


if __name__ == "__main__":unittest.main()
