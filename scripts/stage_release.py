"""Verify a signed factory bundle, install immutably, health-check and retain a rollback slot."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from runtime_safety import runtime_path
from publish_delivery import read_manifest, sha256
from verify_promotion import verify


def atomic_json(path, value):
    if path.is_symlink():
        raise ValueError("State must not be a symlink")
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".factory-state-", mode="w", delete=False) as stream:
        temporary = Path(stream.name); json.dump(value, stream, indent=2); stream.write("\n")
    try:
        temporary.chmod(0o600); os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def members(package):
    entries = package.infolist(); names = [item.filename for item in entries]
    if len(entries) > 10000 or len({name.casefold() for name in names}) != len(names) or sum(item.file_size for item in entries) > 200 * 1024 ** 2:
        raise ValueError("Unsafe distribution size or duplicate names")
    for item in entries:
        name = PurePosixPath(item.filename)
        if (len(name.parts) < 2 or name.parts[0] != "software-factory" or ".." in name.parts or
                str(name) != item.filename or "\\" in item.filename or any(":" in part for part in name.parts) or
                (item.external_attr >> 16) & 0o170000 not in (0, 0o100000) or item.file_size > 32 * 1024 ** 2):
            raise ValueError("Unsafe distribution member")
    return entries


def health(installation, state, root):
    if installation.is_symlink() or not installation.is_dir():
        raise ValueError("Installed distribution is missing or unsafe")
    actual = {}
    for path in installation.rglob("*"):
        if path.is_symlink():
            raise ValueError("Installed distribution contains links")
        if path.is_file():
            actual[path.relative_to(installation).as_posix()] = sha256(path)
    if actual != state["files"]:
        raise ValueError("Installed distribution changed; refusing activation")
    release = json.loads((installation / "factory-release.json").read_text())
    if release.get("source_commit") != state["source_commit"] or release.get("version") != state["version"]:
        raise ValueError("Installed release identity differs from the selected source")
    env = {**{key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL") if key in os.environ},
           "HOME": "/nonexistent", "PYTHONDONTWRITEBYTECODE": "1"}
    with tempfile.TemporaryDirectory(prefix="health-", dir=root) as work:
        base = Path(work); project = base / "project"; project.mkdir(); (project / "README.md").write_text("Synthetic health project")
        cli = [sys.executable, str(installation / "factory.py")]
        for args in [["--help"], ["onboard", "--repo", str(project), "--repository", "example-owner/health", "--out", str(base / "overlay")],
                     ["doctor", "--repo", str(project), "--config", str(base / "overlay/.factory/config.json")]]:
            subprocess.run(cli + args, cwd=base, env=env, check=True, timeout=30, capture_output=True, text=True)
    return True


def install(package_path, directory, source_commit):
    """Install a previously verified payload; external callers must use the signed-bundle CLI."""
    if not isinstance(source_commit, str) or not re.fullmatch(r"[a-f0-9]{40}", source_commit):
        raise ValueError("Independently pin the source commit")
    root = runtime_path(directory); root.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in ("versions", "records"):
        if (root / name).is_symlink():
            raise ValueError("Installation metadata must not traverse links")
        (root / name).mkdir(exist_ok=True)
    with ZipFile(package_path) as package:
        entries = members(package)
        release = json.loads(package.read("software-factory/factory-release.json"))
        version = release.get("version", "")
        if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) or release.get("source_commit") != source_commit:
            raise ValueError("Distribution is not the independently selected version/source")
        key = version + "-" + sha256(package_path)[:16]
        destination = root / "versions" / key
        state = {"schema_version": 1, "version": version, "source_commit": source_commit,
                 "installation": key, "package_sha256": sha256(package_path), "files": {}}
        with tempfile.TemporaryDirectory(prefix="unpack-", dir=root) as temp:
            staging = Path(temp)
            for entry in entries:
                name = PurePosixPath(entry.filename).relative_to("software-factory").as_posix()
                path = staging / name; path.parent.mkdir(parents=True, exist_ok=True)
                data = package.read(entry)
                path.write_bytes(data); path.chmod(0o755 if entry.external_attr >> 16 & 0o111 else 0o644)
                state["files"][name] = hashlib.sha256(data).hexdigest()
            health(staging, state, root)
            if destination.exists() or destination.is_symlink():
                health(destination, state, root)
            else:
                shutil.move(str(staging), destination)
    # Every selectable version has a separate immutable content record; activation is last.
    record = root / "records" / (key + ".json")
    if record.is_symlink():
        raise ValueError("Installed version record must not be a link")
    if record.exists() and json.loads(record.read_text()) != state:
        raise ValueError("Installed version record differs")
    atomic_json(record, state)
    current = root / "current.json"
    if current.exists():
        previous = read_slot(root, "current")
        if previous["installation"] != key:
            atomic_json(root / "previous.json", previous)
    atomic_json(current, state)
    return {"status": "healthy", "version": version, "source_commit": source_commit,
            "package_sha256": state["package_sha256"], "production_deployment_performed": False}


def read_slot(root, slot):
    path = root / (slot + ".json")
    if path.is_symlink():
        raise ValueError("Activation slot must not be a link")
    state = json.loads(path.read_text())
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+-[a-f0-9]{16}", state.get("installation", "")):
        raise ValueError("Invalid installed version identity")
    record = root / "records" / (state["installation"] + ".json")
    if record.is_symlink() or json.loads(record.read_text()) != state:
        raise ValueError("Activation slot differs from installed version record")
    return state


def rollback(directory):
    root = runtime_path(directory)
    previous = read_slot(root, "previous")
    health(root / "versions" / previous["installation"], previous, root)
    atomic_json(root / "current.json", previous)
    return {"status": "restored", "version": previous["version"], "source_commit": previous["source_commit"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--rollback", action="store_true")
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--repository")
    parser.add_argument("--source-sha")
    parser.add_argument("--default-branch", default="main")
    parser.add_argument("--attestation-bundle", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.rollback:
        report = rollback(args.root)
    else:
        if not all((args.archive, args.repository, args.source_sha)):
            parser.error("Signed staging requires archive, repository and independently pinned source-sha")
        evidence = verify(args.archive, "production", repository=args.repository, default_branch=args.default_branch,
                          expected_source_sha=args.source_sha, attestation_bundle=args.attestation_bundle)
        with ZipFile(args.archive) as archive:
            metadata = read_manifest(archive)
            if [item["path"] for item in metadata["files"]] != ["software-factory.zip"]:
                raise ValueError("Staging accepts exactly the factory distribution")
            with tempfile.TemporaryDirectory() as directory:
                payload = Path(directory) / "software-factory.zip"; payload.write_bytes(archive.read("payload/software-factory.zip"))
                report = dict(install(payload, args.root, args.source_sha), provenance_verified=True, verification=evidence)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("x") as stream:
        json.dump(report, stream, indent=2); stream.write("\n")


if __name__ == "__main__":
    main()
