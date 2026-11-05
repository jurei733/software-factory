"""Compile sources in memory, without generating __pycache__ files."""
from pathlib import Path

root = Path(__file__).resolve().parents[1]
paths = [p for p in root.rglob("*.py") if not {"dist", "__pycache__"}.intersection(p.parts)]
for path in paths:
    compile(path.read_text(encoding="utf-8"), str(path), "exec")
print("Syntax geprüft: %d Dateien" % len(paths))
