"""Build a portable source distribution; excludes run reports and Python caches."""
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED, ZipInfo
import json
import os
import re
import subprocess

root = Path(__file__).resolve().parents[1]
target = root / "dist" / "software-factory.zip"
target.parent.mkdir(exist_ok=True)
entries = ["factory.py", "repository_factory.py", "integration_updates.py", "tares_adapter.py", "isolated_runner.py", "connector_publisher.py", "pilot_checks.py", "runtime_safety.py", "README.md", "SECURITY.md", "VERSION", "LICENSE", "CONTRIBUTING.md", "CHANGELOG.md", "Dockerfile.factory-checks", "Dockerfile.tares-checks", ".dockerignore", ".gitignore", ".github", "tasks", "profiles", "templates", "examples", "scripts", "tests", "docs", "config"]
for entry in entries:
    if not (root / entry).exists():
        raise FileNotFoundError("Erforderlicher Paketbestandteil fehlt: " + entry)
with ZipFile(target, "w", compression=ZIP_DEFLATED) as archive:
    for entry in entries:
        source = root / entry
        for path in ([source] if source.is_file() else sorted(source.rglob("*"))):
            if path.is_file() and not {"__pycache__", "node_modules", ".venv", "dist"}.intersection(path.relative_to(root).parts) and path.suffix != ".pyc":
                if path.is_symlink():
                    raise ValueError("Package source must not contain links")
                entry_info = ZipInfo(str(Path("software-factory") / path.relative_to(root)), (2020, 1, 1, 0, 0, 0))
                entry_info.compress_type = ZIP_DEFLATED
                entry_info.external_attr = (0o100755 if path.stat().st_mode & 0o111 else 0o100644) << 16
                archive.writestr(entry_info, path.read_bytes())
    commit = os.environ.get("GITHUB_SHA")
    if not commit:
        result = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=False)
        commit = result.stdout.strip() if result.returncode == 0 else None
    if commit is not None and not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise ValueError("Invalid package source commit")
    metadata = {"schema_version": 1, "version": (root / "VERSION").read_text().strip(), "source_commit": commit}
    info = ZipInfo("software-factory/factory-release.json", (2020, 1, 1, 0, 0, 0))
    info.compress_type = ZIP_DEFLATED; info.external_attr = 0o100644 << 16
    archive.writestr(info, json.dumps(metadata, sort_keys=True) + "\n")
print("Paket gebaut: " + str(target))
