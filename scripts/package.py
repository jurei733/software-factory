"""Build a portable source distribution; excludes run reports and Python caches."""
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

root = Path(__file__).resolve().parents[1]
target = root / "dist" / "software-factory.zip"
target.parent.mkdir(exist_ok=True)
entries = ["factory.py", "repository_factory.py", "tares_adapter.py", "isolated_runner.py", "connector_publisher.py", "pilot_checks.py", "runtime_safety.py", "README.md", "SECURITY.md", "Dockerfile.factory-checks", "Dockerfile.tares-checks", ".dockerignore", ".gitignore", ".github", "tasks", "profiles", "templates", "examples", "scripts", "tests", "docs", "config"]
for entry in entries:
    if not (root / entry).exists():
        raise FileNotFoundError("Erforderlicher Paketbestandteil fehlt: " + entry)
with ZipFile(target, "w", compression=ZIP_DEFLATED) as archive:
    for entry in entries:
        source = root / entry
        for path in ([source] if source.is_file() else sorted(source.rglob("*"))):
            if path.is_file() and not {"__pycache__", "node_modules", ".venv", "dist"}.intersection(path.relative_to(root).parts) and path.suffix != ".pyc":
                archive.write(path, arcname=str(Path("software-factory") / path.relative_to(root)))
print("Paket gebaut: " + str(target))
