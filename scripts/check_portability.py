"""Ensure shipped configuration is independent of a specific repository or organization."""
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import repository_factory

root = Path(__file__).resolve().parents[1]
profile = json.loads((root/'profiles/factory.json').read_text())
repository_factory.validate_repository(profile, reviewed=True)
if profile['project'] != '$GITHUB_REPOSITORY':
    raise ValueError('The factory self-profile must bind to its current GitHub repository')
for path in (root/'.github/workflows').glob('*.yml'):
    text=path.read_text()
    if 'example-owner/' in text or 'github.repository ==' in text:
        raise ValueError('A workflow pins a personal or example repository: '+path.name)
print('Portable repository profile and workflow identity checks passed')
