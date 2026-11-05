#!/usr/bin/env python3
"""Local Tares task ledger with bounded Codex workers and an optional native Claude crew."""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import select
import shutil
import signal
import subprocess
import sys
import time
import tempfile
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

import factory
from isolated_runner import file_manifest, copy_git_tree
from runtime_safety import runtime_path, process_identity, capture_identity, owns_process, stop_owned
from pilot_checks import run_project
from connector_publisher import CodexConnector, Publisher

ROOT = Path(__file__).resolve().parent
GITHUB_APP = "connector_76869538009648d5b282a4bb21c3d157"


def save(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".new")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    temporary.replace(path)


def config_at(path, repository=None):
    config = factory.read_json(path)
    if repository:
        config["repository"] = repository
    factory.require(config.get("schema_version") == 1, "Unsupported pilot schema")
    factory.require(re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", config.get("repository", "")),
                    "A GitHub owner/repository is required")
    factory.require(re.fullmatch(r"[a-z0-9-]+", config.get("project", "")), "Invalid project name")
    factory.require(factory.relative_path(config.get("working_directory")), "Invalid working directory")
    factory.require(config.get("baseline_version") == 1 and re.fullmatch(r"[0-9a-f]{64}", config.get("baseline_sha256", "")), "Pin the versioned unsolved baseline")
    ids = set()
    for task in config.get("tasks", []):
        factory.require(re.fullmatch(r"[A-Z0-9-]+", task.get("id", "")) and task["id"] not in ids, "Invalid task ID")
        ids.add(task["id"])
        factory.require(factory.nonempty(task.get("goal")) and factory.nonempty(task.get("title")), "Task goal/title missing")
        factory.require(task.get("acceptance") in ("zero", "json", "dependency"), "Unknown pilot acceptance")
        factory.require(type(task.get("max_attempts")) is int and 1 <= task["max_attempts"] <= 2, "At most two worker turns")
        factory.require(type(task.get("max_minutes")) is int and 1 <= task["max_minutes"] <= 15, "At most 15 minutes per task")
        factory.require(factory.text_list(task.get("allowed_paths")), "Task scope missing")
        for path in task["allowed_paths"]:
            factory.require(factory.relative_path(path) and path.startswith(config["working_directory"].rstrip("/") + "/"),
                            "Task paths must stay inside the pilot project")
    factory.require(bool(ids), "No pilot tasks")
    return config


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError("Tares credentials must never follow a redirect")


class Tares:
    def __init__(self, runtime, url=None):
        runtime = runtime_path(runtime)
        ports = runtime / "services.json"
        url = url or ("http://127.0.0.1:" + str(factory.read_json(ports)["api_port"]) if ports.exists() else "http://127.0.0.1:18787")
        parsed = urlsplit(url)
        factory.require(parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost") and
                        not parsed.username and not parsed.password and not parsed.query and not parsed.fragment,
                        "The local pilot requires a loopback Tares URL")
        self.url = url.rstrip("/")
        self.token = (Path(runtime) / "root-token").read_text().strip()
        self.opener = build_opener(NoRedirect())

    def call(self, method, path, data=None):
        factory.require(path.startswith("/") and "\r" not in path and "\n" not in path, "Invalid API path")
        req = Request(self.url + path, method=method,
                      data=json.dumps(data).encode() if data is not None else None,
                      headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
        with self.opener.open(req, timeout=30) as response:
            return json.load(response)


def environment(extra=()):
    # Use the installed CLI's own saved login; do not forward provider keys or unrelated service tokens.
    keys = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "USER", "LOGNAME", "CODEX_HOME", "SSL_CERT_FILE", "CODEX_CA_CERTIFICATE") + tuple(extra)
    return {key: os.environ[key] for key in keys if key in os.environ}


