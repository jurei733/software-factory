"""Store a checked CI deliverable as a private, unpublished release asset.

Release files use a separate GitHub service from Actions artifact storage.
Every invocation creates an immutable-by-convention run/attempt/component bundle;
existing assets are never overwritten or deleted. Python standard library only.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from pathlib import PurePosixPath
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

REPOSITORY_PATTERN = r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}"


def repository_name(value):
    if not isinstance(value, str) or not re.fullmatch(REPOSITORY_PATTERN, value) or value.split("/")[1] in (".", ".."):
        raise ValueError("Select an explicit GitHub owner/repository")
    return value



def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def context(environment):
    default_branch = environment.get("FACTORY_DEFAULT_BRANCH", "main")
    if not re.fullmatch(r"[A-Za-z0-9_.\/-]+", default_branch) or ".." in default_branch.split("/") or environment.get("GITHUB_REF") != "refs/heads/"+default_branch:
        raise ValueError("Release delivery is restricted to the configured default branch")
    return run_identity(environment)


def run_identity(environment):
    repository = repository_name(environment.get("GITHUB_REPOSITORY"))
    sha = environment.get("GITHUB_SHA", "")
    run, attempt = environment.get("GITHUB_RUN_ID", ""), environment.get("GITHUB_RUN_ATTEMPT", "")
    if not re.fullmatch(r"[0-9a-f]{40}", sha) or not run.isdecimal() or not attempt.isdecimal():
        raise ValueError("Missing or invalid GitHub run identity")
    return {"factory_repository": repository, "factory_commit": sha, "run_id": int(run),
            "run_attempt": int(attempt), "run_url": "https://github.com/%s/actions/runs/%s" % (repository, run)}


def payload_manifest(source):
    source = Path(source)
    if source.is_symlink() or not source.is_dir():
        raise ValueError("Delivery source must be an existing directory")
    files = []
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise ValueError("Delivery contains a symbolic link: " + str(path))
        if path.is_file() and not any(part.startswith(".") for part in path.relative_to(source).parts):
            files.append({"path": path.relative_to(source).as_posix(), "bytes": path.stat().st_size, "sha256": sha256(path)})
    if not files or len(files) > 10000 or sum(item["bytes"] for item in files) > 200 * 1024 ** 2:
        raise ValueError("Delivery has no files or exceeds its bounded payload limit")
    return files


def validate_security(summary, metadata):
    binding = {key: metadata[key] for key in ("factory_repository", "factory_commit", "run_id", "run_attempt", "run_url")}
    if (summary.get("schema_version") != 1 or summary.get("status") != "passed" or
            summary.get("production_eligible") is not True or summary.get("binding") != binding or
            summary.get("payload") != metadata["files"] or
            any(summary.get(key) != [] for key in ("introduced_high_or_critical", "existing_high_or_critical", "secrets"))):
        raise ValueError("Security evidence is not clean or not bound to this exact source, run and payload")


def bundle(source, target, metadata, evidence=None):
    source = Path(source)
    if source.is_symlink() or not source.is_dir():
        raise ValueError("Delivery source must be an existing directory")
    files = []
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise ValueError("Delivery contains a symbolic link: " + str(path))
        if path.is_file():
            relative = path.relative_to(source)
            if any(part.startswith(".") for part in relative.parts):
                # Match upload-artifact's default: build metadata such as
                # .browserslistrc/.gitkeep is omitted, rather than blocking delivery.
                continue
            files.append((path, relative.as_posix()))
    if not files:
        raise ValueError("Delivery has no files")
    manifest = dict(metadata, schema_version=1, production_eligible=False, files=payload_manifest(source))
    evidence_files = []
    if evidence:
        evidence = Path(evidence)
        for name in ("security-summary.json", "sbom.cdx.json"):
            path = evidence / name
            if path.is_symlink() or not path.is_file():
                raise ValueError("Missing or unsafe security evidence")
            evidence_files.append((path, name))
        summary = json.loads((evidence / "security-summary.json").read_text())
        validate_security(summary, manifest)
        sbom = json.loads((evidence / "sbom.cdx.json").read_text())
        if sbom.get("bomFormat") != "CycloneDX" or summary.get("sbom_sha256") != sha256(evidence / "sbom.cdx.json"):
            raise ValueError("Security evidence SBOM identity mismatch")
        manifest["production_eligible"] = True
        manifest["evidence"] = [{"path": name, "bytes": path.stat().st_size, "sha256": sha256(path)}
                                for path, name in evidence_files]
    with ZipFile(target, "x", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for path, name in files:
            entry = ZipInfo("payload/" + name, date_time=(2020, 1, 1, 0, 0, 0))
            entry.compress_type = ZIP_DEFLATED
            entry.external_attr = (0o100755 if path.stat().st_mode & 0o111 else 0o100644) << 16
            with path.open("rb") as stream, archive.open(entry, "w") as destination:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    destination.write(chunk)
        for path, name in evidence_files:
            entry = ZipInfo("evidence/" + name, date_time=(2020, 1, 1, 0, 0, 0))
            entry.compress_type = ZIP_DEFLATED
            archive.writestr(entry, path.read_bytes())
        entry = ZipInfo("delivery.json", date_time=(2020, 1, 1, 0, 0, 0))
        entry.compress_type = ZIP_DEFLATED
        archive.writestr(entry, json.dumps(manifest, sort_keys=True, ensure_ascii=False, indent=2) + "\n")
    if Path(target).stat().st_size >= 2 * 1024 ** 3:
        raise ValueError("Bundle exceeds GitHub's release asset size limit")
    return manifest


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GitHub:
    def __init__(self, token, repository):
        self.repository = repository_name(repository)
        self.token = token
        self.opener = build_opener(NoRedirect())

    def request(self, method, path, value=None, upload=None):
        if not path.startswith("/") or path.startswith("//") or ".." in path.split("/"):
            raise ValueError("Invalid API path")
        host = "uploads.github.com" if upload else "api.github.com"
        headers = {"Authorization": "Bearer " + self.token,
                   "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2026-03-10",
                   "User-Agent": "software-factory-delivery"}
        data = None
        stream = None
        if upload:
            stream = Path(upload).open("rb")
            data = stream
            headers.update({"Content-Type": "application/json" if Path(upload).suffix == ".json" else "application/zip", "Content-Length": str(Path(upload).stat().st_size)})
        elif value is not None:
            data = json.dumps(value).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            request = Request("https://" + host + "/repos/" + self.repository + path,
                              data=data, headers=headers, method=method)
            with self.opener.open(request, timeout=300) as response:
                body = response.read()
                return json.loads(body) if body else None
        except HTTPError as error:
            raise RuntimeError("Release API HTTP %s (%s %s)" % (error.code, method, path)) from None
        finally:
            if stream:
                stream.close()


def publish(api, archive, metadata, attestation=None):
    tag = "ci-%s-%s-%s" % (metadata["run_id"], metadata["run_attempt"], metadata["component"])
    release = api.request("POST", "/releases", {
        "tag_name": tag, "target_commitish": metadata["factory_commit"], "name": tag,
        "draft": True, "prerelease": True, "make_latest": "false",
        "body": "CI delivery; unpublished, not a production release.\n\n" + metadata["run_url"] +
                "\n\nSource: " + metadata["source_repository"] + "@" + metadata["source_commit"] +
                "\n\nThe ZIP contains payload/ and delivery.json with source provenance and file hashes.",
    })
    if not release.get("draft") or release["tag_name"] != tag:
        raise RuntimeError("Release identity or draft state does not match")
    path = "/releases/%s/assets?%s" % (release["id"], urlencode({"name": metadata["component"] + ".zip"}))
    asset = api.request("POST", path, upload=archive)
    digest = "sha256:" + sha256(archive)
    if asset.get("state") != "uploaded" or asset.get("size") != Path(archive).stat().st_size or asset.get("digest") != digest:
        raise RuntimeError("Uploaded release asset failed size or SHA-256 verification")
    if attestation:
        attestation = Path(attestation)
        if attestation.is_symlink() or not attestation.is_file() or attestation.stat().st_size > 16 * 1024 ** 2:
            raise ValueError("Unsafe attestation bundle")
        signature = api.request("POST", "/releases/%s/assets?name=provenance.sigstore.json" % release["id"], upload=attestation)
        if signature.get("digest") != "sha256:" + sha256(attestation) or signature.get("size") != attestation.stat().st_size:
            raise RuntimeError("Uploaded attestation bundle digest mismatch")
    return {"release_url": release["html_url"], "asset_id": asset["id"],
            "asset_name": asset["name"], "bytes": asset["size"], "sha256": digest[7:]}


def read_manifest(package):
    """Validate archive bounds and paths before decompressing the manifest."""
    entries = package.infolist()
    names = [entry.filename for entry in entries]
    if len(names) != len(set(names)) or sum(entry.file_size for entry in entries) > 2 * 1024 ** 3:
        raise ValueError("Duplicate ZIP entries or oversized expanded bundle")
    for entry in entries:
        name = PurePosixPath(entry.filename)
        mode = entry.external_attr >> 16
        if (name.is_absolute() or ".." in name.parts or "\\" in entry.filename or
                any(":" in part for part in name.parts) or str(name) != entry.filename or
                (mode & 0o170000) == 0o120000):
            raise ValueError("Unsafe bundle member")
    if package.getinfo("delivery.json").file_size > 4 * 1024 ** 2:
        raise ValueError("Oversized delivery manifest")
    with package.open("delivery.json") as stream:
        content = stream.read(4 * 1024 ** 2 + 1)
    if len(content) > 4 * 1024 ** 2:
        raise ValueError("Oversized delivery manifest")
    return json.loads(content)


def verify_bundle(archive, environment):
    """Verify data and platform identity without extracting or executing a build file."""
    expected = context(environment)
    with ZipFile(archive) as package:
        metadata = read_manifest(package)
        names = package.namelist()
        if any(metadata.get(key) != value for key, value in expected.items()):
            raise ValueError("Bundle does not belong to this exact factory run")
        if not re.fullmatch(r"[a-z0-9-]{1,80}", metadata.get("component", "")):
            raise ValueError("Invalid bundle component")
        if metadata.get("source_repository") != expected["factory_repository"]:
            raise ValueError("Bundle source must be the authorized executing repository")
        if not re.fullmatch(r"[0-9a-f]{40}", metadata.get("source_commit", "")):
            raise ValueError("Invalid source identity")
        files = metadata.get("files")
        if not isinstance(files, list) or not files:
            raise ValueError("Bundle has no file manifest")
        listed = ["payload/" + item["path"] for item in files]
        evidence = metadata.get("evidence", [])
        listed += ["evidence/" + item["path"] for item in evidence]
        if len(listed) != len(set(listed)) or set(names) != {"delivery.json", *listed}:
            raise ValueError("Bundle members differ from the manifest")
        for prefix, item in [("payload/", item) for item in files] + [("evidence/", item) for item in evidence]:
            digest = hashlib.sha256()
            size = 0
            with package.open(prefix + item["path"]) as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    size += len(chunk)
                    digest.update(chunk)
            if size != item["bytes"] or digest.hexdigest() != item["sha256"]:
                raise ValueError("Bundle payload failed SHA-256 verification")
        if metadata.get("production_eligible") is True:
            if {item["path"] for item in evidence} != {"security-summary.json", "sbom.cdx.json"}:
                raise ValueError("Missing production security evidence")
            summary = json.loads(package.read("evidence/security-summary.json"))
            validate_security(summary, metadata)
            sbom_bytes = package.read("evidence/sbom.cdx.json")
            if (json.loads(sbom_bytes).get("bomFormat") != "CycloneDX" or
                    hashlib.sha256(sbom_bytes).hexdigest() != summary.get("sbom_sha256")):
                raise ValueError("Security evidence SBOM identity mismatch")
    return metadata


def write_receipt(receipt):
    report = "Delivery stored and SHA-256 verified: [%s](%s)\n\nSHA-256: `%s`\n" % (
        receipt["asset_name"], receipt["release_url"], receipt["sha256"])
    print(report)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as summary:
            summary.write(report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source")
    parser.add_argument("--component")
    parser.add_argument("--source-repository", type=repository_name)
    parser.add_argument("--source-sha")
    parser.add_argument("--security-evidence", type=Path)
    parser.add_argument("--attestation-bundle", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--bundle-only", type=Path)
    mode.add_argument("--publish-bundle", type=Path)
    args = parser.parse_args()
    if args.publish_bundle:
        metadata = verify_bundle(args.publish_bundle, os.environ)
        receipt = publish(GitHub(os.environ["GH_TOKEN"], metadata["factory_repository"]), args.publish_bundle, metadata, args.attestation_bundle)
        write_receipt(receipt)
        return
    if not all((args.source, args.component, args.source_repository, args.source_sha)):
        parser.error("source, component, source-repository and source-sha are required for packaging")
    if not re.fullmatch(r"[a-z0-9-]{1,80}", args.component) or not re.fullmatch(r"[0-9a-f]{40}", args.source_sha):
        raise ValueError("Invalid component or source commit")
    metadata = dict(context(os.environ), component=args.component,
                    source_repository=args.source_repository, source_commit=args.source_sha)
    if args.source_repository != metadata["factory_repository"]:
        raise ValueError("Delivery can only package the authorized executing repository")
    if args.bundle_only:
        args.bundle_only.parent.mkdir(parents=True, exist_ok=True)
        bundle(args.source, args.bundle_only, metadata, args.security_evidence)
        verify_bundle(args.bundle_only, os.environ)
        print("Packaged data only; SHA-256: " + sha256(args.bundle_only))
        return
    with tempfile.TemporaryDirectory() as folder:
        archive = Path(folder) / (args.component + ".zip")
        bundle(args.source, archive, metadata, args.security_evidence)
        receipt = publish(GitHub(os.environ["GH_TOKEN"], metadata["factory_repository"]), archive, metadata)
    write_receipt(receipt)


if __name__ == "__main__":
    main()
