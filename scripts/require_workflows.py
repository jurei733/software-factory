"""Wait for the other required workflows at the same default-branch source commit."""
import json
import os
import time

from publish_delivery import GitHub, context


def require(api, identity, *, branch="main", timeout=360, sleep=time.sleep):
    required = {".github/workflows/repository.yml", ".github/workflows/agent-pilot.yml"}
    deadline = time.monotonic() + timeout
    while True:
        data = api.request("GET", "/actions/runs?head_sha=" + identity["factory_commit"] + "&event=push&per_page=100")
        selected = {}
        for run in data["workflow_runs"]:
            path = run["path"].split("@")[0]
            if (path in required and path not in selected and run["head_sha"] == identity["factory_commit"] and
                    run["head_branch"] == branch):
                selected[path] = run
        if any(run["status"] == "completed" and run["conclusion"] != "success" for run in selected.values()):
            raise ValueError("A required source workflow did not pass")
        if set(selected) == required and all(run["status"] == "completed" and run["conclusion"] == "success" for run in selected.values()):
            return {path: run["id"] for path, run in selected.items()}
        if time.monotonic() >= deadline:
            raise TimeoutError("Required source workflows did not complete successfully")
        sleep(5)


if __name__ == "__main__":
    identity = context(os.environ)
    print(json.dumps(require(GitHub(os.environ["GH_TOKEN"], identity["factory_repository"]), identity,
                             branch=os.environ.get("FACTORY_DEFAULT_BRANCH", "main"))))