def permission_args(cwd, scratch, *, writable=False, source_root=None, allowed_paths=None):
    cwd, scratch = Path(cwd).resolve(), Path(scratch).resolve()
    root = Path(source_root).resolve() if source_root else cwd
    factory.require(root == cwd or root in cwd.parents, "Agent cwd must stay inside the pinned source root")
    access = {":minimal": "read", str(root): "read", str(cwd): "read", str(scratch): "write"}
    if writable:
        for path in allowed_paths if allowed_paths is not None else [cwd]:
            target = Path(path).resolve()
            factory.require(target == cwd or cwd in target.parents, "Agent write path escapes the project")
            access[str(target)] = "write"
    access[str(root/".git")] = "read"
    access[str(cwd/".git")] = "read"
    common = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(root), "rev-parse", "--path-format=absolute", "--git-common-dir"], capture_output=True, text=True)
    if common.returncode == 0:
        access[str(Path(common.stdout.strip()).resolve())] = "read"
    codex = shutil.which("codex")
    if codex:
        executable = Path(codex).resolve()
        access[str(Path(codex).absolute().parent)] = "read"
        access[str(executable.parent)] = "read"
        if executable.parent.name == "MacOS" and executable.parent.parent.name == "Contents":
            access[str(executable.parent.parent.parent)] = "read"
    node = shutil.which("node")
    if node:
        executable = Path(node).resolve()
        access[str(executable.parent)] = "read"
        access[str(executable.parent.parent / "lib/node_modules/npm")] = "read"
    table = "{" + ",".join(json.dumps(k)+"="+json.dumps(v) for k, v in access.items()) + "}"
    return ["-c", "permissions.factory_agent.filesystem=" + table,
            "-c", "permissions.factory_agent.network.enabled=false"]


def codex_command(cwd, output, *, writable=False, resume=None, source_root=None, allowed_paths=None):
    command = ["codex", "exec", "--ignore-user-config", "--disable", "multi_agent", "--disable", "plugins", "--disable", "apps",
               "--json", "--color", "never", "-c", 'default_permissions="factory_agent"',
               "-c", 'forced_login_method="chatgpt"', "-c", 'approval_policy="never"',
               "-c", 'web_search="disabled"', "-c", "allow_login_shell=false", "-c", "apps._default.enabled=false",
               "-c", 'shell_environment_policy.inherit="none"',
               "-c", 'shell_environment_policy.set={PATH='+json.dumps(os.environ.get("PATH", "/usr/bin:/bin"))+',HOME="/nonexistent",TMPDIR='+json.dumps(str(output.parent/(output.stem+"-tmp")))+',OPENSSL_CONF="/dev/null",GIT_CONFIG_GLOBAL="/dev/null",GIT_CONFIG_NOSYSTEM="1",GIT_TERMINAL_PROMPT="0",NPM_CONFIG_USERCONFIG="/dev/null"}']
    command += permission_args(cwd, output.parent/(output.stem+"-tmp"), writable=writable, source_root=source_root, allowed_paths=allowed_paths)
    command += ["--cd", str(cwd), "--output-last-message", str(output)]
    if resume:
        command += ["resume", resume]
    return command + ["-"]


def events(path):
    out = []
    for line in Path(path).read_text().splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def agent(cwd, prompt, directory, name, seconds, *, writable=False, resume=None, on_state=None, source_root=None, allowed_paths=None):
    log, answer = directory / (name + ".jsonl"), directory / (name + ".md")
    factory.require(not log.exists() and not answer.exists(), "Never overwrite an agent turn")
    (directory/(answer.stem+"-tmp")).mkdir()
    with log.open("x") as stream, (directory / (name + ".stderr")).open("x") as error:
        process = subprocess.Popen(codex_command(cwd, answer, writable=writable, resume=resume, source_root=source_root, allowed_paths=allowed_paths),
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=error,
                                   env=environment(), start_new_session=True)
        started_session = resume
        try:
            if on_state:
                identity = capture_identity(process.pid)
                factory.require(identity is not None, "Cannot identify the worker process")
                on_state(dict(identity, runtime=str(directory)))
            process.stdin.write(prompt.encode())
            process.stdin.close()
            deadline = time.monotonic() + max(1, seconds)
            pending = b""
            while True:
                if time.monotonic() > deadline:
                    raise subprocess.TimeoutExpired("codex", seconds)
                if select.select([process.stdout], [], [], 0.25)[0]:
                    data = os.read(process.stdout.fileno(), 65536)
                    if not data:
                        if pending:
                            stream.write(pending.decode("utf-8", errors="replace"))
                        break
                    pending += data
                    while b"\n" in pending:
                        raw, pending = pending.split(b"\n", 1)
                        line = raw.decode("utf-8", errors="replace")
                        stream.write(line+"\n")
                        stream.flush()
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if event.get("type") == "thread.started":
                            started_session = event["thread_id"]
                            if on_state:
                                on_state({"session": started_session})
            process.wait(timeout=max(1, deadline - time.monotonic()))
        except BaseException as failure:
            factory.terminate_check(process)  # Includes state/log/disk failures and callback exceptions.
            try:
                save(directory/(name+".result.json"), {"completed": False, "exit_code": process.returncode, "session": started_session,
                     "usage": [], "error": type(failure).__name__, "log": str(log)})
            except OSError:
                pass  # Preserve the original failure when storage itself is broken.
            raise
        finally:
            for pipe in (process.stdin, process.stdout):
                if not pipe.closed:
                    pipe.close()
    messages = events(log)
    started = next((x.get("thread_id") for x in messages if x.get("type") == "thread.started"), resume)
    result = {"session": started, "exit_code": process.returncode,
              "completed": process.returncode == 0 and any(x.get("type") == "turn.completed" for x in messages)
                           and not any(x.get("type") in ("turn.failed", "error") for x in messages),
              "usage": [x["usage"] for x in messages if x.get("usage")], "log": str(log)}
    save(directory / (name + ".result.json"), result)
    factory.require(result["completed"], "Agent failed or hit a limit; inspect " + str(log))
    return result, answer.read_text()


