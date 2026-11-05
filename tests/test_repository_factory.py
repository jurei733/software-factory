import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import factory
import repository_factory as repository


class RepositoryFactoryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.base = Path(self.folder.name).resolve()
        self.repo = self.base / "repo"
        self.repo.mkdir()
        (self.repo / "README.md").write_text("Independent synthetic repository")
        (self.repo / "src").mkdir()
        (self.repo / "src/main.py").write_text("def answer():\n    return 42\n")
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=Fixture", "-c",
                        "user.email=fixture@example.invalid", "commit", "-qm", "Fixture"], check=True)
        self.config = repository.draft_profile(self.repo, "example-owner/unrelated-project", "custom")
        self.config["reviewed"] = True
        commands = [
            "compile(open('src/main.py').read(), 'src/main.py', 'exec')",
            "import runpy; assert runpy.run_path('src/main.py')['answer']() == 42",
            "from pathlib import Path; Path('dist').mkdir(); Path('dist/result.txt').write_text('42')"]
        for step, command in zip(self.config["steps"], commands):
            step.update(argv=[sys.executable, "-c", command], timeout_seconds=10)
        self.config["ci"]["artifact_paths"] = ["dist/result.txt"]
        self.environment = dict(os.environ, GITHUB_ACTIONS="true", RUNNER_ENVIRONMENT="github-hosted",
                                GITHUB_REPOSITORY=self.config["project"])

    def test_onboarding_is_read_only_and_does_not_require_factory_allowlist(self):
        before = subprocess.check_output(["git", "-C", str(self.repo), "status", "--porcelain"])
        output = self.base / "overlay"
        repository.onboard_repository(self.repo, self.config["project"], output)
        self.assertEqual(before, subprocess.check_output(["git", "-C", str(self.repo), "status", "--porcelain"]))
        config = factory.read_json(output / ".factory/config.json")
        self.assertFalse(config["reviewed"])
        self.assertEqual(config["project"], "example-owner/unrelated-project")
        self.assertTrue((output / "inventory.json").is_file())
        with self.assertRaises(FileExistsError):
            repository.onboard_repository(self.repo, self.config["project"], output)
        with self.assertRaises(factory.FactoryError):
            repository.onboard_repository(self.repo, self.config["project"], self.repo / "overlay")

    def test_exported_tool_is_self_contained_and_executes_real_checks(self):
        overlay = self.base / "overlay"
        factory.stage(None, overlay, config=self.config)
        shutil.copytree(overlay / ".factory", self.repo / ".factory")
        command = [sys.executable, str(self.repo / ".factory/repository_factory.py")]
        result = subprocess.run(command + ["plan", "--repo", ".", "--config", ".factory/config.json"],
                                cwd=self.repo, env=self.environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = subprocess.run(command + ["check", "--repo", ".", "--config", ".factory/config.json",
                                          "--report-dir", str(self.base / "results")],
                                cwd=self.repo, env=self.environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = factory.read_json(self.base / "results/checks.json")
        self.assertTrue(all(step["status"] == "passed" for step in report["steps"]))
        manifest = factory.read_json(self.base / "results/build-manifest.json")
        self.assertEqual(manifest["repository"], self.config["project"])
        self.assertFalse(manifest["production_approved"])
        self.assertEqual((self.base / "results/build/dist/result.txt").read_text(), "42")

    def test_failure_stops_build_and_has_no_successful_bundle(self):
        self.config["steps"][1]["argv"] = [sys.executable, "-c", "raise SystemExit(7)"]
        self.assertEqual(repository.ci_check(self.repo, self.config, self.base / "results", self.environment), 1)
        report = factory.read_json(self.base / "results/checks.json")
        self.assertEqual(report["steps"][1]["returncode"], 7)
        self.assertEqual(report["steps"][2]["status"], "skipped")
        self.assertFalse((self.repo / "dist").exists())
        self.assertFalse((self.base / "results/build-manifest.json").exists())

    def test_failed_installation_cannot_run_tests_or_package(self):
        self.config["ci"]["install"] = [{"id": "dependency-install", "argv": [sys.executable, "-c", "exit(8)"],
                                           "cwd": ".", "timeout_seconds": 10}]
        self.assertEqual(repository.ci_check(self.repo, self.config, self.base / "results", self.environment), 1)
        self.assertFalse((self.base / "results/checks.json").exists())
        self.assertEqual(factory.read_json(self.base / "results/installation.json")["status"], "failed")

    def test_unreviewed_or_wrong_repository_cannot_install_or_execute(self):
        for changes in ({"reviewed": False}, {"project": "another-owner/another-repo"}):
            with self.subTest(changes=changes), self.assertRaises(factory.FactoryError):
                repository.ci_check(self.repo, dict(self.config, **changes), self.base / "results", self.environment)
        self.assertFalse((self.base / "results").exists())

    def test_local_or_self_hosted_ci_has_no_host_fallback(self):
        for environment in ({}, dict(self.environment, RUNNER_ENVIRONMENT="self-hosted")):
            with self.subTest(environment=environment.get("RUNNER_ENVIRONMENT")), self.assertRaises(factory.FactoryError):
                repository.ci_check(self.repo, self.config, self.base / "results", environment)

    def test_project_commands_receive_no_github_or_provider_credentials(self):
        clean = repository.ci_environment({"PATH": "/bin", "GITHUB_TOKEN": "synthetic", "GH_TOKEN": "synthetic",
                                           "ACTIONS_RUNTIME_TOKEN": "synthetic", "ANTHROPIC_API_KEY": "synthetic",
                                           "OPENAI_API_KEY": "synthetic", "JAVA_HOME": "/opt/jdk"})
        self.assertEqual(clean["JAVA_HOME"], "/opt/jdk")
        self.assertFalse(any(key.endswith(("TOKEN", "KEY")) for key in clean))

    def test_node_detection_preserves_real_scripts_and_marks_missing_commands(self):
        (self.repo / "package.json").write_text(json.dumps({"scripts": {"test": "test-runner"}}))
        config = repository.draft_profile(self.repo, self.config["project"])
        self.assertEqual(config["ci"]["stack"], "node")
        self.assertEqual(config["steps"][0]["argv"], ["npm", "run", "test"])
        self.assertEqual(config["ci"]["install"][0]["argv"], ["python3", ".factory/needs-configuration.py"])
        overlay = self.base / "overlay"
        repository.export_repository(config, overlay)
        result = subprocess.run([sys.executable, str(overlay / ".factory/needs-configuration.py")], capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        (self.repo / "package-lock.json").write_text("{}")
        self.assertEqual(repository.draft_profile(self.repo, self.config["project"])["ci"]["install"][0]["argv"],
                         ["npm", "ci", "--no-fund"])

    def test_python_and_nested_non_factory_manifests_are_detected_without_execution(self):
        (self.repo / "pyproject.toml").write_text('[project]\nname = "fixture"\n')
        nested = self.repo / "services/worker"
        nested.mkdir(parents=True)
        (nested / "go.mod").write_text("module example.invalid/worker")
        config = repository.draft_profile(self.repo, self.config["project"])
        self.assertEqual(config["ci"]["stack"], "python")
        paths = {entry["path"] for entry in factory.inspect_repo(self.repo)["manifests"]}
        self.assertIn("services/worker/go.mod", paths)
        self.assertIn("pyproject.toml", paths)

    def test_unsafe_repository_and_output_paths_are_rejected(self):
        for value in ("https://github.com/owner/repo", "owner/repo\nstack=node", "owner/..", "owner/repo;cmd"):
            with self.subTest(value=value), self.assertRaises(factory.FactoryError):
                repository.draft_profile(self.repo, value)
        for value in ("../outside", ".", ".git", "src\nelse", "${{ github.token }}", "/tmp/outside"):
            config = copy.deepcopy(self.config)
            config["ci"]["artifact_paths"] = [value]
            with self.subTest(value=value), self.assertRaises(factory.FactoryError):
                repository.validate_repository(config)

    def test_artifact_export_refuses_missing_files_and_internal_or_external_links(self):
        (self.repo / "inside-link").symlink_to("src/main.py")
        (self.repo / "outside-link").symlink_to(self.base)
        for value in ("missing", "inside-link", "outside-link"):
            with self.subTest(value=value), self.assertRaises(factory.FactoryError):
                repository.collect_artifacts(self.repo, [value], self.base / "data")

    def test_doctor_detects_wrong_commands_without_executing_project_code(self):
        (self.repo / "package.json").write_text('{"scripts":{"postinstall":"touch unexpected"}}')
        config = copy.deepcopy(self.config)
        config["steps"][0]["argv"] = ["npm", "run", "missing"]
        with self.assertRaisesRegex(factory.FactoryError, "npm-Script fehlt"):
            repository.doctor_repository(self.repo, config)
        self.assertFalse((self.repo / "unexpected").exists())

    def test_exported_workflows_have_independent_security_and_no_factory_or_write_access(self):
        overlay = self.base / "overlay"
        repository.export_repository(self.config, overlay)
        for path in (overlay / ".github/workflows").glob("*.yml"):
            content = path.read_text()
            for forbidden in ("example-owner/", "jurei733/", "contents: write", "secrets.", "pull_request_target", "__ACTION_"):
                self.assertNotIn(forbidden, content)
        content = (overlay / ".github/workflows/factory-quality.yml").read_text()
        self.assertIn("github.event.repository.default_branch", content)
        self.assertIn("Repository factory gate", content)
        self.assertIn("needs: [plan, checks, security]", content)
        security = content.split("\n  security:\n", 1)[1].split("\n  gate:\n", 1)[0]
        self.assertNotIn("npm ci", security)
        self.assertNotIn("repository_factory.py check", security)
        self.assertIn("--ignorefile /dev/null", security)

    def test_local_handoff_records_repo_commit_without_invoking_an_agent(self):
        repository.export_repository(self.config, self.base / "overlay")
        shutil.copytree(self.base / "overlay/.factory", self.repo / ".factory")
        environment = dict(os.environ, ASSISTANT="claude", TASK_FILE=".factory/tasks/pilot.json")
        result = subprocess.run([sys.executable, ".factory/prepare_task.py"], cwd=self.repo,
                                env=environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        source = factory.read_json(self.repo / "work-packet/source.json")
        self.assertEqual(source["repository"], self.config["project"])
        self.assertEqual(source["commit"], factory.git_commit(self.repo))
        self.assertFalse(factory.read_json(self.repo / "work-packet/handoff.json")["executes_agent"])

    def test_npm_service_blocks_advisories_and_invalid_provider_responses(self):
        lock = self.repo / "package-lock.json"
        lock.write_text("{}")
        subprocess.run(["git", "-C", str(self.repo), "add", "package-lock.json"], check=True)
        for count, expected in ((0, 0), (1, 1)):
            response = subprocess.CompletedProcess([], expected, json.dumps({
                "metadata": {"vulnerabilities": {"high": count, "critical": 0}}}), "")
            with patch.object(repository.subprocess, "check_output", return_value="package-lock.json\0"), \
                    patch.object(repository.subprocess, "run", return_value=response) as run:
                self.assertEqual(repository.npm_audit(self.repo, self.base / (str(count) + ".json")), expected)
            self.assertIn("--ignore-scripts", run.call_args.args[0])
        response = subprocess.CompletedProcess([], 1, '{"error":{"code":"ELOCKVERIFY"}}', "")
        with patch.object(repository.subprocess, "check_output", return_value="package-lock.json\0"), \
                patch.object(repository.subprocess, "run", return_value=response), self.assertRaises(factory.FactoryError):
            repository.npm_audit(self.repo, self.base / "invalid.json")


if __name__ == "__main__":
    unittest.main()
