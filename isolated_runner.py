"""Run approved checks in disposable Docker workspaces without host credentials or networking."""
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
import time
import uuid


def git_command(root, *args):
    """Local object operations never inherit executable Git configuration or helpers."""
    return ["git", "-C", str(root), "-c", "core.hooksPath=/dev/null", "-c",
            "core.fsmonitor=false", "-c", "core.attributesFile=/dev/null", *args]


def git_environment():
    return {**{key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "TMPDIR") if key in os.environ},
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_ATTR_NOSYSTEM": "1", "GIT_NO_REPLACE_OBJECTS": "1", "GIT_NO_LAZY_FETCH": "1",
            "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}


def git_bytes(root, *args):
    return subprocess.check_output(git_command(root, *args), env=git_environment())


def git(root, *args):
    return git_bytes(root, *args).decode().strip()


def mount_paths(root, paths, *, scratch=False):
    """Refuse root, control metadata, symlinks and overlapping writable mounts."""
    result = []
    for value in paths:
        path = Path(value)
        if (path.is_absolute() or not path.parts or ".." in path.parts or path.parts[0] == ".git" or
                any(character in value for character in (",", "\n", "\r", "\0"))):
            raise ValueError("Unsafe writable path: " + value)
        target = root / path
        for ancestor in (target, *target.parents):
            if ancestor == root:
                break
            if ancestor.is_symlink():
                raise ValueError("Writable mounts cannot traverse symlinks")
        if scratch and target.exists() and (target.is_file() or any(p.is_file() for p in target.rglob("*"))):
            raise ValueError("Scratch mount contains source files: " + value)
        if not target.exists():
            if not scratch:
                # A missing input (including an intentional deletion) must remain missing.
                continue
            target.mkdir(parents=True)
        result.append(target)
    result = sorted(set(result), key=lambda p: len(p.parts))
    if any(parent in child.parents for i, parent in enumerate(result) for child in result[i + 1:]):
        raise ValueError("Writable mounts overlap")
    return result


def snapshot(repo, target):
    """Copy exact committed blobs without filters, export attributes or host hook execution."""
    repo, target = Path(repo).resolve(), Path(target).resolve()
    commit = git(repo, "rev-parse", "HEAD")
    copy_git_tree(repo, commit, target)
    # `git status` may itself execute a repository's clean filter. Compare objects and
    # raw tracked bytes instead. Smudged/EOL-transformed checkouts fail closed explicitly.
    expected = {}
    for item in git_bytes(repo, "ls-tree", "-rz", "--full-tree", commit).split(b"\0"):
        if item:
            info, name = item.split(b"\t", 1)
            mode, kind, sha = info.split()
            expected[name] = (mode, sha, b"0")
    indexed = {}
    for item in git_bytes(repo, "ls-files", "--stage", "-z").split(b"\0"):
        if item:
            info, name = item.split(b"\t", 1)
            mode, sha, stage = info.split()
            if name in indexed:
                raise ValueError("Sandbox requires an unmerged-free committed index")
            indexed[name] = (mode, sha, stage)
    if indexed != expected or git_bytes(repo, "ls-files", "--others", "--exclude-standard", "-z"):
        raise ValueError("Sandbox requires a clean committed checkout; submit a scoped patch separately")
    for name, identity in file_manifest(target).items():
        path = Path(repo) / name
        if any(parent.is_symlink() for parent in path.parents if parent != Path(repo) and Path(repo) in parent.parents):
            raise ValueError("Tracked checkout path traverses a symlink")
        actual = (("symlink", os.readlink(path)) if path.is_symlink() else
                  (hashlib.sha256(path.read_bytes()).hexdigest(), bool(path.stat().st_mode & 0o111)) if path.is_file() else None)
        if actual != identity:
            raise ValueError("Checkout differs from raw committed bytes/modes (modified, smudged or EOL-transformed); use a raw clean checkout")
    return commit