def check_scope(before, after, allowed, scratch=()):
    changed = {path for path in before.keys() | after.keys() if before.get(path) != after.get(path)}
    changed = {path for path in changed if not any(path == item or path.startswith(item + "/") for item in scratch)}
    factory.require(bool(changed), "Worker produced no change")
    factory.require(all(any(path == item or path.startswith(item.rstrip("/") + "/") for item in allowed)
                        for path in changed), "Worker changed files outside the ticket scope")
    factory.require(not any(after.get(path, (None,))[0] == "symlink" for path in changed), "No new/changed links in pilot output")
    return sorted(changed)


def acceptance(project, name, runtime, log_path):
    return run_project(project, name, runtime, log_path, full=False)["passed"]


def check_project(project, log_path, remaining, name, runtime):
    receipt = run_project(project, name, runtime, log_path, seconds=remaining())
    factory.require(receipt["passed"], "Independent ticket acceptance failed")
    return receipt


def verification_worker(folder, item):
    result = folder / ("worker-" + str(item["attempts"]) + ".result.json")
    # A timeout or controller crash can leave only the already-persisted session ID.
    return factory.read_json(result) if result.exists() else {"session": item.get("session"), "usage": []}


def initialize(repo, config, runtime, api):
    runtime = runtime_path(runtime)
    project_source = repo/config["working_directory"]
    validate_git_metadata(repo, project_source)
    baseline = {k: v for k, v in file_manifest(project_source).items() if Path(k).parts[0] not in ("node_modules", "dist")}
    factory.require(factory.digest(baseline) == config["baseline_sha256"], "Versioned pilot baseline changed; use a new reviewed baseline version")
    runtime.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="baseline-", dir=runtime) as directory:
        copy_git_tree(repo, "HEAD", Path(directory), config["working_directory"])
        committed = file_manifest(Path(directory)/config["working_directory"])
        factory.require(factory.digest(committed) == config["baseline_sha256"], "Commit the unchanged versioned baseline before initialization")
    path = runtime / "pilot.json"
    if path.exists():
        state = factory.read_json(path)
        factory.require(state["config_sha256"] == factory.digest(config), "Existing session belongs to another configuration")
        if not any(x["attempts"] for x in state["tasks"].values()):
            # Before the first turn only, bind the initialized tickets to the committed pilot checkout.
            state["base_local_commit"] = factory.git_commit(repo)
            save(path, state)
        return state
    existing = api.call("GET", "/api/projects")["projects"]
    project = next((x for x in existing if x["name"] == config["project"]), None)
    if project is None:
        project = api.call("POST", "/api/projects", {"template": "custom", "name": config["project"],
                      "kind": "software_factory", "goal": "Validate bounded agent work with human merge approval", "objects": []})
    pid = project["id"]
    api.call("PUT", "/api/crew/settings", {"autonomy": "L3-review", "releaser": False,
                                          "challenger": False, "builders_max": 1})
    milestone = api.call("POST", f"/api/projects/{pid}/milestones", {"name": "Three reviewed pilot pull requests",
                           "goal": "Bug fix, CLI feature and dependency update; each checked and held for human merge",
                           "checks": [{"check": "Factory CI and ticket acceptance", "expect": "passed"}]})
    tickets = {}
    existing = api.call("GET", f"/api/projects/{pid}/tickets")["tickets"]
    for task in config["tasks"]:
        ticket = next((x for x in existing if x["title"] == task["title"]), None)
        if ticket is None:
            doc = api.call("POST", f"/api/projects/{pid}/docs", {"kind": "working", "title": task["id"],
                           "body": json.dumps(task, indent=2)})
            ticket = api.call("POST", f"/api/projects/{pid}/tickets", {"title": task["title"], "repo": config["repository"],
                              "milestone": milestone["id"], "working_doc": doc["id"]})
        tickets[task["id"]] = {"ticket": ticket["id"], "status": "pending", "attempts": 0}
    api.call("POST", f"/api/projects/{pid}/handover", {"repos": [{"path": str(repo), "name": config["repository"]}],
                                                     "autonomy": "L3-review"})
    state = {"schema_version": 1, "project_id": pid, "repository": config["repository"], "base_local_commit": factory.git_commit(repo),
             "config_sha256": factory.digest(config), "tasks": tickets, "merge_policy": "human", "production_approved": False}
    save(path, state)
    return state


