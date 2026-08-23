# Changelog

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
