# Changelog

## 1.3.2 - 2026-08-29

- Latches the timestamp and SHA of the most recent successful restart-requiring deployment so later dashboard-only, theme-only, or dashboard-component-only deployments cannot conceal a still-pending Home Assistant restart, including migration of the prior version's pending-restart state.
- Records filesystem rollback exceptions as `last_rollback_result: error` and preserves the filesystem error separately from the original deployment validation error.
- Attempts both priority Pushover and Home Assistant persistent notifications when rollback restoration or rollback validation fails critically.
- Adds regression coverage for restart-latch preservation and rollback manifest-read, copy, unlink, and critical escalation failures.

## 1.3.1 - 2026-08-27

- Sensitive-commit approval notifications now include the full 40-character target commit SHA on its own copyable line.
- Preserves exact-commit approval semantics and all existing deployment, validation, rollback, drift-protection, and repository-health behavior.
- Adds regression coverage to ensure approval notifications continue to expose the complete target SHA.

## 1.3.0 - 2026-08-24

- Flattened the previously layered `deployer.py` / `deployer_v103.py` / `deployer_v110.py` / `deployer_v120.py` runtime into one current `deployer.py`; Git history now carries implementation history instead of runtime wrapper modules.
- Preserves the existing read-only Git access, path allowlisting, forward-only deployment, local-drift blocking, exact-commit sensitive approval, target-aware bootstrap reconciliation, Home Assistant configuration validation, automatic rollback, and manual activation model.
- Adds `last_poll_at`, `last_fetch_success_at`, `last_fetch_result`, `last_fetch_error`, and `fetch_failure_started_at` to Home Assistant-visible deployer status.
- Sends one Pushover warning after repository access has failed continuously for 30 minutes, suppresses duplicates during the same outage, and sends one recovery notification after monitoring resumes.
- Performs one repository fetch per poll cycle instead of the wrapper-era duplicate fetch while preserving deployment semantics.
- Adds automated regression tests for deployment safety invariants and runs them before the private-source publishing workflow can update the public Home Assistant app repository.

## 1.2.1 - 2026-08-23

- Metadata-only repository-managed update test release.
- No deployment, reconciliation, validation, rollback, path-classification, or status-reporting logic changed from 1.2.0.

## 1.2.0 - 2026-08-23

- Prepared the Git Deployer for native Home Assistant repository-managed updates.
- Removed the private Home Assistant configuration repository from distributable defaults.
- Changed the app metadata URL to the public distribution repository.
- Explicitly ignores `.github/` repository metadata so GitHub Actions publishing workflows cannot block managed configuration deployments.
- Added a 1.2 entry point while preserving the proven 1.1 deployment, reconciliation, validation, rollback, and status-reporting behavior.

## 1.1.0 - 2026-08-23

- Publishes Home Assistant-visible deployment and validation status to `/config/.git_deployer_status.json`.
- Tracks the last successful managed-code deployment separately from repository-only commits.
- Records deployment validation failures and rollback validation results for dashboard reporting.
- Allows Git-managed reusable Lovelace components under `dashboard_components/`.
- Marks whether the most recent managed deployment contains configuration that requires activation by Home Assistant restart/reload.

## 1.0.3 - 2026-08-22

- Bootstrap and local-drift reconciliation now accept a managed file that already matches the current GitHub target, while still blocking any content that matches neither the historical baseline nor the target commit.
- This supports safe recovery when production contains a legitimate file that was omitted from the original GitHub baseline and is corrected in `main` before migration deployment.

## 1.0.2 - 2026-08-22

- Added one-time historical bootstrap reconciliation so an already-running production configuration can be anchored to a known ancestor commit even if `main` advances before the first baseline poll completes.
- Treats blank, `[]`, `{}`, `null`, and `~` as equivalent empty representations for `automations.yaml`, `scripts.yaml`, and `scenes.yaml` during baseline and drift checks.

## 1.0.1 - 2026-08-22

- Pinned GitHub's published Ed25519 SSH host key in the app image so the first repository connection does not rely on runtime trust-on-first-use discovery.

## 1.0.0 - 2026-08-22

- Initial staged GitHub deployment implementation with read-only SSH access, strict path allowlisting, local-drift detection, exact-commit approval for sensitive changes, rollback, Home Assistant configuration validation, and Pushover reporting.