def run_ticket(repo, config, runtime, task, api, *, verification_note=None):
    runtime = runtime_path(runtime)
    state = initialize(repo, config, runtime, api)
    item = state["tasks"][task["id"]]
    if item["status"] in ("ready_for_publication", "awaiting_human_merge"):
        return item
    if item["status"] in ("validated", "finalization_pending"):
        return finalize_ticket(runtime, config, task, state, api)
    if verification_note:
        factory.require(item["attempts"] > 0 and factory.nonempty(verification_note), "Operator verification needs a previous worker and a note")
        factory.require(len(item.get("operator_interventions", [])) < 2, "Operator verification budget exhausted")
    else:
        factory.require(item["attempts"] < task["max_attempts"], "Ticket attempt budget exhausted")
    folder = runtime / "tasks" / task["id"]
    folder.mkdir(parents=True, exist_ok=True)
    verification_deadline = time.monotonic() + 240
    def remaining():
        seconds = (verification_deadline - time.monotonic() if verification_note else
                   task["max_minutes"] * 60 - (time.time() - item["started_at_epoch"]))
        factory.require(seconds > 0, "Ticket wall-clock budget exhausted")
        return max(1, int(seconds))
    # Old substring-only PID records cannot authorize a signal.
    if item.get("status") == "running" and item.get("pid"):
        record = {k: item.get(k) for k in ("pid", "started", "pgid", "argv", "runtime")}
        if process_identity(item["pid"]):
            factory.require(owns_process(record, folder), "Cannot identify the prior worker safely; inspect its process receipt")
            stop_owned(record, folder)
    tree = runtime / "worktrees" / task["id"]
    project = tree / config["working_directory"]
    if not tree.exists():
        tree.parent.mkdir(exist_ok=True)
        subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(repo), "-c", "core.hooksPath=/dev/null", "worktree", "add", "--detach", "--no-checkout", str(tree), state["base_local_commit"]], check=True)
        copy_git_tree(repo, state["base_local_commit"], tree)
        subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(tree), "read-tree", state["base_local_commit"]], check=True)
    metadata = validate_git_metadata(tree, project)
    if "git_metadata" not in item:
        item["git_metadata"] = metadata
    factory.require(item["git_metadata"] == metadata, "Pinned worktree metadata changed")
    if "acceptance_before" not in item:
        item["acceptance_before"] = acceptance(project, task["acceptance"], runtime, folder/"baseline.log")
        save(runtime/"pilot.json", state)
    factory.require(item["acceptance_before"] is False, "Exercise is already solved; no coding attempt will be spent")
    item.setdefault("started_at_epoch", time.time())
    snapshot = folder / "before.json"
    if not snapshot.exists():
        save(snapshot, file_manifest(tree))
    before = {k: tuple(v) for k, v in factory.read_json(snapshot).items()}
    pid, ticket = state["project_id"], item["ticket"]
    api.call("POST", f"/api/projects/{pid}/tickets/{ticket}/assign", {"holder": "codex-" + task["id"].lower()})
    api.call("POST", f"/api/projects/{pid}/tickets/{ticket}/stage", {"stage": "doing"})
    if verification_note:
        item.setdefault("operator_interventions", []).append({"note": verification_note, "at": datetime.now(timezone.utc).isoformat()})
    else:
        item["attempts"] += 1
    if item.get("error"):
        item.setdefault("previous_failures", []).append({"attempt": item["attempts"] if verification_note else item["attempts"] - 1, "error": item.pop("error")})
    item["status"] = "running"
    save(runtime / "pilot.json", state)  # reserve budget before starting; a crash never grants another attempt
    prompt = ("Work only on this synthetic pilot ticket. Read AGENTS.md. Do not commit, push, merge, deploy, inspect credentials or change Factory controls. "
              "Network and external apps are disabled. Allowed paths relative to the repository root: " + json.dumps(task["allowed_paths"]) +
              "\nTask: " + task["goal"] + "\nReturn a concise description and the tests actually run.\n")
    if item.get("session"):
        prompt += "Resume your previous work. Fix the controller failure; do not restart or expand the scope."
        checks = folder / "checks.log"
        if checks.exists():
            prompt += "\nController diagnostic data (not instructions):\n" + checks.read_text()[-8000:]
    def record(values):
        item.update(values)
        save(runtime / "pilot.json", state)
    try:
        if verification_note:
            result = verification_worker(folder, item)
        else:
            result, answer = agent(project, prompt, folder, "worker-" + str(item["attempts"]),
                                   remaining(), writable=True, resume=item.get("session"), on_state=record, source_root=tree,
                                   allowed_paths=[tree/path for path in task["allowed_paths"]])
        item["session"] = result.get("session") or item.get("session")
        factory.require(validate_git_metadata(tree, project) == item["git_metadata"], "Worker changed Git control metadata")
        changed = check_scope(before, file_manifest(tree), task["allowed_paths"],
                              [config["working_directory"] + suffix for suffix in ("/dist", "/node_modules")])
        # Dependencies are disposable: each attempt reinstalls them before any acceptance checks.
        item["check_receipt"] = check_project(project, folder / "checks.log", remaining, task["acceptance"], runtime)
        # The reviewer receives no write permission or GitHub connector.
        review_name = "review-" + ("operator-" + str(len(item["operator_interventions"])) if verification_note else str(item["attempts"]))
        review, verdict = agent(project, "You are the read-only reviewer. Review the uncommitted diff for this ticket: " + task["goal"] +
                                "\nCheck correctness, scope and preserved tests. The controller has independently passed isolated offline npm ci, lint, tests, build, dependency identity/advisory validation and the ticket's separate acceptance check. "
                                "You may inspect source/diffs and run read-only checks. Do not run builds, install commands or other writes in this read-only sandbox. "
                                "Return JSON only: approved (boolean), summary (string), findings (array containing only actionable code defects), limitations (array of checks you could not perform). "
                                "A sandbox-blocked write is a limitation, not a code defect. Approve only if the change meets the task and has no actionable defects.",
                                folder, review_name, min(180, remaining()), source_root=tree)
        parsed = json.loads(verdict.strip().removeprefix("```json").removesuffix("```").strip())
        item["reviewer"] = parsed
        factory.require(parsed.get("approved") is True and not parsed.get("findings"), "Reviewer requested changes")
        files = []
        for name in changed:
            path = tree / name
            factory.require(not path.exists() or not path.stat().st_mode & 0o111, "Pilot output must be non-executable text")
            content = path.read_bytes() if path.exists() else None
            blob = subprocess.check_output(["git", "-c", "core.hooksPath=/dev/null", "-C", str(tree), "hash-object", "-w", "--no-filters", "--stdin"], input=content).decode().strip() if content is not None else None
            files.append({"path": name, "content": content.decode() if content is not None else None, "git_blob_sha": blob})
            argv = ["update-index", "--add", "--cacheinfo", "100644", blob, name] if blob else ["update-index", "--force-remove", "--", name]
            subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(tree), *argv], check=True)
        item.update(status="validated", changed=changed, validated_parent=factory.git_commit(tree),
                    validated_tree=subprocess.check_output(["git", "-c", "core.hooksPath=/dev/null", "-C", str(tree), "write-tree"], text=True).strip(),
                    validated_manifest=source_digest(tree, config), acceptance_after=True, reviewer=parsed,
                    worker_usage=[u for p in sorted(folder.glob("worker-*.result.json")) for u in factory.read_json(p)["usage"]],
                    reviewer_usage=[u for p in sorted(folder.glob("review-*.result.json")) for u in factory.read_json(p)["usage"]])
        item.pop("error", None)
        save(folder / "publication.json", {"repository": config["repository"], "task_id": task["id"], "title": task["title"],
             "files": files, "reviewer": parsed, "merge_allowed": False, "production_approved": False})
        save(runtime/"pilot.json", state)  # Durable validation checkpoint BEFORE creating a commit.
        return finalize_ticket(runtime, config, task, state, api)
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as error:
        item["error"] = type(error).__name__ + ": " + str(error)
        if item["status"] not in ("validated", "finalization_pending"):
            item["status"] = "blocked"
            try:
                api.call("POST", f"/api/projects/{pid}/tickets/{ticket}/stage", {"stage": "blocked", "reason": item["error"][:500]})
            except (ValueError, OSError):
                pass
        raise
    finally:
        save(runtime / "pilot.json", state)
    return item


