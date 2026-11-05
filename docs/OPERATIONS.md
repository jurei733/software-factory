# Operation, releases and recovery

The repository maintainer owns release decisions, security updates, failed-check triage
and recovery. Main requires PRs and successful named checks without bypass. A second
reviewer or Code Owner is optional. No workflow buys services, increases budgets or calls
a model API. Public standard runners are used; native assistant subscriptions remain local.

## Release

1. Change VERSION and CHANGELOG.md through a PR. Required CI covers independent security,
   real Docker isolation, six independent consumer integrations, the native sandbox
   contract and a synthetic recovery drill. Merge only after every required check passes.
2. Default-branch delivery reuses the distribution built by verification. Security evidence
   binds source SHA, run/attempt, payload hashes and SBOM. Delivery waits for the other
   source workflows, bundles once, attests public provenance and SBOM, verifies them and
   installs the exact signed payload in temporary staging. It creates an unpublished
   `ci-RUN-ATTEMPT-factory` draft with `factory.zip` and `provenance.sigstore.json`.
3. Review the candidate, staging result and changelog. Manually dispatch **Publish verified
   release** on the current default branch with the exact candidate and version. Keep
   `publish=false` for rehearsal; choose `true` only for intentional publication. The job
   rejects stale source, unprotected branches, missing evidence/signatures, wrong versions
   and existing version tags. It promotes the same assets to `vVERSION`, without rebuilding.
4. Consumers verify before extraction. Pin repository and source SHA independently from
   the approved release/change, not from an untrusted downloaded manifest:

```sh
python3 scripts/stage_release.py --archive /downloads/factory.zip \
  --repository YOUR_OWNER/YOUR_FACTORY --source-sha APPROVED_40_HEX_COMMIT \
  --attestation-bundle /downloads/provenance.sigstore.json \
  --root /outside-checkouts/factory-install --out /outside-checkouts/staging.json
```

Public Sigstore, exact repository/workflow/ref/source and GitHub-hosted runner identity
are verified. Private attestation trust is an explicit alternative only where already
supported; no plan is enabled. An unavailable signature service blocks promotion. There
is no unsigned fallback and no automatic deployment of another application's production.

## Integration updates

Use a new verified factory distribution. `.factory/integration.json` records managed hashes
and factory version/source. Own config, rules and tasks are preserved; edited managed files
cause a conflict. Preview before applying on the consumer's own branch:

```sh
python3 /new-factory/factory.py upgrade --repo /consumer --out /outside/preview
python3 /new-factory/factory.py upgrade --repo /consumer --out /outside/application --apply
```

Inspect `plan.json`, `candidate/` and local conflicts first. Apply rechecks original and
candidate hashes, keeps external backups and restores touched files on write failure.
It executes no project commands and performs no GitHub mutation. Pre-manifest integrations
need a one-time manual comparison/migration. Keep the last working release and upgrade
backup until the consumer's real checks pass.

## Health and rollback

The installer verifies installed file hashes and source/version identity, then exercises
real CLI onboarding/doctor before activation. It retains current/previous version records:

```sh
python3 scripts/stage_release.py --root /outside-checkouts/factory-install \
  --rollback --out /outside-checkouts/rollback.json
```

Rollback health-checks the previous installation and leaves project data untouched. The
CI drill uses a synthetic previous version, detects deliberately corrupted candidate bytes,
rolls back and restores synthetic runtime budgets/state. This verifies mechanisms, not a
real application's database recovery. An actual application needs its own production target,
owner, data classification, health/abort checks, migration and database restore procedure.

## Incident response and maintenance

Stop promotion on a failed or unavailable check. Inspect its sanitized report, reproduce
on a branch, fix and rerun; never bypass the gate or run project code on the host. Suspected
credential exposure requires stopping services/publication, revocation/rotation at its
provider and private evidence retention. Never paste credentials into issues or CI logs.

The operating maintainer should subscribe to this repository's failed Actions notifications,
inspect failed runs before every release and review Dependabot weekly. CI also runs weekly
so newly disclosed dependency issues are rechecked. Repeat sandbox contracts for CLI upgrades.
Support is limited to the versions actually exercised in CI. Native Claude crew remains
optional/experimental until independently verified on an entitled colleague's account.
General Codex/Claude work packets do not start providers. The three-ticket controller is
a synthetic pilot; it is not a general unattended repository agent.

## Private runtime backup

Stop owned Tares services and finish/publish active tasks first. Do not copy live database
files. The backup command rejects identified running services and unfinished tasks, excludes
disposable worktrees/toolchains, preserves published receipts/budgets and reserves a private
mode-0600 archive. It prints counts/digest, never credential content:

```sh
python3 scripts/tares_runtime.py stop --runtime /private/factory-runtime
python3 scripts/runtime_backup.py backup --runtime /private/factory-runtime \
  --archive /private-backups/runtime.zip
python3 scripts/runtime_backup.py restore --runtime /private/restored-runtime \
  --archive /private-backups/runtime.zip --sha256 INDEPENDENTLY_RECORDED_SHA256
```

Backups contain private authentication/state/logs. Store them in access-controlled encrypted
storage; this tool does not encrypt. Never upload them as GitHub artifacts or release assets.
Restore into an absent directory with the independently recorded digest. Stale process
identities are not restored as signal authority; start services explicitly and check API/MCP
readiness. Recreate disposable caches/worktrees and retain published source commits separately.
Never reset spent attempts to recover a ticket. Runtime backups are not source-repository backups.

## Retention

Project evidence lasts 14 days; security, recovery and signed candidate evidence 30 days;
release receipts 90 days. Approved release bundles permanently contain the bound summary
and SBOM, with provenance stored alongside. Keep approved rollback assets under the owner's
retention policy. Review storage regularly. Cleanup is an explicit maintainer decision,
never an automatic deletion or budget increase. Private consumer repositories retain their
own quotas and approvals; exporting an integration does not change their plans or settings.
