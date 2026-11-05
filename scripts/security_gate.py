"""Block secrets and newly introduced high/critical vulnerabilities; expose existing debt."""
import argparse
import copy
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import re


def normalized_target(value):
    target = value.replace("\\", "/")
    for prefix in ("scan-current/", "scan-baseline/"):
        position = target.find(prefix)
        if position == 0 or (position > 0 and target[position - 1] == "/"):
            return target[position + len(prefix):]
    return target


def triage_test_fixtures(report, source, application, policy, today=None):
    """An exact, expiring fingerprint exception never exempts a file or a new JWT on the same line."""
    today = today or date.today()
    source = source.resolve()
    approved = {}
    for exception in policy.get("exceptions", []):
        if exception.get("application") == application and date.fromisoformat(exception["expires"]) >= today:
            if exception["rule"] != "jwt-token" or not re.fullmatch(r"[0-9a-f]{64}", exception["token_sha256"]):
                raise ValueError("Only exact reviewed JWT-fixture exceptions are supported")
            approved.setdefault(exception["path"], []).append(exception)
    result = copy.deepcopy(report)
    accepted = []
    for item in result.get("Results") or []:
        target = normalized_target(item.get("Target", ""))
        remaining = []
        for secret in item.get("Secrets") or []:
            matches = approved.get(target, []) if secret.get("RuleID") == "jwt-token" else []
            path = source / target
            safe = source in path.resolve().parents and not path.is_symlink()
            if matches and safe and path.is_file() and path.stat().st_size < 4 * 1024 ** 2:
                line = path.read_text().splitlines()[secret["StartLine"] - 1]
                tokens = re.findall(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", line)
                fingerprints = {hashlib.sha256(token.encode()).hexdigest() for token in tokens}
                known = {exception["token_sha256"] for exception in matches}
                if fingerprints and fingerprints <= known:
                    accepted.append({"target": target, "rule": "jwt-token", "line": secret["StartLine"],
                                     "fingerprints": sorted(fingerprints),
                                     "reason": "Exact reviewed non-credential test fixture; exception has an expiry."})
                    continue
            remaining.append(secret)
        item["Secrets"] = remaining
    return result, accepted


def findings(report):
    if report.get("SchemaVersion") != 2 or not isinstance(report.get("ArtifactName"), str):
        raise ValueError("Missing or invalid scanner evidence")
    vulnerabilities = {}
    secrets = []
    for result in report.get("Results") or []:
        target = normalized_target(result.get("Target", ""))
        for vulnerability in result.get("Vulnerabilities") or []:
            if vulnerability.get("Severity") in ("HIGH", "CRITICAL"):
                key = (target, vulnerability["PkgName"], vulnerability.get("InstalledVersion", ""),
                       vulnerability["VulnerabilityID"])
                vulnerabilities[key] = {"target": target, "package": key[1], "id": key[3],
                                        "severity": vulnerability["Severity"],
                                        "version": vulnerability.get("InstalledVersion", ""),
                                        "fixed_version": vulnerability.get("FixedVersion", "")}
        for secret in result.get("Secrets") or []:
            # Never copy a matched credential into logs or a report.
            secrets.append({"target": target, "rule": secret.get("RuleID"),
                            "line": secret.get("StartLine")})
    return vulnerabilities, secrets


def evaluate(current, baseline):
    current_vulns, secrets = findings(current)
    old_vulns, _ = findings(baseline)
    introduced = [current_vulns[key] for key in sorted(current_vulns.keys() - old_vulns.keys())]
    return {"schema_version": 1, "status": "failed" if secrets or introduced else "passed",
            "introduced_high_or_critical": introduced, "secrets": secrets,
            "existing_high_or_critical": [current_vulns[key] for key in sorted(current_vulns.keys() & old_vulns.keys())],
            "production_eligible": not current_vulns and not secrets,
            "policy": "Block secrets and new HIGH/CRITICAL findings; existing findings forbid production promotion."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--current", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--application", help="Optional project identifier for explicitly configured exceptions")
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--exceptions", type=Path)
    args = parser.parse_args()
    current = json.loads(args.current.read_text())
    accepted = []
    if args.exceptions:
        if not args.source_root or not args.application:
            parser.error("Fixture exceptions require a fixed application and source root")
        current, accepted = triage_test_fixtures(current, args.source_root, args.application,
                                                json.loads(args.exceptions.read_text()))
    report = evaluate(current, json.loads(args.baseline.read_text()))
    report["triaged_test_fixtures"] = accepted
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    message = ("Security: %s; new HIGH/CRITICAL: %d; existing HIGH/CRITICAL: %d; secrets: %d; production eligible: %s" %
               (report["status"], len(report["introduced_high_or_critical"]), len(report["existing_high_or_critical"]),
                len(report["secrets"]), report["production_eligible"]))
    print(message)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as stream:
            stream.write("\n" + message + "\n")
    raise SystemExit(0 if report["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
