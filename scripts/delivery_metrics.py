"""Calculate delivery metrics from explicit deployment events; never count staging as production."""
import argparse
from datetime import datetime, timedelta, timezone
import json
import re
from pathlib import Path
from statistics import median


def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def calculate(events, application, days=30, now=None):
    if not isinstance(days, int) or days < 1:
        raise ValueError("Metric window must be positive")
    if not isinstance(application, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", application):
        raise ValueError("Select a valid application identifier")
    now = now or datetime.now(timezone.utc)
    production = [event for event in events if event.get("environment") == "production" and
                  event.get("synthetic_api") is not True and event.get("application") == application and
                  now - timedelta(days=days) <= timestamp(event["time"]) <= now]
    deployments = [event for event in production if event.get("kind") == "deploy"]
    if any(event.get("status") not in ("passed", "failed") for event in deployments):
        raise ValueError("Unknown production deployment outcome")
    failed = [event for event in deployments if event.get("status") == "failed"]
    succeeded = [event for event in deployments if event.get("status") == "passed"]
    lead_times = [(timestamp(event["time"]) - timestamp(event["committed_at"])).total_seconds()
                  for event in succeeded if event.get("committed_at")]
    if any(value < 0 for value in lead_times):
        raise ValueError("Deployment precedes its source commit")
    recovery = [(timestamp(event["recovered_at"]) - timestamp(event["time"])).total_seconds()
                for event in failed if event.get("recovered_at")]
    if any(value < 0 for value in recovery):
        raise ValueError("Recovery precedes the failed deployment")
    rework = [event for event in deployments if event.get("unplanned_rework") is True]
    return {"schema_version": 1, "application": application, "window_days": days,
            "window_end": now.isoformat(), "production_deployments": len(deployments),
            "deployment_frequency_per_day": len(succeeded) / days if deployments else None,
            "change_lead_time_seconds_median": median(lead_times) if lead_times and len(lead_times) == len(succeeded) else None,
            "change_fail_rate": len(failed) / len(deployments) if deployments else None,
            "failed_deployment_recovery_seconds_median": median(recovery) if recovery and len(recovery) == len(failed) else None,
            "deployment_rework_rate": len(rework) / len(deployments) if deployments and
                all(type(event.get("unplanned_rework")) is bool for event in deployments) else None,
            "metric_evidence": {
                "lead_time": {"recorded": len(lead_times), "expected": len(succeeded)},
                "recovery_time": {"recorded": len(recovery), "expected": len(failed)},
                "rework": {"recorded": sum(type(event.get("unplanned_rework")) is bool for event in deployments),
                           "expected": len(deployments)}},
            "excluded_events_outside_scope": len(events) - len(production),
            "note": "Null means no evidence, never a measured zero. Staging and synthetic events are excluded."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=Path, nargs="*", default=[])
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--application", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    events = []
    for path in args.events:
        value = json.loads(path.read_text())
        events.extend(value if isinstance(value, list) else value.get("events", []))
    result = calculate(events, args.application, args.days)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
