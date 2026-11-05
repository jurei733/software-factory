"""Verify an artifact before promotion; production requires authenticated main provenance and clean security."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
from zipfile import ZipFile
from publish_delivery import repository_name, read_manifest, sha256, verify_bundle


def verify(archive, environment, *, repository, default_branch="main", signer_workflow="factory.yml",
           expected_source_sha=None, trust_domain="public", attestation_bundle=None, verifier=subprocess.run):
    repository = repository_name(repository)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+\.yml", signer_workflow):
        raise ValueError("Pin an approved signer workflow filename")
    if environment not in ("isolated-staging", "production"):
        raise ValueError("Unknown promotion environment")
    with ZipFile(archive) as package:
        metadata = read_manifest(package)
    platform = {"GITHUB_REPOSITORY": repository, "GITHUB_REF": "refs/heads/"+default_branch, "FACTORY_DEFAULT_BRANCH": default_branch,
                "GITHUB_SHA": metadata["factory_commit"], "GITHUB_RUN_ID": str(metadata["run_id"]),
                "GITHUB_RUN_ATTEMPT": str(metadata["run_attempt"])}
    metadata = verify_bundle(archive, platform)
    if trust_domain not in ("public", "github-private"):
        raise ValueError("Select an explicit attestation trust domain")
    if expected_source_sha is not None and (not re.fullmatch(r"[a-f0-9]{40}", expected_source_sha) or
            metadata["factory_commit"] != expected_source_sha or metadata["source_commit"] != expected_source_sha):
        raise ValueError("Bundle source differs from the independently pinned source commit")
    if environment == "production":
        if metadata.get("production_eligible") is not True or not metadata.get("evidence"):
            raise ValueError("Production is blocked by missing or unresolved security evidence")
        if expected_source_sha is None:
            raise ValueError("Production requires an independently pinned source commit")
        command = ["gh", "attestation", "verify", str(archive), "--repo", repository,
                  "--signer-workflow", repository + "/.github/workflows/" + signer_workflow,
                  "--source-ref", "refs/heads/"+default_branch, "--source-digest", metadata["factory_commit"],
                   "--deny-self-hosted-runners"]
        if trust_domain == "github-private":
            command.append("--no-public-good")
        if attestation_bundle:
            command += ["--bundle", str(attestation_bundle)]
        verifier(command, check=True, timeout=120)
    return {"schema_version": 1, "environment": environment, "status": "verified",
            "archive_sha256": sha256(archive), "source_commit": metadata["source_commit"],
            "factory_commit": metadata["factory_commit"], "deployment_performed": False,
            "verified_at": datetime.now(timezone.utc).isoformat()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True, type=repository_name)
    parser.add_argument("--default-branch", default="main")
    parser.add_argument("--signer-workflow", default="factory.yml")
    parser.add_argument("--source-sha", required=True, help="Source SHA selected independently of the downloaded manifest")
    parser.add_argument("--trust-domain", choices=("public", "github-private"), default="public")
    parser.add_argument("--attestation-bundle", type=Path)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--environment", choices=("isolated-staging", "production"), required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = verify(args.archive, args.environment, repository=args.repository, default_branch=args.default_branch,
                    signer_workflow=args.signer_workflow, expected_source_sha=args.source_sha,
                    trust_domain=args.trust_domain, attestation_bundle=args.attestation_bundle)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("x") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
    print("Promotion evidence verified; deployment remains a separate controlled action.")


if __name__ == "__main__":
    main()
