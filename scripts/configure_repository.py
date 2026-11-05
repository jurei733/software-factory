"""Inspect or apply the exact factory ruleset; never changes billing, visibility or upstreams."""
import argparse
import json
from pathlib import Path
import subprocess

from publish_delivery import repository_name
ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True, type=repository_name)
    parser.add_argument("--branch", default="main")
    parser.add_argument("--apply", action="store_true", help="Apply the reviewed ruleset to the factory only")
    args = parser.parse_args()
    repository = args.repository
    ruleset = json.loads((ROOT / "config/repository-ruleset.json").read_text())
    if not args.apply:
        print(json.dumps({"repository": repository, "desired": ruleset,
                          "note": "Requires repository protection to be available on this plan/visibility. Human review remains optional. No paid plan is enabled."}, indent=2))
        return
    result = subprocess.run(["gh", "api", "repos/" + repository + "/rulesets"],
                            check=True, capture_output=True, text=True)
    matches = [item for item in json.loads(result.stdout) if item["name"] == ruleset["name"]]
    if len(matches) > 1:
        raise ValueError("Multiple matching rulesets; refusing ambiguous update")
    endpoint = "repos/" + repository + "/rulesets" + ("/" + str(matches[0]["id"]) if matches else "")
    subprocess.run(["gh", "api", "--method", "PUT" if matches else "POST", endpoint, "--input", "-"],
                   input=json.dumps(ruleset), text=True, check=True)
    branch = subprocess.check_output(["gh", "api", "repos/" + repository + "/branches/"+args.branch], text=True)
    if not json.loads(branch)["protected"]:
        raise RuntimeError("GitHub did not confirm protected main")
    print("Default branch is protected. Visibility and billing were not changed.")


if __name__ == "__main__":
    main()
