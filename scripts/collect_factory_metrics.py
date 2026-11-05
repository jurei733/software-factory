"""Read a bounded sample of this factory's CI and PR metadata; never infer production delivery."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from statistics import median
from urllib.request import Request, build_opener
from publish_delivery import NoRedirect

from publish_delivery import repository_name


def fetch(path, repository):
    request = Request("https://api.github.com/repos/" + repository + path,
                      headers={"Authorization": "Bearer " + os.environ["GH_TOKEN"],
                               "Accept": "application/vnd.github+json", "User-Agent": "factory-factory-metrics"})
    with build_opener(NoRedirect()).open(request, timeout=30) as response:
        return json.load(response)


def ci_metrics(runs):
    completed = [run for run in runs if run["status"] == "completed"]
    executed = [run for run in completed if run["conclusion"] not in ("cancelled", "skipped", None)]
    return {"completed_ci_runs": len(executed),
            "skipped_ci_runs": sum(run["conclusion"] == "skipped" for run in completed),
            "cancelled_ci_runs": sum(run["conclusion"] == "cancelled" for run in completed),
            "ci_success_rate": sum(run["conclusion"] == "success" for run in executed) / len(executed) if executed else None}


def main():
    repository = repository_name(os.environ.get("GITHUB_REPOSITORY"))
    runs = fetch("/actions/runs?per_page=100", repository)["workflow_runs"]
    pulls = fetch("/pulls?state=all&per_page=100", repository)
    review_cycles = [(datetime.fromisoformat(pr["merged_at"].replace("Z", "+00:00")) -
                      datetime.fromisoformat(pr["created_at"].replace("Z", "+00:00"))).total_seconds()
                     for pr in pulls if pr.get("merged_at")]
    report = {"schema_version": 1, "repository": repository, "collected_at": datetime.now(timezone.utc).isoformat(),
              "sample_limit": 100, **ci_metrics(runs),
              "merged_prs": len(review_cycles), "open_prs": sum(pr["state"] == "open" for pr in pulls),
              "pr_open_to_merge_seconds_median": median(review_cycles) if review_cycles else None,
              "production_metrics": "Not inferred from CI or PR events. Import deployment receipts per application.",
              "assistant_cost": None, "human_review_minutes": None}
    target = Path("metrics/factory.json")
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
