"""Versioned overlay ownership, drift detection and explicitly applied, recoverable updates."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

import factory

ROOT = Path(__file__).resolve().parent
MANIFEST = ".factory/integration.json"
CUSTOM = {".factory/config.json", ".factory/PROJECT_RULES.md"}
MANAGED = {
    ".factory/factory.py", ".factory/repository_factory.py", ".factory/isolated_runner.py",
    ".factory/prepare_task.py", ".factory/needs-configuration.py",
    ".factory/scripts/install_scanner.py", ".factory/scripts/security_gate.py",
    ".factory/config/security/tools.json", ".factory/config/trivy.yaml",
    ".github/workflows/factory-quality.yml", ".github/workflows/factory-task.yml", "INTEGRATION.md",
}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def managed(name):
    factory.require(factory.relative_path(name) and not {".git", ".."}.intersection(Path(name).parts),
                    "Unsafe managed integration path")
    return name in MANAGED


def regular(root, name, *, missing=False):
    path = root / name
    factory.require(not any(item.is_symlink() for item in (path, *path.parents) if item == root or root in item.parents),
                    "Integration path must not traverse links: " + name)
    factory.require((missing and not path.exists()) or path.is_file(), "Integration file is missing or unsafe: " + name)
    return path


def identity():
    version = (ROOT / "VERSION").read_text().strip()
    factory.require(re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version), "Invalid factory version")
    release = ROOT / "factory-release.json"
    source = factory.read_json(release).get("source_commit") if release.is_file() else factory.git_commit(ROOT)
    return {"version": version, "source_commit": source}


def record(output):
    files = {}
    for path in sorted(output.rglob("*")):
        name = path.relative_to(output).as_posix()
        if path.is_file() and managed(name):
            files[name] = digest(regular(output, name))
    factory.write_new_json(output / MANIFEST, {"schema_version": 1, "factory": identity(), "files": files})


def read_manifest(root):
    value = factory.read_json(regular(root, MANIFEST))
    factory.require(value.get("schema_version") == 1 and isinstance(value.get("files"), dict) and value["files"],
                    "Integration ownership manifest is missing or invalid; migrate manually")
    for name, sha in value["files"].items():
        factory.require(managed(name) and re.fullmatch(r"[a-f0-9]{64}", sha), "Invalid managed file ownership")
    return value


def drift(root, manifest):
    changes = []
    for name, sha in manifest["files"].items():
        path = regular(root, name, missing=True)
        if not path.exists() or digest(path) != sha:
            changes.append(name)
    return changes


def upgrade(repo, output, *, apply=False):
    """No network, hooks, project execution or Git mutations. User profiles/tasks are preserved."""
    from repository_factory import export_repository
    root, output = Path(repo).resolve(), Path(output).resolve()
    factory.require(root.is_dir() and root != output and root not in output.parents,
                    "Create upgrade plan outside the consumer checkout")
    old = read_manifest(root)
    factory.require(not drift(root, old), "Managed integration drift; reconcile before upgrade: " + ", ".join(drift(root, old)))
    config = factory.read_json(regular(root, ".factory/config.json"))
    output.mkdir(parents=True, exist_ok=False)
    candidate = output / "candidate"
    export_repository(config, candidate)
    new = read_manifest(candidate)
    changes = []
    for name in sorted(set(old["files"]) | set(new["files"])):
        before, after = old["files"].get(name), new["files"].get(name)
        if before is None and regular(root, name, missing=True).exists():
            raise factory.FactoryError("New managed file collides with a local file: " + name)
        if before != after:
            changes.append({"path": name, "before": before, "after": after})
    plan = {"schema_version": 1, "from": old["factory"], "to": new["factory"],
            "manifest_before": digest(root / MANIFEST), "changes": changes,
            "preserved": [".factory/config.json", ".factory/PROJECT_RULES.md", ".factory/tasks/"], "applied": False}
    factory.write_new_json(output / "plan.json", plan)
    if not apply:
        return plan
    # Validate all leases and candidate bytes before the first project write.
    factory.require(digest(root / MANIFEST) == plan["manifest_before"] and not drift(root, old), "Integration moved after planning")
    for item in changes:
        name = item["path"]
        target = regular(root, name, missing=True)
        factory.require((digest(target) if target.exists() else None) == item["before"], "File moved after planning")
        if item["after"]:
            factory.require(digest(regular(candidate, name)) == item["after"], "Upgrade candidate changed")
    backup = output / "backup"; backup.mkdir()
    touched = []
    try:
        for name in [item["path"] for item in changes] + [MANIFEST]:
            target = regular(root, name, missing=True)
            if target.exists():
                saved = backup / name; saved.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(target, saved)
            touched.append(name)
            source = candidate / name
            if source.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".factory-update-", delete=False) as stream:
                    staged = Path(stream.name)
                    stream.write(source.read_bytes())
                try:
                    staged.chmod(0o644); os.replace(staged, target)
                finally:
                    staged.unlink(missing_ok=True)
            else:
                target.unlink()
    except BaseException:
        for name in reversed(touched):
            saved, target = backup / name, root / name
            if saved.exists():
                shutil.copyfile(saved, target)
            else:
                target.unlink(missing_ok=True)
        raise
    factory.require(not drift(root, new), "Applied integration does not match the candidate")
    plan["applied"] = True
    factory.write_new_json(output / "receipt.json", plan)
    return plan
