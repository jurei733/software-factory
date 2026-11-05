"""Portable GitHub repository onboarding and read-only hosted CI; no provider API or deployment."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import factory

ROOT = Path(__file__).resolve().parent
REPOSITORY = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9_.-]{1,100}"
PATH = r"[A-Za-z0-9_. /-]+"


def safe_path(value, *, root=False):
    factory.require(isinstance(value, str) and re.fullmatch(PATH, value) and
                    factory.relative_path(value) and (root or value not in (".", "./")) and
                    ".git" not in Path(value).parts, "Ungültiger Repository-Pfad: " + str(value))


def validate_repository(config, *, reviewed=False):
    factory.validate_config(config, require_reviewed=reviewed)
    factory.require((config["project"] == "$GITHUB_REPOSITORY" or (re.fullmatch(REPOSITORY, config["project"]) and
                    config["project"].split("/")[1] not in (".", ".."))),
                    "project muss ein GitHub owner/repository ohne URL oder Zugangsdaten sein.")
    ci = config.get("ci")
    factory.require(isinstance(ci, dict) and set(ci) ==
                    {"stack", "runtime", "install", "artifact_paths", "timeout_minutes"},
                    "ci benötigt stack, runtime, install, artifact_paths und timeout_minutes.")
    factory.require(ci["stack"] in ("node", "python", "custom"), "Unbekannter CI-Stack.")
    factory.require(isinstance(ci["runtime"], str) and
                    re.fullmatch(r"[0-9]+(?:\.[0-9]+){0,2}", ci["runtime"]),
                    "ci.runtime muss eine numerische Node-/Python-Version sein (custom: Python-Controller).")
    factory.require(type(ci["timeout_minutes"]) is int and 10 <= ci["timeout_minutes"] <= 120,
                    "ci.timeout_minutes muss zwischen 10 und 120 liegen.")
    factory.require(isinstance(ci["install"], list) and len(ci["install"]) <= 32,
                    "ci.install muss eine Liste mit höchstens 32 Schritten sein.")
    identifiers = set()
    for step in ci["install"]:
        factory.require(isinstance(step, dict) and set(step) == {"id", "argv", "cwd", "timeout_seconds"},
                        "Installationsschritte benötigen id, argv, cwd und timeout_seconds.")
        factory.require(isinstance(step["id"], str) and re.fullmatch(r"[a-z0-9-]+", step["id"]) and
                        step["id"] not in identifiers, "Ungültige oder doppelte Installations-ID.")
        identifiers.add(step["id"])
        factory.require(factory.text_list(step["argv"]) and
                        not any("\0" in value for value in step["argv"]), "Ungültiges Installations-argv.")
        safe_path(step["cwd"], root=True)
        factory.require(type(step["timeout_seconds"]) is int and 1 <= step["timeout_seconds"] <= 3600,
                        "Installations-Timeout muss zwischen 1 und 3600 liegen.")
    for step in config["steps"]:
        safe_path(step.get("cwd", "."), root=True)
        factory.require(not any("\0" in value for value in step["argv"]), "Ungültiges Prüf-argv.")
    factory.require(sum(step["timeout_seconds"] for step in ci["install"] + config["steps"]) <=
                    (ci["timeout_minutes"] - 5) * 60,
                    "Schrittbudgets müssen mit fünf Minuten Reserve ins CI-Zeitbudget passen.")
    factory.require(isinstance(ci["artifact_paths"], list) and len(ci["artifact_paths"]) <= 16,
                    "ci.artifact_paths benötigt höchstens 16 konkrete Dateipfade/Verzeichnisse.")
    for path in ci["artifact_paths"] + config["rules_files"]:
        safe_path(path)


def doctor_repository(repo, config):
    """Check paths/manifests without running repository commands or installations."""
    validate_repository(config)
    root = Path(repo).resolve()
    factory.require(root.is_dir(), "Repository-Verzeichnis fehlt.")
    for rule in config["rules_files"]:
        factory.require(factory.inside(root, rule).is_file(), "Projektregel fehlt: " + rule)
    for step in config["ci"]["install"] + config["steps"]:
        cwd = factory.inside(root, step.get("cwd", "."))
        factory.require(cwd.is_dir(), "Arbeitsverzeichnis fehlt: " + str(cwd))
        argv = step["argv"]
        if len(argv) >= 3 and argv[:2] == ["npm", "run"]:
            package = factory.read_json(cwd / "package.json")
            factory.require(argv[2] in package.get("scripts", {}), "npm-Script fehlt: " + argv[2])
    return {"project": config["project"], "status": "passed", "reviewed": config["reviewed"],
            "stack": config["ci"]["stack"], "config_sha256": factory.digest(config),
            "scope": "Pfade und Schema; keine Ausführung, Sicherheits- oder Produktionsfreigabe."}


def draft_profile(repo, repository, stack="auto"):
    factory.require(re.fullmatch(REPOSITORY, repository) and repository.split("/")[1] not in (".", ".."), "Select an explicit GitHub owner/repository")
    root = Path(repo).resolve()
    package = factory.read_json(root / "package.json") if (root / "package.json").is_file() else None
    if stack == "auto":
        stack = "node" if package is not None else "python" if (root / "pyproject.toml").is_file() else "custom"
    rules = [name for name in ("AGENTS.md", "CLAUDE.md", "rules.md", "README.md")
             if (root / name).is_file()]
    # The overlay always provides explicit integration rules, even for an empty repository.
    rules = rules or [".factory/PROJECT_RULES.md"]
    steps, install = [], []
    if stack == "node" and package is not None:
        manager = "npm"
        declared = package.get("packageManager", "")
        if re.fullmatch(r"(?:pnpm|yarn)@[0-9]+\.[0-9]+\.[0-9]+(?:\+sha(?:224|512)\.[a-f0-9]+)?", declared):
            manager = declared.split("@")[0]
        command = ["npm"] if manager == "npm" else ["corepack", manager]
        available = package.get("scripts", {})
        steps = [{"id": category, "category": category, "argv": command + ["run", category],
                  "cwd": ".", "timeout_seconds": 600} for category in ("lint", "test", "build")
                 if category in available]
        lock = {"npm": "package-lock.json", "pnpm": "pnpm-lock.yaml", "yarn": "yarn.lock"}[manager]
        if (root / lock).is_file():
            install_argv = ["npm", "ci", "--no-fund"] if manager == "npm" else command + (["install", "--frozen-lockfile"] if manager == "pnpm" else ["install", "--immutable"])
            install = [{"id": "dependencies", "argv": install_argv,
                        "cwd": ".", "timeout_seconds": 600}]
        else:
            # Do not silently substitute an unlocked install or another package manager.
            install = [{"id": "configure-dependencies", "argv": ["python3", ".factory/needs-configuration.py"],
                        "cwd": ".", "timeout_seconds": 30}]
    elif stack == "python":
        commands = {"lint": ["python3", "-m", "ruff", "check", "."],
                    "test": ["python3", "-m", "pytest"], "build": ["python3", "-m", "build"]}
        steps = [{"id": name, "category": name, "argv": argv, "cwd": ".", "timeout_seconds": 600}
                 for name, argv in commands.items()]
        install = [{"id": "dependencies", "argv": ["python3", "-m", "pip", "install", "-e", ".[dev]"],
                    "cwd": ".", "timeout_seconds": 600}]
    for category in ("lint", "test", "build"):
        if not any(step["category"] == category for step in steps):
            steps.append({"id": category, "category": category,
                          "argv": ["python3", ".factory/needs-configuration.py"],
                          "cwd": ".", "timeout_seconds": 30})
    config = {"schema_version": 1, "project": repository, "reviewed": False, "rules_files": rules,
              "prerequisites": ["Befehle, Projektregeln, Laufzeit, Lockfiles und Ausgabeordner prüfen.",
                                "Python-Vorschlag erwartet deklarierte dev-Werkzeuge ruff, pytest und build.",
                                "Andere Stacks/Monorepos: eigene argv-, cwd- und Installationsschritte eintragen.",
                                "KI-Anmeldung bleibt lokal; kein Deployment und keine kostenpflichtige Aktivierung."],
              "steps": steps,
              "ci": {"stack": stack, "runtime": "22.23.3" if stack == "node" else "3.13",
                     "install": install, "artifact_paths": [], "timeout_minutes": 60}}
    validate_repository(config)
    return config


def render_quality(*, tool=".factory", config=".factory/config.json"):
    text = (ROOT / "templates/github/repository.quality.yml").read_text()
    text = text.replace("__TOOL__", tool).replace("__CONFIG__", config)
    for name, pin in factory.read_json(ROOT / "templates/github/actions.json").items():
        text = text.replace("__ACTION_" + name + "__", pin["uses"] + " # " + pin["version"])
    return text


def export_repository(config, output):
    validate_repository(config)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    tool = output / ".factory"
    tool.mkdir()
    factory.write_new_json(tool / "config.json", config)
    for name in ("factory.py", "isolated_runner.py", "repository_factory.py"):
        shutil.copyfile(ROOT / name, tool / name)
    for name in ("install_scanner.py", "security_gate.py"):
        destination = tool / "scripts" / name
        destination.parent.mkdir(exist_ok=True)
        shutil.copyfile(ROOT / "scripts" / name, destination)
    for name in ("security/tools.json", "trivy.yaml"):
        destination = tool / "config" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / "config" / name, destination)
    shutil.copyfile(ROOT / "scripts/prepare_task.py", tool / "prepare_task.py")
    (tool / "needs-configuration.py").write_text(
        'raise SystemExit("Configure the real project command in .factory/config.json; no placeholder check may pass.")\n')
    (tool / "PROJECT_RULES.md").write_text(
        "# Projektregeln\n\nVorhandene Bereichsregeln zuerst lesen. Nur beauftragte Pfade ändern.\n"
        "Keine Zugangsdaten oder Produktionsdaten in Aufgabenpakete aufnehmen.\n"
        "Die Integration erlaubt keinen Push, Merge oder Deployment durch einen KI-Assistenten.\n")
    task = {"id": "REPOSITORY-BASELINE-001", "title": "Repository und Factory-Anbindung prüfen",
            "goal": "Projektregeln und konfigurierte Prüfungen nachvollziehen; fehlende Voraussetzungen berichten.",
            "acceptance": ["Jede konfigurierte Prüfung und fehlende Betriebsfreigabe ist dokumentiert."],
            "out_of_scope": ["Codeänderungen, Push, Merge, Release und Deployment"], "allowed_paths": [],
            "risk": "low", "data_class": "synthetic", "max_attempts": 2, "max_minutes": 60}
    factory.write_new_json(tool / "tasks/pilot.json", task)
    workflows = output / ".github/workflows"
    workflows.mkdir(parents=True)
    (workflows / "factory-quality.yml").write_text(render_quality())
    text = (ROOT / "templates/github/task.yml").read_text()
    for name, pin in factory.read_json(ROOT / "templates/github/actions.json").items():
        text = text.replace("__ACTION_" + name + "__", pin["uses"] + " # " + pin["version"])
    (workflows / "factory-task.yml").write_text(text)
    (output / "INTEGRATION.md").write_text((ROOT / "templates/REPOSITORY_INTEGRATION.md").read_text())


def onboard_repository(repo, repository, output, *, stack="auto"):
    root, destination = Path(repo).resolve(), Path(output).resolve()
    factory.require(root.is_dir(), "Repository-Verzeichnis fehlt.")
    factory.require(root != destination and root not in destination.parents,
                    "Overlay außerhalb des Quellcheckouts anlegen; das Original bleibt unverändert.")
    config = draft_profile(root, repository, stack)
    export_repository(config, destination)
    factory.write_new_json(destination / "inventory.json", factory.inspect_repo(root))


def bind_repository(config, environment):
    actual = environment.get("GITHUB_REPOSITORY", "")
    factory.require(re.fullmatch(REPOSITORY, actual) and actual.split("/")[1] not in (".", ".."), "Invalid GitHub repository context")
    if config["project"] == "$GITHUB_REPOSITORY":
        return dict(config, project=actual)
    return config


def ci_plan(repo, config, environment):
    validate_repository(config, reviewed=True)
    config = bind_repository(config, environment)
    factory.require(environment.get("GITHUB_REPOSITORY", "").lower() == config["project"].lower(),
                    "Profil gehört nicht zum ausführenden GitHub-Repository.")
    doctor_repository(repo, config)
    return {"stack": config["ci"]["stack"], "node": config["ci"]["runtime"] if config["ci"]["stack"] == "node" else "22",
            "python": config["ci"]["runtime"] if config["ci"]["stack"] != "node" else "3.13",
            "timeout_minutes": str(config["ci"]["timeout_minutes"])}


def ci_environment(environment):
    # Never forward GitHub runtime/artifact tokens or arbitrary host/provider variables to project commands.
    names = ("PATH", "HOME", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "JAVA_HOME", "GOROOT", "GOPATH",
             "CARGO_HOME", "RUSTUP_HOME", "DOTNET_ROOT", "PKG_CONFIG_PATH", "LD_LIBRARY_PATH")
    return dict({name: environment[name] for name in names if name in environment},
                CI="true", NX_DAEMON="false", NX_NO_CLOUD="true", PIP_DISABLE_PIP_VERSION_CHECK="1")


def collect_artifacts(root, paths, destination):
    """Export bounded regular data only, never follow links into credentials or outside the checkout."""
    selected, total = {}, 0
    for value in paths:
        safe_path(value)
        relative = Path(value)
        factory.require(not any((root / Path(*relative.parts[:i])).is_symlink()
                                for i in range(1, len(relative.parts) + 1)), "Keine Links in Build-Ausgaben.")
        path = factory.inside(root, value)
        factory.require(path.exists(), "Build-Ausgabe fehlt: " + value)
        for item in ([root / value] if path.is_file() else sorted((root / value).rglob("*"))):
            relative = item.relative_to(root)
            factory.require(not any((root / Path(*relative.parts[:i])).is_symlink()
                                    for i in range(1, len(relative.parts) + 1)), "Keine Links in Build-Ausgaben.")
            safe_path(relative.as_posix())
            if item.is_dir():
                continue
            factory.require(item.is_file(), "Build-Ausgabe enthält eine Spezialdatei.")
            selected[relative.as_posix()] = item
    manifest = []
    for name, source in selected.items():
        total += source.stat().st_size
        factory.require(total <= 200 * 1024 * 1024 and len(selected) <= 10000,
                        "Build-Ausgaben überschreiten 200 MiB oder 10000 Dateien.")
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        manifest.append({"path": name, "sha256": hashlib.sha256(target.read_bytes()).hexdigest()})
    return manifest


def npm_audit(repo, report):
    """Complement scanner databases with npm's advisory service, without install scripts."""
    root = Path(repo).resolve()
    tracked = subprocess.check_output(["git", "-C", str(root), "ls-files", "-z"], text=True).split("\0")
    findings = []
    failed = False
    for name in sorted(path for path in tracked if Path(path).name == "package-lock.json"):
        safe_path(name)
        lock = factory.inside(root, name)
        factory.require(not (root / name).is_symlink(), "Lockfile darf kein Link sein.")
        result = subprocess.run(["npm", "audit", "--package-lock-only", "--ignore-scripts", "--audit=true",
                                 "--audit-level=high", "--registry=https://registry.npmjs.org", "--json"],
                                cwd=lock.parent, env=ci_environment(os.environ), capture_output=True,
                                text=True, timeout=180, check=False)
        # Raw provider responses (which might contain local config details) never enter the evidence artifact.
        data = json.loads(result.stdout)
        counts = data.get("metadata", {}).get("vulnerabilities", {})
        factory.require(result.returncode in (0, 1) and not data.get("error") and
                        all(type(counts.get(key)) is int for key in ("high", "critical")),
                        "npm-Advisory-Prüfung lieferte keinen gültigen Nachweis für " + name)
        blocked = counts["high"] > 0 or counts["critical"] > 0 or result.returncode != 0
        findings.append({"lockfile": name, "high": counts["high"], "critical": counts["critical"],
                         "status": "failed" if blocked else "passed"})
        failed = failed or blocked
    factory.write_new_json(report, {"status": "failed" if failed else "passed", "lockfiles": findings})
    print("npm advisory check: %d lockfiles; %s" % (len(findings), "failed" if failed else "passed"))
    return 1 if failed else 0