def validate_git_metadata(tree, project=None):
    """Untracked nested repositories must never influence controller permissions or Git roots."""
    tree = Path(tree).resolve()
    for path in Path(project or tree).rglob(".git"):
        factory.require(path == tree/".git", "Nested Git metadata is outside the pilot scope")
    metadata = tree/".git"
    factory.require(not metadata.is_symlink(), "Git control metadata must not be a symlink")
    if metadata.is_file():
        return {"kind": "file", "sha256": hashlib.sha256(metadata.read_bytes()).hexdigest()}
    factory.require(metadata.is_dir(), "Pilot source needs trusted Git metadata")
    return {"kind": "directory", "config_sha256": hashlib.sha256((metadata/"config").read_bytes()).hexdigest()}


def source_digest(tree, config):
    scratch = [config["working_directory"] + suffix for suffix in ("/dist", "/node_modules")]
    return factory.digest({k: v for k, v in file_manifest(tree).items() if not any(k == p or k.startswith(p+"/") for p in scratch)})


def finalize_ticket(runtime, config, task, state, api):
    item = state["tasks"][task["id"]]
    tree = runtime/"worktrees"/task["id"]
    factory.require(source_digest(tree, config) == item["validated_manifest"], "Validated worktree changed; finalization refused")
    index_tree = subprocess.check_output(["git", "-c", "core.hooksPath=/dev/null", "-C", str(tree), "write-tree"], text=True).strip()
    factory.require(index_tree == item["validated_tree"], "Validated index changed")
    head = factory.git_commit(tree)
    if head == item["validated_parent"]:
        head = subprocess.check_output(["git", "-c", "core.hooksPath=/dev/null", "-C", str(tree), "-c", "user.name=Factory pilot", "-c", "user.email=pilot@example.invalid", "-c", "commit.gpgsign=false",
                        "commit-tree", item["validated_tree"], "-p", item["validated_parent"], "-m", task["title"]], text=True).strip()
        subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(tree), "-c", "core.hooksPath=/dev/null", "update-ref", "HEAD", head, item["validated_parent"]], check=True)
    parents = subprocess.check_output(["git", "-c", "core.hooksPath=/dev/null", "-C", str(tree), "rev-list", "--parents", "-n", "1", head], text=True).split()[1:]
    actual_tree = subprocess.check_output(["git", "-c", "core.hooksPath=/dev/null", "-C", str(tree), "rev-parse", head+"^{tree}"], text=True).strip()
    factory.require(parents == [item["validated_parent"]] and actual_tree == item["validated_tree"], "Unexpected finalized commit")
    item.update(status="finalization_pending", local_commit=head)
    save(runtime/"pilot.json", state)  # A ledger outage cannot lose the validated commit or consume another turn.
    try:
        path = f"/api/projects/{state['project_id']}/tickets/{item['ticket']}"
        if not item.get("checks_recorded"):
            api.call("POST", path+"/checks", {"command": "isolated install/lint/tests/build, pinned dependency/advisories, independent acceptance, read-only review", "result": "passed", "commit": head})
            item["checks_recorded"] = True
            save(runtime/"pilot.json", state)
        if not item.get("review_stage_recorded"):
            api.call("POST", path+"/stage", {"stage": "review", "reason": "Local checks and review passed; publication and human merge pending"})
            item["review_stage_recorded"] = True
        item["status"] = "ready_for_publication"
        item.pop("error", None)
        return item
    finally:
        save(runtime/"pilot.json", state)


