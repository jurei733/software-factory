#!/usr/bin/env python3
"""Repository-independent software factory. Python >=3.9; standard library only."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone


class FactoryError(ValueError):
    pass


def read_json(path):
    path = Path(path)
    if path.stat().st_size > 1024 * 1024:
        raise FactoryError("JSON-Datei ist größer als 1 MiB: " + str(path))
    return json.loads(path.read_text(encoding="utf-8"))


def require(condition, message):
    if not condition:
        raise FactoryError(message)


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def text_list(value):
    return isinstance(value, list) and bool(value) and all(nonempty(x) for x in value)


def relative_path(value):
    return nonempty(value) and not Path(value).is_absolute() and ".." not in Path(value).parts


def inside(root, value):
    require(relative_path(value), "Pfad muss relativ sein und darf kein '..' enthalten: " + str(value))
    target = (root / value).resolve()
    require(target == root or root in target.parents, "Pfad verlässt das Repository: " + value)
    return target


def validate_config(config, require_reviewed=True):
    require(isinstance(config, dict), "Konfiguration muss ein JSON-Objekt sein.")
    require(config.get("schema_version") == 1, "schema_version muss 1 sein.")
    require(nonempty(config.get("project")), "project fehlt.")
    require(type(config.get("reviewed")) is bool, "reviewed muss true oder false sein.")
    if require_reviewed:
        require(config["reviewed"],
                "Profil ist ungeprüft. Befehle und Voraussetzungen prüfen; danach reviewed=true setzen.")
    require(text_list(config.get("rules_files")), "rules_files muss die geltenden Projektregeln nennen.")
    require(all(relative_path(x) for x in config["rules_files"]), "Ungültiger Pfad in rules_files.")
    steps = config.get("steps")
    require(isinstance(steps, list) and bool(steps), "steps darf nicht leer sein.")
    ids, categories = set(), set()
    for step in steps:
        require(isinstance(step, dict), "Jeder Prüfschritt muss ein Objekt sein.")
        name = step.get("id")
        require(nonempty(name) and re.fullmatch(r"[a-z0-9-]+", name), "Ungültige step.id.")
        require(name not in ids, "Doppelte step.id: " + name)
        ids.add(name)
        category = step.get("category")
        require(category in ("lint", "test", "build", "e2e", "contract", "security"),
                "Ungültige Kategorie: " + str(category))
        categories.add(category)
        require(text_list(step.get("argv")), "argv muss eine nichtleere Liste von Argumenten sein.")
        require(relative_path(step.get("cwd", ".")), "Ungültiges cwd in " + name)
        seconds = step.get("timeout_seconds")
        require(type(seconds) is int and 1 <= seconds <= 3600,
                "timeout_seconds muss zwischen 1 und 3600 liegen.")
    require({"lint", "test", "build"}.issubset(categories),
            "Mindestens lint, test und build müssen konfiguriert sein.")
    execution = config.get("execution", {})
    require(isinstance(execution, dict), "execution muss ein Objekt sein.")
    if "image" in execution:
        require(nonempty(execution["image"]) and
                re.fullmatch(r"(?:[a-zA-Z0-9./:_-]+@)?sha256:[0-9a-f]{64}", execution["image"]),
                "execution.image muss einen unveränderlichen SHA-256-Digest nennen.")
    if "scratch_paths" in execution:
        require(isinstance(execution["scratch_paths"], list) and
                all(relative_path(path) for path in execution["scratch_paths"]),
                "execution.scratch_paths muss relative Pfade enthalten.")
    if "dependencies" in execution:
        require(isinstance(execution["dependencies"], list) and len(execution["dependencies"]) <= 16,
                "execution.dependencies muss eine Liste mit höchstens 16 Einträgen sein.")
        for dependency in execution["dependencies"]:
            require(isinstance(dependency, dict) and set(dependency) == {"image_path", "target"},
                    "Jede Abhängigkeit benötigt ausschließlich image_path und target.")
            source, target = dependency["image_path"], dependency["target"]
            require(nonempty(source) and re.fullmatch(r"/opt/factory-deps/[A-Za-z0-9_./-]+", source) and
                    not {".", ".."}.intersection(source.split("/")), "Ungültiger Pfad im Werkzeugimage.")
            require(relative_path(target) and target != "." and not target.startswith(".git") and
                    not any(char in target for char in (",", "\n", "\r", "\0")), "Ungültiger Abhängigkeitszielpfad.")


def validate_task(task):
    require(isinstance(task, dict), "Aufgabe muss ein JSON-Objekt sein.")
    require(nonempty(task.get("id")) and re.fullmatch(r"[A-Za-z0-9-]+", task["id"]), "Ungültige Aufgaben-ID.")
    for key in ("title", "goal"):
        require(nonempty(task.get(key)), key + " fehlt.")
    for key in ("acceptance", "out_of_scope"):
        require(text_list(task.get(key)), key + " muss eine nichtleere Liste enthalten.")
    paths = task.get("allowed_paths")
    require(isinstance(paths, list) and all(
        nonempty(path) and relative_path(path) and Path(path).parts and Path(path).parts[0] != ".git" and
        not any(char in path for char in (",", "\n", "\r", "\0")) for path in paths),
        "allowed_paths muss konkrete relative Pfade oder eine leere Liste für einen Leseauftrag enthalten.")
    require(task.get("risk") in ("low", "medium", "high"), "risk muss low, medium oder high sein.")
    require(task.get("data_class") in ("public", "synthetic"),
            "Dieser Pilot nimmt nur public oder synthetic an. Vertrauliche Daten brauchen eine eigene Umgebung.")
    require(type(task.get("max_attempts")) is int and 1 <= task["max_attempts"] <= 3,
            "max_attempts muss zwischen 1 und 3 liegen.")
    require(type(task.get("max_minutes")) is int and 1 <= task["max_minutes"] <= 120,
            "max_minutes muss zwischen 1 und 120 liegen.")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def write_new_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def inspect_repo(repo):
    root = Path(repo).resolve()
    require(root.is_dir(), "Repository-Verzeichnis fehlt.")
    manifests = []
    names = {"package.json", "composer.json", "pyproject.toml", "requirements.txt", "go.mod", "Cargo.toml",
             "pom.xml", "build.gradle", "build.gradle.kts", "Gemfile", "Makefile", "CMakeLists.txt"}
    skipped = {".git", "node_modules", "vendor", ".venv", "venv", "dist", "build", "__pycache__", ".factory"}
    count, truncated = 0, False
    for directory, folders, files in os.walk(root, followlinks=False):
        folder = Path(directory)
        count += 1
        if count > 5000:
            truncated = True
            break
        folders[:] = sorted(name for name in folders if name not in skipped and
                            not (folder / name).is_symlink() and len(folder.relative_to(root).parts) < 8)
        for filename in sorted(set(files) & names):
            path = folder / filename
            if path.is_symlink():
                continue
            if filename not in ("package.json", "composer.json"):
                manifests.append({"path": path.relative_to(root).as_posix(), "type": filename})
                continue
            data = read_json(path)
            require(isinstance(data, dict), "Manifest muss ein Objekt sein: " + str(path))
            manifests.append({"path": str(path.relative_to(root)), "name": data.get("name"),
                              "scripts": data.get("scripts", {}),
                              "runtime": {k: v for k, v in data.get("require", {}).items() if k == "php"},
                              "frameworks": sorted(k for k in {**data.get("dependencies", {}),
                                                                  **data.get("devDependencies", {})}
                                                   if k in ("@angular/core", "@nestjs/core", "nx", "jest", "cypress"))})
    candidates = (".gitlab-ci.yml", ".github/workflows", "Dockerfile", "docker-compose.yml",
                  "docker-compose.yaml", "AGENTS.md", "CLAUDE.md", "rules.md", ".ai", "Makefile")
    return {"repository": str(root), "scope": "Dateiinventar; kein Projektcode und keine Abhängigkeiten ausgeführt.",
            "truncated": truncated,
            "files": [p for p in candidates if inside(root, p).exists()], "manifests": manifests}


def prepare(config, task, output, assistant=None):
    validate_config(config, require_reviewed=False)
    validate_task(task)
    require(assistant in (None, "claude", "codex"), "Unbekannter Assistent.")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    write_new_json(output / "task.json", task)
    write_new_json(output / "config.json", config)
    write_new_json(output / "handoff.json", {"assistant": assistant, "executes_agent": False,
                                            "configuration_reviewed": config["reviewed"]})
    lines = ["# " + task["id"] + ": " + task["title"], "", task["goal"], "",
             "## Abnahme", ""] + ["- " + s for s in task["acceptance"]]
    lines += ["", "## Außerhalb des Auftrags", ""] + ["- " + s for s in task["out_of_scope"]]
    lines += ["", "## Bearbeitungsrahmen", "",
              "Erlaubte Pfade: " + (", ".join(task["allowed_paths"]) if task["allowed_paths"] else
                                   "keine; reiner Leseauftrag ohne Quelländerungen"),
              "Projektregeln zuerst lesen: " + ", ".join(config["rules_files"]),
              "Risiko: " + task["risk"] + "; Datenklasse: " + task["data_class"],
              "Budget: höchstens %s Minuten und %s Versuche." % (task["max_minutes"], task["max_attempts"]),
              "", "## Durchführung und Übergabe", "",
              "In einem separaten Branch/Worktree arbeiten. Bestehende Projektregeln berücksichtigen.",
              "Bei unklaren Anforderungen, überschrittenem Umfang oder Budget stoppen und berichten.",
              "Keine Produktionsdaten, Zugangsdaten oder unveröffentlichten Testaufgaben verwenden.",
              "Keine Merge-, Release- oder Deployment-Aktionen durchführen.",
              "Änderung und passende Regressionstests liefern; Abnahmekriterien einzeln nachweisen.",
              "Abschließend Diff, ausgeführte Checks, fehlende Checks und offene Risiken zum Review übergeben.",
              "", "## Konfigurierte Prüfungen", ""]
    lines += ["- " + s["id"] + " (cwd=" + s.get("cwd", ".") + "): " + json.dumps(s["argv"])
              for s in config["steps"]]
    if task["risk"] == "high":
        lines += ["", "Vor Umsetzung ist eine Architektur- und Fachprüfung durch Menschen erforderlich."]
    if not config["reviewed"]:
        lines += ["", "Das Projektprofil ist noch ungeprüft. Prüfkommandos und Voraussetzungen zuerst bestätigen."]
    if assistant:
        lines += ["", "## Übergabe an " + assistant.capitalize(), "",
                  "Dieses Briefing im gewählten, bereits angemeldeten Assistenten zusammen mit dem Projekt öffnen.",
                  "Für Codex gelten AGENTS.md und Bereichsregeln; für Claude auch vorhandene CLAUDE.md-Verweise.",
                  "Die obigen Projektregeln gelten für beide Werkzeuge. Keine Freigaben oder Rechte erweitern."]
    lines += ["", "Konfigurations-SHA256: " + digest(config), "Aufgaben-SHA256: " + digest(task), "",
              "Dieses Paket startet keinen Agenten und überträgt keine Anmeldung.",
              "Die native Assistentensitzung bleibt manuell und benötigt ihre eigenen Sandbox-/Zugriffsregeln.",
              "Zur Abnahme einen Patch exportieren und an factory.py run --image <geprüfter Digest> --patch <Datei> übergeben.",
              "Der Runner prüft den Patchumfang und führt Checks ohne Netzwerk oder Host-Zugangsdaten aus.",
              "Zeit- und Versuchsgrenzen gelten technisch pro Prüfsitzung; das Coding-Budget der nativen Sitzung bleibt separat.", ""]
    (output / "BRIEF.md").write_text("\n".join(lines), encoding="utf-8")


def git_commit(root):
    try:
        result = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True,
                                text=True, timeout=5, check=False)
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def terminate_check(process):
    if process is None:
        return
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            if process.poll() is None:
                raise
    else:
        process.kill()
    process.wait()


def _run_host_checks(repo, config, task, report, *, environment=None):
    validate_config(config)
    validate_task(task)
    root = Path(repo).resolve()
    require(root.is_dir(), "Repository-Verzeichnis fehlt.")
    for rule in config["rules_files"]:
        require(inside(root, rule).is_file(), "Projektregel fehlt: " + rule)
    for step in config["steps"]:
        cwd = inside(root, step.get("cwd", "."))
        require(cwd.is_dir(), "Arbeitsverzeichnis fehlt: " + str(cwd))
        argv = step["argv"]
        if len(argv) >= 3 and argv[:2] == ["npm", "run"]:
            package = read_json(cwd / "package.json")
            require(argv[2] in package.get("scripts", {}), "npm-Script fehlt: " + argv[2])
    report = Path(report)
    report.parent.mkdir(parents=True, exist_ok=True)
    # Reserving the output before executing prevents an existing report being overwritten.
    with report.open("x", encoding="utf-8") as stream:
        result = {"schema_version": 1, "started_at": datetime.now(timezone.utc).isoformat(),
                  "project": config["project"], "task_id": task["id"], "source_commit": git_commit(root),
                  "config_sha256": digest(config), "task_sha256": digest(task),
                  "scope": "Nur konfigurierte lokale Befehle; keine Freigabe oder Release-Attestierung.", "steps": []}
        failed = False
        interrupted = False
        environment = dict(os.environ if environment is None else environment, CI="true", NX_DAEMON="false")
        for step in config["steps"]:
            item = {"id": step["id"], "category": step["category"], "argv": step["argv"],
                    "cwd": step.get("cwd", ".")}
            if failed:
                item["status"] = "skipped"
            else:
                print("Prüfe " + step["id"], flush=True)
                started = time.monotonic()
                process = None
                try:
                    process = subprocess.Popen(step["argv"], cwd=inside(root, step.get("cwd", ".")),
                                               env=environment, start_new_session=(os.name == "posix"))
                    code = process.wait(timeout=step["timeout_seconds"])
                    item.update(status="passed" if code == 0 else "failed", returncode=code)
                except subprocess.TimeoutExpired:
                    terminate_check(process)
                    item["status"] = "timeout"
                except KeyboardInterrupt:
                    terminate_check(process)
                    item["status"] = "cancelled"
                    interrupted = True
                except OSError as error:
                    item.update(status="error", error=str(error))
                item["duration_seconds"] = round(time.monotonic() - started, 3)
                failed = item["status"] != "passed"
            result["steps"].append(item)
        result["status"] = "cancelled" if interrupted else "failed" if failed else "passed"
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return 130 if interrupted else 1 if failed else 0


def run_checks(repo, config, task, report, image=None, patch=None):
    """Public execution always uses the isolated runner; there is no host fallback."""
    from isolated_runner import run_checks as isolated_checks
    return isolated_checks(repo, config, task, report,
                           image or config.get("execution", {}).get("image"), patch=patch)


def stage(profile, output, ci="github", *, config=None):
    require(profile is None and config is not None and ci == "github", "Provide a reviewed repository configuration for GitHub export")
    from repository_factory import export_repository
    return export_repository(config, output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect", help="Inventar ohne Codeausführung")
    inspect.add_argument("--repo", required=True)
    inspect.add_argument("--out")
    validate = commands.add_parser("validate", help="Konfiguration/Aufgabe prüfen")
    validate.add_argument("--config", required=True)
    validate.add_argument("--task")
    validate.add_argument("--schema-only", action="store_true", help="Nur Schema, keine lokale Ausführungsfreigabe")
    packet = commands.add_parser("prepare", help="Arbeitspaket erzeugen; startet keinen Agenten")
    packet.add_argument("--config", required=True)
    packet.add_argument("--task", required=True)
    packet.add_argument("--out", required=True)
    packet.add_argument("--assistant", choices=("claude", "codex"))
    run = commands.add_parser("run", help="Geprüfte Befehle isoliert in Docker ausführen")
    run.add_argument("--repo", required=True)
    run.add_argument("--config", required=True)
    run.add_argument("--task", required=True)
    run.add_argument("--report", required=True)
    run.add_argument("--image", help="Freigegebenes Werkzeugimage mit unveränderlichem @sha256-Digest")
    run.add_argument("--patch", help="Optionaler Patch; Änderungen außerhalb allowed_paths werden abgewiesen")
    base = Path(__file__).resolve().parent
    if all((base / name).is_dir() for name in ("profiles", "templates", "scripts")):
        overlay = commands.add_parser("stage", help="Integrationsdateien in NEUEN Ordner exportieren")
        overlay.add_argument("--config", required=True, help="Repository-Profil mit ci-Konfiguration")
        overlay.add_argument("--out", required=True)

        onboard = commands.add_parser("onboard", help="GitHub-Repository untersuchen und allgemeine Integration erzeugen")
        onboard.add_argument("--repo", required=True, help="Lokaler Checkout; wird ausschließlich gelesen")
        onboard.add_argument("--repository", required=True, help="GitHub owner/repository")
        onboard.add_argument("--stack", choices=("auto", "node", "python", "custom"), default="auto")
        onboard.add_argument("--out", required=True, help="Neuer Overlay-Ordner außerhalb des Checkouts")
    doctor = commands.add_parser("doctor", help="Repository-Profil und lokale Voraussetzungen ohne Codeausführung prüfen")
    doctor.add_argument("--repo", required=True)
    doctor.add_argument("--config", required=True)
    args = parser.parse_args()
    if args.command == "inspect":
        inventory = inspect_repo(args.repo)
        if args.out:
            write_new_json(args.out, inventory)
        else:
            print(json.dumps(inventory, ensure_ascii=False, indent=2))
    elif args.command == "stage":
        stage(None, args.out, config=read_json(args.config))
        print("Overlay erstellt: " + args.out)
    elif args.command == "onboard":
        from repository_factory import onboard_repository
        onboard_repository(args.repo, args.repository, args.out, stack=args.stack)
        print("Allgemeine Integration erstellt: " + args.out + "; Profil vor Aktivierung prüfen.")
    elif args.command == "doctor":
        from repository_factory import doctor_repository
        print(json.dumps(doctor_repository(args.repo, read_json(args.config)), ensure_ascii=False, indent=2))
    else:
        config = read_json(args.config)
        if args.command == "validate":
            validate_config(config, require_reviewed=not args.schema_only)
            if args.task:
                validate_task(read_json(args.task))
            print("Schema gültig; inhaltliche Prüfung und Freigaben bleiben erforderlich.")
        elif args.command == "prepare":
            prepare(config, read_json(args.task), args.out, args.assistant)
            print("Arbeitspaket erstellt: " + args.out)
        elif args.command == "run":
            return run_checks(args.repo, config, read_json(args.task), args.report, args.image, args.patch)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print("FEHLER: " + str(error), file=sys.stderr)
        sys.exit(2)