def extract_archive(data, target):
    """Extract Git files first; only then create and validate repository-internal links."""
    if any(target.iterdir()):
        raise ValueError("Snapshot destination must be empty")
    links = []
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        for entry in archive.getmembers():
            path = Path(entry.name)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("Snapshot contains an unsafe path")
            destination = target / path
            if entry.isdir():
                destination.mkdir(parents=True, exist_ok=True)
            elif entry.isfile():
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(archive.extractfile(entry).read())
                destination.chmod(0o755 if entry.mode & 0o111 else 0o644)
            elif entry.issym() and not Path(entry.linkname).is_absolute():
                links.append((destination, entry.linkname))
            else:
                raise ValueError("Snapshot contains an unsafe link")
    for destination, link in links:
        for ancestor in destination.parents:
            if ancestor == target:
                break
            if ancestor.is_symlink():
                raise ValueError("Snapshot link has a symbolic-link parent")
        resolved_target = (destination.parent / link).resolve()
        if resolved_target != target and target not in resolved_target.parents:
            raise ValueError("Snapshot link escapes the isolated workspace")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.symlink_to(link)
    for destination, _ in links:
        resolved = destination.resolve()
        if resolved != target and target not in resolved.parents:
            raise ValueError("Snapshot link escapes the isolated workspace")


def copy_git_tree(repo, revision, target, prefix=None):
    """Read raw objects: never execute checkout filters/hooks or honor export-ignore/subst."""
    target = Path(target).resolve()
    if any(path.name != ".git" for path in target.iterdir()):
        raise ValueError("Raw snapshot destination must contain only optional worktree metadata")
    argv = ["ls-tree", "-rz", "--full-tree", revision]
    if prefix:
        argv += ["--", prefix]
    entries = git_bytes(repo, *argv).split(b"\0")
    links = []
    for entry in entries:
        if not entry:
            continue
        metadata, raw_path = entry.split(b"\t", 1)
        mode, kind, sha = metadata.decode().split()
        path = Path(os.fsdecode(raw_path))
        if path.is_absolute() or ".." in path.parts or ".git" in path.parts or kind != "blob" or mode not in ("100644", "100755", "120000"):
            raise ValueError("Unsafe raw Git snapshot entry")
        destination = target/path
        destination.parent.mkdir(parents=True, exist_ok=True)
        data = git_bytes(repo, "cat-file", "blob", sha)
        if mode == "120000":
            links.append((destination, os.fsdecode(data)))
        else:
            destination.write_bytes(data)
            destination.chmod(0o755 if mode == "100755" else 0o644)
    for destination, link in links:
        resolved = (destination.parent/link).resolve()
        if Path(link).is_absolute() or (resolved != target and target not in resolved.parents):
            raise ValueError("Raw snapshot link escapes source")
        if any(parent.is_symlink() for parent in destination.parents if parent != target and target in parent.parents):
            raise ValueError("Raw snapshot link has a symlink parent")
        destination.symlink_to(link)
    file_manifest(target)  # Reject link chains that escape after all links are present.


