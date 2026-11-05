"""Bind the independently passed security gate and SBOM to the exact checked CI payload."""
import argparse
import json
import os
from pathlib import Path

from publish_delivery import payload_manifest, run_identity, sha256, validate_security


def bind(summary, sbom, payload, environment):
    report = dict(summary, binding=run_identity(environment), payload=payload_manifest(payload), sbom_sha256=sha256(sbom))
    validate_security(report, dict(report["binding"], files=report["payload"]))
    if json.loads(Path(sbom).read_text()).get("bomFormat") != "CycloneDX":
        raise ValueError("A real CycloneDX SBOM is required")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--sbom", type=Path, required=True)
    parser.add_argument("--payload", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    report = bind(json.loads(args.summary.read_text()), args.sbom, args.payload, os.environ)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("x") as stream:
        json.dump(report, stream, indent=2); stream.write("\n")


if __name__ == "__main__":
    main()
