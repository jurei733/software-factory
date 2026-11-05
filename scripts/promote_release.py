"""Explicitly promote a verified, current-source draft; never rebuild or overwrite a version."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from zipfile import ZipFile

from publish_delivery import GitHub, context, read_manifest
from stage_release import install
from verify_promotion import verify


def promote(tag, version, environment, *, publish=False):
    identity = context(environment)
    if not re.fullmatch(r"ci-[0-9]+-[0-9]+-factory", tag) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError("Pin a candidate tag and an explicit release version")
    repo, sha = identity["factory_repository"], identity["factory_commit"]
    api = GitHub(environment["GH_TOKEN"], repo)
    branch = environment.get("FACTORY_DEFAULT_BRANCH", "main")
    current = api.request("GET", "/branches/" + branch)
    if current["commit"]["sha"] != sha or current.get("protected") is not True:
        raise ValueError("Release requires the current protected default branch")
    candidates = [item for item in api.request("GET", "/releases?per_page=100") if item["tag_name"] == tag]
    if len(candidates) != 1 or not candidates[0]["draft"] or candidates[0]["target_commitish"] != sha:
        raise ValueError("Candidate is missing, published, ambiguous or stale")
    candidate = candidates[0]
    try:
        api.request("GET", "/git/ref/tags/v" + version)
    except RuntimeError as error:
        if "HTTP 404" not in str(error):
            raise
    else:
        raise ValueError("Release version already exists; never overwrite it")
    if not {"factory.zip", "provenance.sigstore.json"} <= {item["name"] for item in candidate["assets"]}:
        raise ValueError("Candidate is missing signed assets")
    with tempfile.TemporaryDirectory(prefix="factory-release-") as folder:
        root = Path(folder)
        subprocess.run(["gh", "release", "download", tag, "--repo", repo, "--pattern", "factory.zip",
                        "--pattern", "provenance.sigstore.json", "--dir", str(root)], check=True, timeout=120)
        archive = root / "factory.zip"
        verified = verify(archive, "production", repository=repo, default_branch=branch, expected_source_sha=sha,
                          attestation_bundle=root / "provenance.sigstore.json")
        with ZipFile(archive) as package:
            manifest = read_manifest(package)
            if manifest["run_id"] != int(tag.split("-")[1]) or manifest["run_attempt"] != int(tag.split("-")[2]) or manifest["component"] != "factory":
                raise ValueError("Candidate tag does not match its signed run")
            payload = root / "software-factory.zip"; payload.write_bytes(package.read("payload/software-factory.zip"))
        staged = install(payload, root / "staging", sha)
        if staged["version"] != version:
            raise ValueError("Confirmation differs from the signed distribution version")
    # Fresh lease immediately before the only remote write.
    if api.request("GET", "/branches/" + branch)["commit"]["sha"] != sha:
        raise ValueError("Default branch moved during release verification")
    result = {"status": "verified", "source_commit": sha, "version": version, "candidate": tag,
              "archive_sha256": verified["archive_sha256"], "staging": staged, "published": False}
    if publish:
        released = api.request("PATCH", "/releases/" + str(candidate["id"]), {
            "tag_name": "v" + version, "target_commitish": sha, "name": "Software Factory " + version,
            "draft": False, "prerelease": False, "make_latest": "true"})
        if released.get("draft") or released.get("tag_name") != "v" + version or released.get("target_commitish") != sha:
            raise RuntimeError("Published release readback differs from verified identity")
        result.update(published=True, release_url=released["html_url"])
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True); parser.add_argument("--confirm-version", required=True)
    parser.add_argument("--publish", action="store_true"); parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = promote(args.candidate, args.confirm_version, os.environ, publish=args.publish)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("x") as stream:
        json.dump(result, stream, indent=2); stream.write("\n")
