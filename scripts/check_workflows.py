"""Validate real central workflows and rendered generic overlays with actionlint."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("factory", ROOT / "factory.py")
factory = importlib.util.module_from_spec(spec)
spec.loader.exec_module(factory)
import sys
sys.path.insert(0, str(ROOT))
from repository_factory import draft_profile
with tempfile.TemporaryDirectory() as folder:
    paths = list((ROOT / ".github/workflows").glob("*.yml"))
    for stack in ("node", "python", "custom"):
        output = Path(folder) / ("generic-" + stack)
        config = draft_profile(folder, "example-owner/example-repository", stack)
        factory.stage(None, output, config=config)
        paths.extend((output / ".github/workflows").glob("*.yml"))
    subprocess.run(["actionlint", "-shellcheck=", "-pyflakes="] + [str(path) for path in paths], check=True)
    print("Zentrale und exportierte Workflows geprüft: " + str(len(paths)))
