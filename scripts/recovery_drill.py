"""Synthetic two-version install/rollback and stopped runtime recovery; no production data."""
import argparse
import json
from pathlib import Path
import tempfile
from zipfile import ZipFile, ZIP_DEFLATED

from stage_release import install, rollback, read_slot, health
from runtime_backup import backup, restore


def exercise(package, base):
    with ZipFile(package) as current:
        release = json.loads(current.read("software-factory/factory-release.json"))
        old = base / "previous.zip"
        with ZipFile(old, "w", ZIP_DEFLATED) as previous:
            for entry in current.infolist():
                data = current.read(entry)
                if entry.filename == "software-factory/VERSION":
                    data = b"0.0.0\n"
                if entry.filename == "software-factory/factory-release.json":
                    data = json.dumps(dict(release, version="0.0.0")).encode()
                previous.writestr(entry, data)
    deployment = base / "staging"
    install(old, deployment, release["source_commit"])
    install(package, deployment, release["source_commit"])
    selected = read_slot(deployment, "current")
    # Corrupt the candidate after installation to prove independent health and rollback.
    (deployment / "versions" / selected["installation"] / "factory.py").write_text("synthetic corruption")
    try:
        health(deployment / "versions" / selected["installation"], selected, deployment)
        raise AssertionError("Corrupt candidate was accepted")
    except ValueError:
        pass
    assert rollback(deployment)["version"] == "0.0.0"
    runtime = base / "runtime"; (runtime / "data").mkdir(parents=True)
    (runtime / "root-token").write_text("synthetic-only-not-a-credential")
    (runtime / "data/state.json").write_text('{"synthetic":true}')
    (runtime / "pilot.json").write_text('{"tasks":{"T":{"status":"published","attempts":2}}}')
    archive = base / "private-backup.zip"; receipt = backup(runtime, archive)
    recovered = base / "restored"; restore(archive, recovered, receipt["sha256"])
    assert json.loads((recovered / "pilot.json").read_text())["tasks"]["T"]["attempts"] == 2
    assert (recovered / "data/state.json").read_bytes() == (runtime / "data/state.json").read_bytes()
    assert archive.stat().st_mode & 0o777 == 0o600
    return {"status": "passed", "synthetic": True, "candidate_health": "passed-before-corruption",
            "corruption_rejected": True, "rollback": "previous-version-restored", "runtime_restore": "state-and-budgets-preserved",
            "production_approved": False, "backup_uploaded": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True); args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="factory-recovery-") as folder:
        result = exercise(args.package, Path(folder))
    args.out.parent.mkdir(parents=True, exist_ok=True); args.out.write_text(json.dumps(result, indent=2) + "\n")