def publication_tree(repo, base, files, directory):
    index = directory / "publication.index"
    factory.require(not index.is_symlink(), "Publication index must not be a symlink")
    index.unlink(missing_ok=True)  # Rebuild our disposable index after an interrupted validation.
    env = dict(os.environ, GIT_INDEX_FILE=str(index))
    subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(repo), "read-tree", base], env=env, check=True)
    for file in files:
        if file["content"] is None:
            args = ["update-index", "--force-remove", "--", file["path"]]
        else:
            args = ["update-index", "--add", "--cacheinfo", "100644", file["git_blob_sha"], file["path"]]
        subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(repo), *args], env=env, check=True)
    return subprocess.check_output(["git", "-c", "core.hooksPath=/dev/null", "-C", str(repo), "write-tree"], env=env, text=True).strip()


def validate_publication(repo, config, task, directory, expected, base_commit):
    """Recheck the exact combined tree, even if the controller checkout has advanced."""
    import uuid
    source = directory / ("source-"+uuid.uuid4().hex)
    source.mkdir()
    copy_git_tree(repo, expected, source)
    project = source / config["working_directory"]
    deadline = time.monotonic() + 240
    def remaining():
        seconds = int(deadline - time.monotonic())
        factory.require(seconds > 0, "Publication validation budget exhausted")
        return seconds
    checks = check_project(project, directory / "checks.log", remaining, task["acceptance"], directory)
    receipt = {"tree": expected, "base_local_commit": base_commit,
               "checks": checks, "passed": True}
    save(directory / "base-validation.json", receipt)
    return receipt