def ci_check(repo, config, report_dir, environment):
    factory.require(environment.get("GITHUB_ACTIONS") == "true" and
                    environment.get("RUNNER_ENVIRONMENT") == "github-hosted",
                    "Host-Prüfungen sind nur für wegwerfbare GitHub-hosted Runner vorgesehen; lokal factory.py run nutzen.")
    root = Path(repo).resolve()
    ci_plan(root, config, environment)
    config = bind_repository(config, environment)
    report_dir = Path(report_dir).resolve()
    factory.require(root != report_dir and root not in report_dir.parents,
                    "Prüfberichte müssen außerhalb des Quellcheckouts liegen.")
    report_dir.mkdir(parents=True, exist_ok=False)
    safe_env = ci_environment(environment)
    setup = []
    for step in config["ci"]["install"]:
        print("Installiere " + step["id"], flush=True)
        process = None
        try:
            process = subprocess.Popen(step["argv"], cwd=factory.inside(root, step["cwd"]),
                                       env=safe_env, start_new_session=(os.name == "posix"))
            code = process.wait(timeout=step["timeout_seconds"])
            setup.append({"id": step["id"], "status": "passed" if code == 0 else "failed", "returncode": code})
        except (OSError, subprocess.TimeoutExpired) as error:
            if process is not None:
                factory.terminate_check(process)
            setup.append({"id": step["id"], "status": "failed", "error": type(error).__name__})
        if setup[-1]["status"] != "passed":
            factory.write_new_json(report_dir / "installation.json", {"status": "failed", "steps": setup})
            return 1
    factory.write_new_json(report_dir / "installation.json", {"status": "passed", "steps": setup})
    task = {"id": "GITHUB-CI", "title": "Repository CI", "goal": "Run reviewed repository checks",
            "acceptance": ["Every configured check passes"], "out_of_scope": ["Deployment and production approval"],
            "allowed_paths": [], "risk": "low", "data_class": "synthetic", "max_attempts": 1, "max_minutes": 120}
    source_commit = factory.git_commit(root)
    code = factory._run_host_checks(root, config, task, report_dir / "checks.json", environment=safe_env)
    if code == 0:
        manifest = collect_artifacts(root, config["ci"]["artifact_paths"], report_dir / "build")
        factory.write_new_json(report_dir / "build-manifest.json", {
            "repository": config["project"], "source_commit": source_commit,
            "config_sha256": factory.digest(config), "files": manifest,
            "created_at": datetime.now(timezone.utc).isoformat(), "production_approved": False,
            "scope": "CI data and checksums; not a signed provenance or production authorization."})
    return code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "check", "npm-audit"))
    parser.add_argument("--repo", required=True)
    parser.add_argument("--config")
    parser.add_argument("--report-dir")
    parser.add_argument("--report")
    args = parser.parse_args()
    if args.command == "npm-audit":
        factory.require(args.report, "--report fehlt.")
        return npm_audit(args.repo, Path(args.report))
    factory.require(args.config, "--config fehlt.")
    config = factory.read_json(args.config)
    if args.command == "plan":
        values = ci_plan(args.repo, config, os.environ)
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
                for key, value in values.items():
                    output.write(key + "=" + value + "\n")
        print(json.dumps(values))
        return 0
    factory.require(args.report_dir, "--report-dir fehlt.")
    return ci_check(args.repo, config, args.report_dir, os.environ)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (factory.FactoryError, OSError, ValueError, subprocess.SubprocessError) as error:
        print("FEHLER: " + str(error), file=sys.stderr)
        sys.exit(2)
