import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import factory
import isolated_runner


class IsolationTests(unittest.TestCase):
    def setUp(self):
        work = ROOT / "work"
        work.mkdir(exist_ok=True)
        self.folder = tempfile.TemporaryDirectory(prefix="isolation-test-", dir=work)
        self.addCleanup(self.folder.cleanup)
        self.base = Path(self.folder.name).resolve()
        self.repo = self.base / "repo"; self.repo.mkdir()
        (self.repo / "src").mkdir()
        (self.repo / "src/allowed.txt").write_text("original\n")
        (self.repo / "blocked.txt").write_text("original\n")
        (self.repo / "README.md").write_text("Synthetic isolation test")
        (self.repo / ".gitignore").write_text("ignored.txt\n")
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=Synthetic test", "-c",
                        "user.email=test@example.invalid", "commit", "-qm", "Fixture"], check=True)
        (self.repo / "ignored.txt").write_text("synthetic ignored credential")
        self.image = json.loads((ROOT / "config/sandbox.json").read_text())["test_image"]
        self.config = {"schema_version": 1, "project": "synthetic", "reviewed": True,
                       "rules_files": ["README.md"], "steps": [
                           {"id": name, "category": name, "argv": ["/bin/sh", "-c", "true"],
                            "timeout_seconds": 10} for name in ("lint", "test", "build")]}
        self.task = {"id": "ISOLATION-001", "title": "Test isolation", "goal": "Synthetic test",
                     "acceptance": ["Isolation holds"], "out_of_scope": ["Real systems"],
                     "allowed_paths": ["src"], "risk": "low", "data_class": "synthetic",
                     "max_attempts": 1, "max_minutes": 1}

    def run_report(self):
        report = self.base / "session/report.json"
        code = factory.run_checks(self.repo, self.config, self.task, report, self.image)
        return code, json.loads(report.read_text())

    def test_public_runner_never_falls_back_to_host_execution(self):
        with self.assertRaisesRegex(ValueError, "immutable"):
            factory.run_checks(self.repo, self.config, self.task, self.base / "report.json")
        with self.assertRaisesRegex(ValueError, "immutable"):
            factory.run_checks(self.repo, self.config, self.task, self.base / "report.json", "alpine:latest")

    def test_attempt_ledger_rejects_budget_reuse_and_changed_task(self):
        source = "a" * 40
        isolated_runner.reserve_attempt(self.base / "ledger", self.task, "config", source)
        with self.assertRaisesRegex(ValueError, "budget exhausted"):
            isolated_runner.reserve_attempt(self.base / "ledger", self.task, "config", source)
        changed = dict(self.task, max_attempts=3)
        with self.assertRaisesRegex(ValueError, "changed"):
            isolated_runner.reserve_attempt(self.base / "ledger", changed, "config", source)

    def test_attempt_lock_survives_process_crash_without_resetting_budget(self):
        task = dict(self.task, max_attempts=2)
        directory = self.base / "ledger"
        isolated_runner.reserve_attempt(directory, task, "config", "a" * 40)
        lock = directory / ("." + task["id"] + ".lock")
        code = ('import fcntl,sys,time; f=open(sys.argv[1],"a+"); '
                'fcntl.flock(f,fcntl.LOCK_EX); print("locked",flush=True); time.sleep(30)')
        process = subprocess.Popen([sys.executable, "-c", code, str(lock)], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), "locked")
            with self.assertRaisesRegex(ValueError, "already locked"):
                isolated_runner.reserve_attempt(directory, task, "config", "a" * 40)
        finally:
            process.kill();process.wait(timeout=5);process.stdout.close()
        self.assertEqual(isolated_runner.reserve_attempt(directory, task, "config", "a" * 40), 2)
        with self.assertRaisesRegex(ValueError, "budget exhausted"):
            isolated_runner.reserve_attempt(directory, task, "config", "a" * 40)

    def test_missing_allowed_paths_stay_missing_and_dependency_mounts_cannot_hide_source(self):
        self.assertEqual(isolated_runner.mount_paths(self.repo, ["Makefile", "src/missing.txt"]), [])
        self.assertFalse((self.repo / "Makefile").exists())
        self.assertFalse((self.repo / "src/missing.txt").exists())
        with self.assertRaisesRegex(ValueError, "hide source"):
            isolated_runner.dependency_plan(self.repo, [{"image_path": "/opt/factory-deps/modules", "target": "src"}], [])
        with self.assertRaises(factory.FactoryError):
            factory.validate_config(dict(self.config, execution={"dependencies": [
                {"image_path": "/opt/factory-deps/../outside", "target": "node_modules"}]}))

    def test_snapshot_preserves_internal_links_but_never_copies_ignored_inputs(self):
        (self.repo / "internal-link").symlink_to("src/allowed.txt")
        subprocess.run(["git", "-C", str(self.repo), "add", "internal-link"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=Synthetic test", "-c",
                        "user.email=test@example.invalid", "commit", "-qm", "Internal link"], check=True)
        target = self.base / "snapshot";target.mkdir()
        isolated_runner.snapshot(self.repo, target)
        self.assertEqual((target / "internal-link").read_text(), "original\n")
        self.assertFalse((target / "ignored.txt").exists())
        with self.assertRaisesRegex(ValueError, "symlink"):
            isolated_runner.mount_paths(target, ["internal-link"])

    def test_scope_validation_includes_new_ignored_files(self):
        patch = self.base / "outside.patch"
        patch.write_text("diff --git a/ignored.txt b/ignored.txt\nnew file mode 100644\n"
                         "--- /dev/null\n+++ b/ignored.txt\n@@ -0,0 +1 @@\n+synthetic\n")
        with self.assertRaisesRegex(ValueError, "outside allowed_paths"):
            factory.run_checks(self.repo, self.config, self.task, self.base / "session/report.json",
                               self.image, patch)
        self.assertEqual((self.repo / "ignored.txt").read_text(), "synthetic ignored credential")

    def test_root_symlink_and_source_backed_scratch_mounts_are_rejected(self):
        for value in (".", ".git", "../outside", "src,readonly=false"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                isolated_runner.mount_paths(self.repo, [value])
        (self.repo / "link").symlink_to(self.repo / "src", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            isolated_runner.mount_paths(self.repo, ["link"])
        with self.assertRaisesRegex(ValueError, "source files"):
            isolated_runner.mount_paths(self.repo, ["blocked.txt"], scratch=True)

    @unittest.skipUnless(os.environ.get("FACTORY_DOCKER_TESTS") == "1", "Required in the isolation CI job")
    def test_real_container_allows_scoped_edits_without_changing_original_checkout(self):
        self.config["steps"][0]["argv"] = ["/bin/sh", "-c", "printf changed > src/allowed.txt"]
        self.config["steps"][1]["argv"] = ["/bin/sh", "-c", "test \"$(cat src/allowed.txt)\" = changed"]
        code, report = self.run_report()
        self.assertEqual(code, 0, report)
        self.assertEqual((self.repo / "src/allowed.txt").read_text(), "original\n")
        self.assertNotEqual(subprocess.run(["docker", "inspect", report["container"]],
                                          capture_output=True).returncode, 0)

    @unittest.skipUnless(os.environ.get("FACTORY_DOCKER_TESTS") == "1", "Required in the isolation CI job")
    def test_real_container_denies_write_outside_scope(self):
        self.config["steps"][0]["argv"] = ["/bin/sh", "-c", "printf forbidden > blocked.txt"]
        code, report = self.run_report()
        self.assertEqual(code, 1)
        self.assertEqual(report["steps"][0]["status"], "failed")
        self.assertEqual(report["steps"][1]["status"], "skipped")
        self.assertEqual((self.repo / "blocked.txt").read_text(), "original\n")

    @unittest.skipUnless(os.environ.get("FACTORY_DOCKER_TESTS") == "1", "Required in the isolation CI job")
    def test_real_container_has_no_host_credential_ignored_file_or_external_route(self):
        self.config["steps"][0]["argv"] = ["/bin/sh", "-c",
            'test -z "$FACTORY_SYNTHETIC_TOKEN" && test ! -e /workspace/ignored.txt && '
            'test ! -S /var/run/docker.sock && test "$(wc -l < /proc/net/route)" -eq 1']
        previous = os.environ.get("FACTORY_SYNTHETIC_TOKEN")
        os.environ["FACTORY_SYNTHETIC_TOKEN"] = "fixture-only"
        try:
            code, report = self.run_report()
        finally:
            if previous is None:
                os.environ.pop("FACTORY_SYNTHETIC_TOKEN", None)
            else:
                os.environ["FACTORY_SYNTHETIC_TOKEN"] = previous
        self.assertEqual(code, 0, report)
        self.assertFalse(report["host_credentials"])
        self.assertEqual(report["network"], "none")

    @unittest.skipUnless(os.environ.get("FACTORY_DOCKER_TESTS") == "1", "Required in the isolation CI job")
    def test_real_container_timeout_stops_following_checks(self):
        self.config["steps"][0].update(argv=["/bin/sh", "-c", "sleep 5"], timeout_seconds=1)
        code, report = self.run_report()
        self.assertEqual(code, 1)
        self.assertEqual(report["steps"][0]["status"], "timeout")
        self.assertEqual(report["steps"][1]["status"], "skipped")

    @unittest.skipUnless(os.environ.get("FACTORY_DOCKER_TESTS") == "1", "Required in the isolation CI job")
    def test_deleted_allowed_file_is_absent_during_checks(self):
        patch = self.base / "delete.patch"
        patch.write_text("diff --git a/src/allowed.txt b/src/allowed.txt\ndeleted file mode 100644\n"
                         "--- a/src/allowed.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-original\n")
        self.task["allowed_paths"] = ["src/allowed.txt"]
        self.config["steps"][0]["argv"] = ["/bin/sh", "-c", "test ! -e src/allowed.txt"]
        report = self.base / "session/report.json"
        self.assertEqual(factory.run_checks(self.repo, self.config, self.task, report, self.image, patch), 0)
        self.assertEqual(json.loads(report.read_text())["patch_sha256"], hashlib.sha256(patch.read_bytes()).hexdigest())
        self.assertTrue((self.repo / "src/allowed.txt").exists())

    @unittest.skipUnless(os.environ.get("FACTORY_DOCKER_TESTS") == "1", "Required in the isolation CI job")
    def test_bundled_analysis_tasks_allow_checks_without_writable_source_paths(self):
        for application in ("pilot",):
            self.task = json.loads((ROOT / "tasks" / (application + ".json")).read_text())
            factory.validate_task(self.task)
            self.assertEqual(self.task["allowed_paths"], [])
            self.config["steps"][0]["argv"] = ["/bin/sh", "-c", "! touch src/allowed.txt && test -f README.md"]
            report = self.base / (application + "/report.json")
            self.assertEqual(factory.run_checks(self.repo, self.config, self.task, report, self.image), 0)
            packet = self.base / (application + "-packet")
            factory.prepare(self.config, self.task, packet)
            self.assertIn("reiner Leseauftrag", (packet / "BRIEF.md").read_text())

    @unittest.skipUnless(os.environ.get("FACTORY_DOCKER_TESTS") == "1", "Required in the isolation CI job")
    def test_different_patches_have_distinct_reports_and_attempt_inputs(self):
        self.task["max_attempts"] = 2
        records = []
        for number, value in enumerate(("first", "second"), 1):
            patch = self.base / ("change-%d.patch" % number)
            patch.write_text("diff --git a/src/allowed.txt b/src/allowed.txt\n--- a/src/allowed.txt\n"
                             "+++ b/src/allowed.txt\n@@ -1 +1 @@\n-original\n+" + value + "\n")
            report = self.base / ("session/report-%d.json" % number)
            self.assertEqual(factory.run_checks(self.repo, self.config, self.task, report, self.image, patch), 0)
            records.append(json.loads(report.read_text()))
            self.assertEqual(records[-1]["patch_sha256"], hashlib.sha256(patch.read_bytes()).hexdigest())
        self.assertNotEqual(records[0]["prepared_source_sha256"], records[1]["prepared_source_sha256"])
        ledger = json.loads((self.base / "session/.ISOLATION-001.attempts.json").read_text())
        for record, attempt in zip(records, ledger["history"]):
            self.assertEqual(record["patch_sha256"], attempt["inputs"]["patch_sha256"])
            self.assertEqual(record["prepared_source_sha256"], attempt["inputs"]["prepared_source_sha256"])

    @unittest.skipUnless(os.environ.get("FACTORY_DOCKER_TESTS") == "1", "Required in the isolation CI job")
    def test_image_dependencies_are_visible_read_only_and_cleaned_up(self):
        context = self.base / "image";context.mkdir()
        (context / "nx").write_text("#!/bin/sh\nprintf image-dependency\\n")
        (context / "Dockerfile").write_text("FROM " + self.image + "\n"
            "COPY nx /opt/factory-deps/node_modules/nx/bin/nx\n"
            "RUN mkdir /opt/factory-deps/node_modules/.bin && "
            "chmod 755 /opt/factory-deps/node_modules/nx/bin/nx && "
            "ln -s ../nx/bin/nx /opt/factory-deps/node_modules/.bin/nx\n")
        image_id = context / "image.id"
        subprocess.run(["docker", "build", "--quiet", "--iidfile", str(image_id), str(context)],
                       check=True, capture_output=True, text=True, timeout=60)
        self.image = image_id.read_text().strip()
        self.addCleanup(lambda: subprocess.run(["docker", "image", "rm", self.image], capture_output=True))
        self.config["execution"] = {"dependencies": [{"image_path": "/opt/factory-deps/node_modules", "target": "node_modules"}]}
        self.config["steps"][0]["argv"] = ["/bin/sh", "-c", './node_modules/.bin/nx | grep -q image-dependency']
        self.config["steps"][1]["argv"] = ["/bin/sh", "-c", '! touch node_modules/forbidden && test ! -e ignored.txt']
        code, report = self.run_report()
        self.assertEqual(code, 0, report)
        self.assertTrue(report["dependencies"][0]["read_only"])
        self.assertFalse((self.repo / "node_modules").exists())
        self.assertNotEqual(subprocess.run(["docker", "volume", "inspect", report["container"] + "-dependency-0"],
                                          capture_output=True).returncode, 0)

    @unittest.skipUnless(os.environ.get("FACTORY_DOCKER_TESTS") == "1", "Required in the isolation CI job")
    def test_image_declared_writable_volumes_are_rejected(self):
        context = self.base / "image";context.mkdir()
        (context / "Dockerfile").write_text("FROM " + self.image + "\nVOLUME /workspace/blocked\n")
        image_id = context / "image.id"
        subprocess.run(["docker", "build", "--quiet", "--iidfile", str(image_id), str(context)],
                       check=True, capture_output=True, timeout=60)
        self.image = image_id.read_text().strip()
        self.addCleanup(lambda: subprocess.run(["docker", "image", "rm", self.image], capture_output=True))
        code, report = self.run_report()
        self.assertEqual(code, 1)
        self.assertIn("implicit writable volumes", report["runner_error"])
        self.assertEqual(report["steps"], [])


if __name__ == "__main__":
    unittest.main()
