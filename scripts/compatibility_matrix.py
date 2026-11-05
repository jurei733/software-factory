"""Exercise real independent consumer projects, installs, exported CLIs and negative gates."""
import argparse
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import factory
import repository_factory
import integration_updates
from isolated_runner import git_command, git_environment
from scripts.security_gate import evaluate

CASES = {"npm": "npm@10.9.9", "pnpm": "pnpm@10.8.1", "yarn-classic": "yarn@1.22.22",
         "yarn-modern": "yarn@4.9.1", "python": None, "monorepo": None}


def command(argv, cwd, **kwargs):
    return subprocess.run(argv, cwd=cwd, check=True, timeout=300, **kwargs)


def exercise(case, base):
    repo = base / "project"; repo.mkdir()
    (repo / "README.md").write_text("Synthetic independent compatibility project\n")
    manager = CASES[case]
    if manager:
        (repo / "src").mkdir(); (repo / "tests").mkdir()
        (repo / "src/answer.cjs").write_text("exports.answer = () => 42;\n")
        (repo / "tests/answer.test.cjs").write_text("require('node:test')('answer',()=>require('node:assert/strict').equal(require('../src/answer.cjs').answer(),42));\n")
        (repo / "package.json").write_text(json.dumps({"name": "factory-compatibility-fixture", "version": "1.0.0", "private": True,
            "packageManager": manager, "scripts": {"lint": "node --check src/answer.cjs", "test": "node --test tests/answer.test.cjs",
            "build": "node -e \"require('fs').mkdirSync('dist',{recursive:true});require('fs').copyFileSync('src/answer.cjs','dist/answer.cjs')\""}}))
        env = dict(os.environ, YARN_ENABLE_IMMUTABLE_INSTALLS="false")
        if case == "npm":
            command(["npm", "install", "--package-lock-only", "--ignore-scripts", "--no-audit", "--no-fund"], repo, env=env)
        elif case == "pnpm":
            command(["corepack", "pnpm", "install", "--lockfile-only", "--ignore-scripts"], repo, env=env)
        else:
            command(["corepack", "yarn", "install"] + (["--ignore-scripts"] if case == "yarn-classic" else ["--mode=skip-build"]), repo, env=env)
    else:
        project = repo / ("services/worker" if case == "monorepo" else ".")
        project.mkdir(parents=True, exist_ok=True)
        (project / "answer.py").write_text("def answer():\n    return 42\n")
        (project / "test_answer.py").write_text("import unittest\nfrom answer import answer\nclass Answer(unittest.TestCase):\n def test_answer(self): self.assertEqual(answer(),42)\n")
        if case == "python":
            (repo / "pyproject.toml").write_text('[project]\nname="factory-compatibility-fixture"\nversion="1.0.0"\n')
    for args in [("init", "-q"), ("add", "."), ("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "Fixture")]:
        subprocess.run(git_command(repo, *args), env=git_environment(), check=True)
    overlay = base / "overlay"
    repository_factory.onboard_repository(repo, "example-owner/compatibility-fixture", overlay)
    config = factory.read_json(overlay / ".factory/config.json")
    if not manager:
        cwd = "services/worker" if case == "monorepo" else "."
        config["ci"]["install"] = []
        for step, argv in zip(config["steps"], [
                ["python3", "-c", "compile(open('answer.py').read(),'answer.py','exec')"],
                ["python3", "-m", "unittest", "discover"],
                ["python3", "-c", "from pathlib import Path;import zipfile;Path('dist').mkdir(exist_ok=True);z=zipfile.ZipFile('dist/app.zip','w');z.write('answer.py');z.close()"]]):
            step.update(argv=argv, cwd=cwd)
        artifact = (cwd + "/" if cwd != "." else "") + "dist/app.zip"
    else:
        artifact = "dist/answer.cjs"
    config["reviewed"] = True; config["ci"]["artifact_paths"] = [artifact]
    (overlay / ".factory/config.json").write_text(json.dumps(config))
    shutil.copytree(overlay, repo, dirs_exist_ok=True)
    # Execute the exported controller, not an import of the central implementation.
    env = dict(os.environ, GITHUB_ACTIONS="true", RUNNER_ENVIRONMENT="github-hosted", GITHUB_REPOSITORY=config["project"])
    cli = [sys.executable, ".factory/repository_factory.py"]
    command(cli + ["plan", "--repo", ".", "--config", ".factory/config.json"], repo, env=env)
    command(cli + ["check", "--repo", ".", "--config", ".factory/config.json", "--report-dir", str(base / "positive")], repo, env=env)
    assert factory.read_json(base / "positive/checks.json")["status"] == "passed"
    manifest = factory.read_json(base / "positive/build-manifest.json")
    assert manifest["files"] and (base / "positive/build" / artifact).is_file()
    # Preserve user configuration/tasks while updating an owned previous integration.
    old = integration_updates.read_manifest(repo)
    tool = repo / ".factory/factory.py"; tool.write_text(tool.read_text() + "\n# older integration\n")
    old["files"][".factory/factory.py"] = integration_updates.digest(tool); old["factory"]["version"] = "0.0.1"
    (repo / integration_updates.MANIFEST).write_text(json.dumps(old))
    assert integration_updates.upgrade(repo, base / "upgrade", apply=True)["applied"]
    assert factory.read_json(repo / ".factory/config.json") == config
    # A failed installation may not enter tests/build or create a successful manifest.
    failed = copy.deepcopy(config)
    failed["ci"]["install"] = [{"id": "negative-install", "argv": ["python3", "-c", "raise SystemExit(19)"], "cwd": ".", "timeout_seconds": 10}]
    assert repository_factory.ci_check(repo, failed, base / "install-failure", env) == 1
    assert not (base / "install-failure/checks.json").exists()
    failed = copy.deepcopy(config)
    next(step for step in failed["steps"] if step["category"] == "test")["argv"] = ["python3", "-c", "raise SystemExit(17)"]
    assert repository_factory.ci_check(repo, failed, base / "test-failure", env) == 1
    assert not (base / "test-failure/build-manifest.json").exists()
    assert next(step for step in factory.read_json(base / "test-failure/checks.json")["steps"] if step["category"] == "build")["status"] == "skipped"
    scan = {"SchemaVersion": 2, "ArtifactName": "synthetic", "Results": [{"Target": "fixture", "Vulnerabilities": [{"Severity": "HIGH", "PkgName": "fixture", "VulnerabilityID": "SYNTHETIC"}]}]}
    assert evaluate(scan, {"SchemaVersion": 2, "ArtifactName": "empty", "Results": []})["status"] == "failed"
    return {"case": case, "status": "passed", "real_install_and_exported_checks": True,
            "artifact_verified": True, "upgrade_preserved_profile": True, "negative_install_test_security_gates": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("--case", choices=CASES, required=True)
    parser.add_argument("--out", type=Path, required=True); args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="factory-compatibility-") as folder:
        report = exercise(args.case, Path(folder))
    args.out.parent.mkdir(parents=True, exist_ok=True); args.out.write_text(json.dumps(report, indent=2) + "\n")