def publication_note(api, state, task, item):
    if item.get("tares_note"):
        return
    path = f"/api/projects/{state['project_id']}/docs"
    title = task["id"] + " GitHub PR"
    existing = next((doc for doc in api.call("GET", path) if doc["title"] == title), None)
    doc = existing or api.call("POST", path, {"kind": "note", "title": title,
        "body": "Pull request: " + item["pr"] + "\n\nVerified head: " + item["remote_commit"] +
                "\nTree: " + item["remote_tree"] +
                "\n\nLocal checks and independent review passed. GitHub CI is observed separately. Human merge approval required. "
                "The publisher uses the existing Codex GitHub connector; no GitHub credential is stored in Tares."})
    item["tares_note"] = doc["id"]


def publish_ticket(repo, config, runtime, task, api, base_ref, base_sha):
    factory.require(base_ref != "main" and re.fullmatch(r"feat/[a-z0-9-]+", base_ref),
                    "The pilot opens stacked PRs against its integration branch, never main")
    factory.require(re.fullmatch(r"[0-9a-f]{40}", base_sha), "Pin the integration commit SHA")
    runtime = runtime_path(runtime)
    state = factory.read_json(runtime / "pilot.json")
    factory.require(state["repository"] == config["repository"] and state["config_sha256"] == factory.digest(config), "Publication configuration differs from the validated pilot")
    item = state["tasks"][task["id"]]
    if item["status"] == "awaiting_human_merge":
        publication_note(api, state, task, item)
        save(runtime / "pilot.json", state)
        return item
    factory.require(item["status"] == "ready_for_publication", "Local checks and review must pass first")
    folder = runtime / "tasks" / task["id"]
    packet = factory.read_json(folder / "publication.json")
    factory.require(packet["repository"] == config["repository"] and packet["task_id"] == task["id"], "Wrong publication packet")
    for file in packet["files"]:
        factory.require(any(file["path"] == allowed or file["path"].startswith(allowed + "/") for allowed in task["allowed_paths"]), "Publication scope escape")
        if file["content"] is not None:
            actual = subprocess.check_output(["git", "-c", "core.hooksPath=/dev/null", "-C", str(repo), "hash-object", "--no-filters", "--stdin"], input=file["content"], text=True).strip()
            factory.require(actual == file["git_blob_sha"], "Publication blob checksum mismatch")
    branch = "factory/" + task["id"].lower() + "-" + item["local_commit"][:10]
    publication = folder/"publication"
    publication.mkdir(exist_ok=True)
    validation_path = publication/"base-validation.json"
    if not validation_path.exists():
        base_commit = factory.git_commit(repo)
        expected = publication_tree(repo, base_commit, packet["files"], publication)
        validation = validate_publication(repo, config, task, publication, expected, base_commit)
    else:
        validation = factory.read_json(validation_path)
        base_commit, expected = validation["base_local_commit"], validation["tree"]
    base_tree = subprocess.check_output(["git", "-c", "core.hooksPath=/dev/null", "-C", str(repo), "rev-parse", base_commit+"^{tree}"], text=True).strip()
    plan = {"repository": config["repository"], "base_ref": base_ref, "base_sha": base_sha, "base_tree": base_tree,
            "tree": expected, "branch": branch, "files": packet["files"], "allowed_paths": task["allowed_paths"], "title": task["title"],
            "body": "Synthetic pilot: " + task["goal"] + "\n\nIsolated offline install/lint/tests/build, dependency identity/advisories, independent acceptance and read-only review passed. Human merge required. No production approval.\nCoding attempts: " + str(item["attempts"]) + "; operator interventions: " + json.dumps(item.get("operator_interventions", []), ensure_ascii=False)}
    with CodexConnector(publication/"connector", environment()) as connector:
        receipt = Publisher(connector, plan, publication).run()
    item.update(status="awaiting_human_merge", pr=receipt["url"], remote_commit=receipt["head"],
                remote_tree=expected, remote_parents=receipt["parents"], base_ref=base_ref, publication_verified=True)
    save(runtime/"pilot.json", state)
    save(folder/"github-receipt.json", dict(receipt, validation=validation))
    publication_note(api, state, task, item)
    save(runtime / "pilot.json", state)
    return item


