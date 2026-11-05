# Contributing

Use a branch and pull request. Keep commands as explicit argument lists, preserve the
security boundaries in SECURITY.md, and add a regression for a changed boundary.

Before requesting merge:

```sh
python3 scripts/check_syntax.py
python3 -m unittest discover -s tests -v
python3 scripts/check_workflows.py
```

GitHub additionally requires real Docker isolation, the repository integration matrix,
the recovery drill and independent security checks. A maintainer merges only after the
required checks pass. A second maintainer's approval is optional. Never bypass a red gate.

Increment VERSION for a release and describe compatibility changes in CHANGELOG.md.
Exported integrations are updated with the new distribution's `factory.py upgrade`;
review its plan and local conflicts before applying it on the consumer's own branch.
Do not publish keys, personal logs or production data. Report vulnerabilities privately
as described in SECURITY.md. Contributions are made under the repository's MIT license.