def reserve_attempt(directory, task, config_digest, source_commit, *, inputs=None):
    """The ledger is outside all container mounts and binds attempts to an unchanged task."""
    directory.mkdir(parents=True, exist_ok=True)
    import fcntl
    lock = (directory / ("." + task["id"] + ".lock")).open("a+")
    with lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Task execution session is already locked") from None
        ledger = directory / ("." + task["id"] + ".attempts.json")
        task_digest = hashlib.sha256(json.dumps(task, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        identity = {"task_sha256": task_digest, "config_sha256": config_digest, "source_commit": source_commit}
        state = json.loads(ledger.read_text()) if ledger.exists() else dict(identity, attempts=0, history=[])
        if any(state.get(key) != value for key, value in identity.items()) or "history" not in state:
            raise ValueError("Task, configuration or source changed inside an existing execution session")
        if state["attempts"] >= task["max_attempts"]:
            raise ValueError("Task attempt budget exhausted")
        state["attempts"] += 1
        state["history"].append({"attempt": state["attempts"], "inputs": inputs})
        temporary = ledger.with_suffix(".new")
        temporary.write_text(json.dumps(state, indent=2) + "\n")
        temporary.replace(ledger)
        return state["attempts"]


def file_manifest(root):
    manifest = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if relative.parts[0] == ".git":
            continue
        if path.is_symlink():
            resolved = path.resolve()
            if resolved != root and root not in resolved.parents:
                raise ValueError("Patch introduced a symbolic link outside the workspace")
            manifest[relative.as_posix()] = ("symlink", os.readlink(path))
            continue
        if path.is_file():
            manifest[relative.as_posix()] = (hashlib.sha256(path.read_bytes()).hexdigest(),
                                           bool(path.stat().st_mode & 0o111))
    return manifest


def dependency_plan(root, dependencies, writable):
    """Image dependencies may fill empty reserved paths, never hide source or writable mounts."""
    result = []
    for dependency in dependencies:
        target = root / dependency["target"]
        for ancestor in (target, *target.parents):
            if ancestor == root:
                break
            if ancestor.is_symlink():
                raise ValueError("Dependency target cannot traverse symlinks")
        if target.exists() and (not target.is_dir() or any(target.iterdir())):
            raise ValueError("Dependency target would hide source files")
        if any(target == path or target in path.parents for path in writable):
            raise ValueError("Dependency target overlaps a writable mount")
        if any(target == root / item["target"] or target in (root / item["target"]).parents or
               root / item["target"] in target.parents for item in result):
            raise ValueError("Dependency targets overlap")
        target.mkdir(parents=True, exist_ok=True)
        result.append(dict(dependency))
    return result


def container_command(name, image, root, writable, dependencies=()):
    user = os.getuid() if hasattr(os, "getuid") else 1000
    group = os.getgid() if hasattr(os, "getgid") else 1000
    if user == 0:
        raise ValueError("Run the factory controller as a non-root user")
    command = ["docker", "run", "--detach", "--name", name, "--label", "factory.factory.sandbox=true",
               "--network", "none", "--read-only", "--cap-drop", "ALL",
               "--security-opt", "no-new-privileges:true", "--pids-limit", "256",
               "--memory", "4g", "--cpus", "2", "--user", "%s:%s" % (user, group),
               "--tmpfs", "/tmp:rw,noexec,nosuid,size=256m,mode=1777", "--init",  # nosec B108 -- container tmpfs, never a host path
               "--env", "HOME=/tmp/home", "--env", "CI=true", "--env", "NX_DAEMON=false",
               "--env", "NX_NO_CLOUD=true", "--env", "PYTHONDONTWRITEBYTECODE=1",
               "--env", "TMPDIR=/tmp", "--env", "XDG_CACHE_HOME=/tmp/cache",
               "--env", "GIT_OPTIONAL_LOCKS=0",
               "--mount", "type=bind,src=%s,dst=/workspace,readonly" % root]
    for path in writable:
        command += ["--mount", "type=bind,src=%s,dst=/workspace/%s" % (path, path.relative_to(root).as_posix())]
    for dependency in dependencies:
        command += ["--mount", "type=volume,src=%s,dst=/workspace/%s,readonly,volume-nocopy" %
                    (dependency["volume"], dependency["target"])]
    return command + ["--workdir", "/workspace", "--entrypoint", "/bin/sh", image,
                      "-c", "while :; do sleep 3600; done"]


def run_checks(repo, config, task, report, image, *, patch=None):
    import factory

    factory.validate_config(config)
    factory.validate_task(task)
    if not image or not re.fullmatch(r"(?:[a-zA-Z0-9./:_-]+@)?sha256:[0-9a-f]{64}", image):
        raise ValueError("Select an approved toolchain image by immutable @sha256 digest")
    repo, report = Path(repo).resolve(), Path(report).resolve()
    report.parent.mkdir(parents=True, exist_ok=True)
    if report.exists():
        raise FileExistsError(report)
    if repo == report.parent or repo in report.parent.parents:
        raise ValueError("Keep execution reports and session state outside the source checkout")
    with tempfile.TemporaryDirectory(prefix="factory-factory-", dir=report.parent) as directory:
        root = Path(directory) / "source"
        root.mkdir()
        source = snapshot(repo, root)
        for rule in config["rules_files"]:
            if not factory.inside(root, rule).is_file():
                raise ValueError("Project rule is absent: " + rule)
        patch_digest = None
        if patch:
            patch_bytes = Path(patch).read_bytes()
            patch_digest = hashlib.sha256(patch_bytes).hexdigest()
            applied_patch = Path(directory) / "submitted.patch"
            applied_patch.write_bytes(patch_bytes)
            # Own empty Git metadata prevents discovery of an enclosing checkout.
            # No add/commit: neither filters, signing programs nor project hooks run.
            templates = Path(directory) / "empty-templates"
            templates.mkdir()
            subprocess.run(git_command(root, "init", "-q", "--template=" + str(templates)),
                           env=git_environment(), check=True)
            before = file_manifest(root)
            subprocess.run(git_command(root, "apply", "--check", str(applied_patch)), env=git_environment(), check=True)
            subprocess.run(git_command(root, "apply", str(applied_patch)), env=git_environment(), check=True)
            after = file_manifest(root)
            changed = {name for name in set(before) | set(after) if before.get(name) != after.get(name)}
            if any(not any(name == allowed or name.startswith(allowed.rstrip("/") + "/")
                           for allowed in task["allowed_paths"]) for name in changed):
                raise ValueError("Patch changes files outside allowed_paths")
        prepared_digest = factory.digest(file_manifest(root))
        scratch = config.get("execution", {}).get("scratch_paths", ["dist", "coverage", ".nx", "test-results"])
        writable = mount_paths(root, task["allowed_paths"]) + mount_paths(root, scratch, scratch=True)
        # A writable directory must never overlap another writable or scratch mount.
        if any(a != b and a in b.parents for a in writable for b in writable):
            raise ValueError("Writable and scratch paths overlap")
        dependencies = dependency_plan(root, config.get("execution", {}).get("dependencies", []), writable)
        for step in config["steps"]:
            if not factory.inside(root, step.get("cwd", ".")).is_dir():
                raise ValueError("Check working directory is absent")
        inputs = {"patch_sha256": patch_digest, "prepared_source_sha256": prepared_digest, "image": image}
        attempt = reserve_attempt(report.parent, task, factory.digest(config), source, inputs=inputs)
        result = {"schema_version": 2, "status": "running", "project": config["project"],
                  "task_id": task["id"], "attempt": attempt, "source_commit": source,
                  "task_sha256": factory.digest(task), "config_sha256": factory.digest(config),
                  "image": image, "network": "none", "host_credentials": False,
                  "patch_sha256": patch_digest, "prepared_source_sha256": prepared_digest,
                  "dependencies": [{"image_path": item["image_path"], "target": item["target"], "read_only": True}
                                   for item in dependencies],
                  "max_minutes": task["max_minutes"], "started_at": datetime.now(timezone.utc).isoformat(),
                  "steps": []}
        name = "factory-factory-" + uuid.uuid4().hex
        result["container"] = name
        deadline = time.monotonic() + task["max_minutes"] * 60
        with report.open("x", encoding="utf-8") as stream:
            failed = False
            cancelled = False
            container_started = False
            completed_all = False
            try:
                inspected = subprocess.run(["docker", "image", "inspect", image], check=True,
                                           capture_output=True, text=True, timeout=max(0.01, min(10, deadline - time.monotonic())))
                if json.loads(inspected.stdout)[0].get("Config", {}).get("Volumes"):
                    raise ValueError("Toolchain image must not declare implicit writable volumes")
                for index, item in enumerate(dependencies):
                    item.update(volume=name + "-dependency-" + str(index), provider=name + "-provider-" + str(index))
                    # Docker copies image files into a new volume during create. No image code is started.
                    subprocess.run(["docker", "create", "--name", item["provider"], "--network", "none",
                                    "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
                                    "--user", "%s:%s" % (os.getuid(), os.getgid()),
                                    "--mount", "type=volume,src=%s,dst=%s,readonly" % (item["volume"], item["image_path"]),
                                    "--entrypoint", "/bin/true", image], check=True, capture_output=True, text=True,
                                   timeout=max(0.01, min(60, deadline - time.monotonic())))
                subprocess.run(container_command(name, image, root, writable, dependencies), check=True,
                               capture_output=True, text=True, timeout=max(0.01, min(60, deadline - time.monotonic())))
                container_started = True
                for item in dependencies:
                    subprocess.run(["docker", "exec", name, "/bin/sh", "-c", 'test -n "$(ls -A "$1")"',
                                    "dependency-check", "/workspace/" + item["target"]], check=True,
                                   capture_output=True, text=True, timeout=max(0.01, min(10, deadline - time.monotonic())))
                for step in config["steps"]:
                    item = {"id": step["id"], "category": step["category"], "argv": step["argv"]}
                    if failed:
                        item["status"] = "skipped"
                    else:
                        remaining = deadline - time.monotonic()
                        started = time.monotonic()
                        try:
                            if remaining <= 0:
                                raise subprocess.TimeoutExpired("task budget", 0)
                            completed = subprocess.run(
                                ["docker", "exec", "--workdir", "/workspace/" + step.get("cwd", "."),
                                 name, *step["argv"]], timeout=min(step["timeout_seconds"], remaining), check=False)
                            item.update(status="passed" if completed.returncode == 0 else "failed",
                                        returncode=completed.returncode)
                        except subprocess.TimeoutExpired:
                            item["status"] = "timeout"
                        except OSError as error:
                            item.update(status="error", error=str(error))
                        item["duration_seconds"] = round(time.monotonic() - started, 3)
                        failed = item["status"] != "passed"
                    result["steps"].append(item)
                completed_all = True
            except KeyboardInterrupt:
                cancelled = True
                failed = True
            except (OSError, subprocess.SubprocessError, ValueError, RuntimeError) as error:
                result["runner_error"] = str(error)
                failed = True
            finally:
                try:
                    cleanup = subprocess.run(["docker", "rm", "--force", name], capture_output=True,
                                             text=True, timeout=30, check=False)
                    if container_started and cleanup.returncode:
                        result["cleanup_error"] = cleanup.stderr[-1000:]
                        failed = True
                except (OSError, subprocess.SubprocessError) as error:
                    result["cleanup_error"] = str(error)
                    failed = True
                for item in reversed(dependencies):
                    if "volume" not in item:
                        continue
                    try:
                        provider = subprocess.run(["docker", "container", "inspect", item["provider"]],
                                                  capture_output=True, text=True, timeout=30, check=False)
                        if provider.returncode == 0:
                            subprocess.run(["docker", "rm", "--force", item["provider"]],
                                           capture_output=True, text=True, timeout=30, check=True)
                        elif "no such" not in provider.stderr.lower():
                            raise RuntimeError("Cannot verify dependency provider cleanup")
                        volume = subprocess.run(["docker", "volume", "inspect", item["volume"]],
                                                capture_output=True, text=True, timeout=30, check=False)
                        if volume.returncode == 0:
                            subprocess.run(["docker", "volume", "rm", item["volume"]],
                                           capture_output=True, text=True, timeout=30, check=True)
                        elif "no such" not in volume.stderr.lower():
                            raise RuntimeError("Cannot verify dependency volume cleanup")
                    except (OSError, subprocess.SubprocessError, RuntimeError) as error:
                        result["dependency_cleanup_error"] = str(error)
                        failed = True
                failed = failed or not completed_all
                result["status"] = "cancelled" if cancelled else "failed" if failed else "passed"
                result["finished_at"] = datetime.now(timezone.utc).isoformat()
                json.dump(result, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
        return 130 if cancelled else 1 if failed else 0