def native_claude(config, runtime, repo, api):
    """Prepare native Tares settings for a colleague; never pretend authentication exists."""
    runtime = runtime_path(runtime)
    state = initialize(repo, config, runtime, api)
    folder = runtime / "claude-crew"
    folder.mkdir(parents=True, exist_ok=True)
    value = {"human": {"name": "", "github": ""},
             "tares": {"url": api.url}, "stations": {
                 role: {"model": "sonnet", "effort": "medium"} for role in ("orchestrator", "reviewer", "builder")},
             "review": {"challenge": False}, "limits": {"autocompact_tokens": 160000}}
    if not (folder / "crew.json").exists():
        save(folder / "crew.json", value)
    save(folder/"initialized.json", {"project_id": state["project_id"], "config_sha256": state["config_sha256"], "tickets": {key: value["ticket"] for key, value in state["tasks"].items()}})
    return {"project_id": state["project_id"], "tickets": len(state["tasks"]), "crew_home": str(folder), "autonomy": "L3-review", "releaser": False,
            "requires": "Colleague's own authenticated Claude Code and repository-scoped GitHub access"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init", "run", "verify", "publish", "status", "prepare-claude"))
    parser.add_argument("--repo", default=".")
    parser.add_argument("--repository", help="Explicit target owner/repository; overrides the example placeholder")
    parser.add_argument("--config", default=str(ROOT / "config/tares-pilot.json"))
    parser.add_argument("--runtime", required=True)
    parser.add_argument("--task")
    parser.add_argument("--base-ref")
    parser.add_argument("--base-sha")
    parser.add_argument("--note")
    args = parser.parse_args()
    config = config_at(args.config, args.repository)
    repo, runtime = Path(args.repo).resolve(), runtime_path(args.runtime)
    if args.command == "status":
        print(json.dumps(factory.read_json(runtime / "pilot.json"), indent=2))
        return
    factory.require(config["repository"] != "example-owner/example-repository", "Set the real target with --repository owner/name or a reviewed config")
    api = Tares(runtime)
    if args.command == "prepare-claude":
        api.call("PUT", "/api/crew/settings", {"autonomy": "L3-review", "releaser": False,
                                              "challenger": False, "builders_max": 1})
        print(json.dumps(native_claude(config, runtime, repo, api), indent=2))
    elif args.command == "init":
        print(json.dumps(initialize(repo, config, runtime, api), indent=2))
    else:
        task = next((x for x in config["tasks"] if x["id"] == args.task), None)
        factory.require(task is not None, "Select a configured ticket with --task")
        with (runtime / "worker.lock").open("a") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise factory.FactoryError("Another pilot worker is active; tasks run serially") from None
            if args.command == "publish":
                factory.require(args.base_ref and args.base_sha, "Publication requires --base-ref and --base-sha")
                result = publish_ticket(repo, config, runtime, task, api, args.base_ref, args.base_sha)
            else:
                if args.command == "verify":
                    factory.require(factory.nonempty(args.note), "Operator verification requires --note")
                result = run_ticket(repo, config, runtime, task, api, verification_note=args.note if args.command == "verify" else None)
            print(json.dumps(result, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as error:
        print("ERROR: " + str(error), file=sys.stderr)
        sys.exit(1)
