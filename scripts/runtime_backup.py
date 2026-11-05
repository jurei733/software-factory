"""Offline backup/restore of a stopped, quiescent local runtime. Never upload these private files."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from runtime_safety import runtime_path, owns_process

EXCLUDED = {"worktrees", "toolchain"}
LIMIT = 200 * 1024 ** 2


def file_digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def backup(directory, archive):
    root, archive = runtime_path(directory), Path(archive).resolve()
    if not root.is_dir() or root == archive.parent or root in archive.parents:
        raise ValueError("Backup must be outside the existing runtime")
    for name in ("daemon", "mcp"):
        identity = root / (name + ".json")
        if identity.is_symlink():
            raise ValueError("Runtime process identity must not be a link")
        if identity.exists() and owns_process(json.loads(identity.read_text()), root):
            raise ValueError("Stop the owned daemon and MCP before backup")
    state = root / "pilot.json"
    if state.is_symlink():
        raise ValueError("Runtime state must not be a link")
    if state.exists():
        for item in json.loads(state.read_text()).get("tasks", {}).values():
            if item.get("status") != "published" and not (item.get("status", "pending") == "pending" and item.get("attempts", 0) == 0):
                raise ValueError("Finish/publish active tasks before a quiescent runtime backup")
    files = []
    for folder, directories, names in os.walk(root, followlinks=False):
        if Path(folder) == root:
            directories[:] = [name for name in directories if name not in EXCLUDED]
        for name in directories + names:
            path = Path(folder) / name
            if path.is_symlink() or name == ".git":
                raise ValueError("Runtime backup contains an unsafe link or Git metadata")
        for name in names:
            path = Path(folder) / name
            if not path.is_file():
                raise ValueError("Runtime backup cannot contain special files")
            files.append(path)
    if len(files) > 10000 or sum(path.stat().st_size for path in files) > LIMIT:
        raise ValueError("Runtime backup exceeds its bounded size")
    archive.parent.mkdir(parents=True, exist_ok=True)
    # Reserve private output before writing credentials or logs.
    descriptor = os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    manifest = {"schema_version": 1, "excluded_disposable": sorted(EXCLUDED), "files": {}}
    try:
        with os.fdopen(descriptor, "wb") as stream, ZipFile(stream, "w", ZIP_DEFLATED) as package:
            total = 0
            for path in sorted(files):
                data = path.read_bytes(); name = path.relative_to(root).as_posix()
                total += len(data)
                if total > LIMIT:
                    raise ValueError("Runtime grew beyond backup bounds")
                manifest["files"][name] = hashlib.sha256(data).hexdigest()
                entry = ZipInfo("runtime/" + name); entry.compress_type = ZIP_DEFLATED; entry.external_attr = 0o100600 << 16
                package.writestr(entry, data)
            package.writestr("manifest.json", json.dumps(manifest, sort_keys=True))
    except BaseException:
        archive.unlink(missing_ok=True); raise
    return {"schema_version": 1, "sha256": file_digest(archive),
            "files": len(files), "private": True, "excluded_disposable": sorted(EXCLUDED)}


def restore(archive, directory, expected_sha256):
    archive, root = Path(archive), runtime_path(directory)
    if (root.exists() or archive.stat().st_size > LIMIT + 4 * 1024 ** 2 or
            not re.fullmatch(r"[a-f0-9]{64}", expected_sha256) or file_digest(archive) != expected_sha256):
        raise ValueError("Restore needs an absent destination and independently pinned backup digest")
    with ZipFile(archive) as package:
        entries = package.infolist(); names = [entry.filename for entry in entries]
        if len(names) > 10001 or len({name.casefold() for name in names}) != len(names) or sum(entry.file_size for entry in entries) > LIMIT:
            raise ValueError("Unsafe backup size or duplicate members")
        if package.getinfo("manifest.json").file_size > 4 * 1024 ** 2:
            raise ValueError("Oversized backup manifest")
        manifest = json.loads(package.read("manifest.json"))
        if manifest.get("schema_version") != 1 or not isinstance(manifest.get("files"), dict):
            raise ValueError("Invalid backup manifest")
        if set(names) != {"manifest.json", *("runtime/" + name for name in manifest["files"])}:
            raise ValueError("Backup membership differs from manifest")
        validated = []
        for entry in entries:
            if entry.filename == "manifest.json":
                continue
            path = PurePosixPath(entry.filename)
            if (path.is_absolute() or ".." in path.parts or ".git" in path.parts or "\\" in entry.filename or
                    str(path) != entry.filename or any(":" in part for part in path.parts) or
                    (entry.external_attr >> 16) & 0o170000 not in (0, 0o100000)):
                raise ValueError("Unsafe backup member")
            relative = path.relative_to("runtime")
            data = package.read(entry)
            if hashlib.sha256(data).hexdigest() != manifest["files"][relative.as_posix()]:
                raise ValueError("Backup file digest mismatch")
            validated.append((relative, data))
    # Validate every byte before creating the destination. Process identities are stale
    # at the new path and intentionally never restored as signal authority.
    root.mkdir(parents=True, mode=0o700)
    try:
        for relative, data in validated:
            if relative.as_posix() in ("daemon.json", "mcp.json", "daemon.pid", "mcp.pid"):
                continue
            target = root / str(relative); target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
    except BaseException:
        shutil.rmtree(root)
        raise
    return {"status": "restored", "files": len(validated), "services_started": False,
            "note": "Disposable worktrees/toolchains must be recreated; attempts and published receipts are preserved."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("backup", "restore")); parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True); parser.add_argument("--sha256")
    args = parser.parse_args()
    result = backup(args.runtime, args.archive) if args.command == "backup" else restore(args.archive, args.runtime, args.sha256 or "")
    print(json.dumps(result, indent=2))
