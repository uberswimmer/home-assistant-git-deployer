# Home Automation Git Deployer

<!-- Version history: 1.3.4 - 2026-09-07 - Documented exact AGENTS.md metadata handling and app-upgrade recovery from a blocked configuration deployment. -->
<!-- Version history: 1.3.3 - 2026-08-29 - Documented confirmed-delivery deduplication, bounded Pushover retry, persistent fallback, and notification-health status fields. -->
<!-- Version history: 1.3.2 - 2026-08-29 - Documented latched restart tracking and critical rollback-filesystem failure reporting with persistent notification fallback. -->
<!-- Version history: 1.3.1 - 2026-08-27 - Clarified sensitive-approval guidance for the full copyable target SHA included in Pushover notifications. -->
<!-- Version history: 1.3.0 - 2026-08-24 - Documented the flattened runtime, automated regression testing, and persistent repository-poll health/alert semantics. -->

This Home Assistant app safely deploys selected YAML files from a Git repository to Home Assistant.

## Security model

- Repository access is read-only through a repository-scoped SSH deploy key.
- The private SSH key is generated and retained in the app's private `/data` directory.
- No network ports or ingress UI are exposed.
- Only explicitly allowlisted Home Assistant YAML paths can be written.
- Local drift blocks deployment rather than being overwritten.
- `configuration.yaml` changes and managed-file deletions require approval of the exact commit SHA.
- A rollback copy is created before every deployment, and rollback filesystem
  failures are recorded as a distinct critical state.
- Every deployment must pass Home Assistant's own configuration check.
- Deployment, validation, rollback, and repository-fetch health status is published to `/config/.git_deployer_status.json` for Home Assistant dashboards.
- The app never restarts Home Assistant and never writes back to the managed configuration repository.

## Configuration

Set `repository` to the SSH URL of the private Git repository that contains your Home Assistant configuration, for example:

```text
git@github.com:OWNER/REPOSITORY.git
```

The repository option is intentionally not populated with a default so public distribution of this app does not expose a private repository name.

## First start

1. Configure the repository URL and other app options.
2. Start the app.
3. Open its log.
4. Copy the generated `ssh-ed25519 ... home-assistant-git-deployer` public key.
5. In the managed configuration repository, open **Settings > Deploy keys > Add deploy key**.
6. Name it `Home Assistant Git Deployer` and paste the public key.
7. **Do not** enable write access.
8. Restart the app, or wait for the next retry.

On its first successful connection, the app compares the current configured branch with production. It does not copy anything. If every managed file matches, it records that commit as the baseline and begins polling for later commits.

## Historical bootstrap recovery

`bootstrap_base_commit` handles the case where the repository advances before the first baseline poll completes. Set it to a known ancestor commit representing the deployed production configuration before the pending changes.

During bootstrap, the deployer accepts a managed local file only when it matches either:

- the historical bootstrap commit, or
- the corresponding file in the current target commit.

This target-aware comparison supports safe partial reconciliation. Content that matches neither baseline nor target still blocks deployment.

After bootstrap succeeds, the historical commit is recorded as `last_deployed_sha` and the app immediately follows the normal forward-only deployment path to the current configured branch. The option is ignored after the baseline is established and may then be cleared.

For `automations.yaml`, `scripts.yaml`, and `scenes.yaml`, blank, `[]`, `{}`, `null`, and `~` are treated as equivalent only when both compared representations are empty. Real content is compared normally.

## Managed paths

The deployer permits selected Home Assistant configuration content, including root dashboard YAML files, package YAML, theme YAML, and reusable Lovelace YAML under `dashboard_components/`.

The exact root files `README.md`, `AGENTS.md`, and `.gitignore`, plus repository metadata and development content under `docs/`, `hubitat/`, `local_apps/`, and `.github/`, are ignored and never copied into Home Assistant. Other unrecognized paths remain forbidden and block deployment until intentionally classified.

## Sensitive commit approval

If a commit changes `configuration.yaml` or deletes a managed YAML file, deployment pauses. The Pushover notification and app log include the full 40-character commit SHA; the notification places it on a separate `Full SHA:` line for easy copying. Paste that full SHA into `approved_sensitive_commit` in the app configuration. Only that exact commit is authorized. A later sensitive commit requires its own approval.

## Home Assistant status publishing

The app writes a non-secret status document to `/config/.git_deployer_status.json`. It records the most recent successful managed-code deployment, the Home Assistant configuration-check result, rollback validation when applicable, changed managed paths, whether the successful deployment contains configuration that requires activation, and the health of repository polling.

Repository-health fields include:

- `last_poll_at`: most recent poll attempt.
- `last_fetch_success_at`: most recent successful fetch from the configured repository.
- `last_fetch_result`: `success`, `error`, or `unknown`.
- `last_fetch_error`: most recent repository-access error, truncated to 2000 characters.
- `fetch_failure_started_at`: beginning of the current continuous repository-access outage, or `0` while healthy.
- `last_notification_at`: time of the most recent Pushover service attempt.
- `last_notification_result`: `success`, `error`, or `unknown`.
- `last_notification_title`: title of the most recent attempted remote notification.
- `last_notification_error`: delivery failure summary, truncated to 2000 characters.
- `notification_pending`: whether an ongoing blocking condition still needs confirmed remote delivery.
- `notification_retry_at`: earliest time for the next bounded retry.
- `pending_notification_title`: title of the pending alert.

If repository access fails continuously for 30 minutes, the app attempts a Pushover warning. The outage is marked notified only after Home Assistant confirms that the notification service call succeeded. A failed attempt creates a local persistent-notification fallback and retries remote delivery every 15 minutes while the outage continues. If the warning was delivered, the first successful repository fetch sends one recovery notification and clears the outage state.

The same confirmed-delivery rule applies to bootstrap, reconciliation, non-fast-forward, forbidden-path, sensitive-approval, and local-drift warnings. Failed delivery does not consume their deduplication marker. The local fallback is dismissed after a later successful remote attempt or when a pending repository-outage alert becomes obsolete after recovery.

Repository-only commits do not update the last managed-code deployment timestamp. The status document is runtime state and is intentionally not managed by Git.

The restart-required timestamp and SHA are latched only when a successful
deployment changes a restart-requiring path. A later dashboard-only, theme-only,
or dashboard-component-only deployment does not overwrite that latch. Home
Assistant compares the latched timestamp with its recorded startup time, so the
pending state clears only after Home Assistant has actually restarted. On the
first version 1.3.2 start, an existing version 1.3.1 pending-restart state is
migrated from its recorded managed-code deployment timestamp and SHA.

If filesystem restoration itself raises an exception, the app records
`last_rollback_result: error` and preserves the rollback exception separately in
`last_rollback_errors`. It attempts both a priority Pushover notification and a
local Home Assistant persistent notification so a Pushover outage cannot hide
the critical mixed-version condition.

## Runtime architecture and regression tests

Version 1.3.0 flattened the historical version-wrapper chain into one current `deployer.py`. Git history and `CHANGELOG.md` preserve earlier implementation history; the running add-on no longer imports prior-version Python modules.

The source repository includes standard-library regression tests for the safety behavior that must remain stable, including path classification, target-aware reconciliation, local-drift refusal, non-fast-forward refusal, exact-SHA sensitive approval, repository-only commits, deletion/rollback behavior, validation failure handling, rollback escalation, restart-required classification, and repository-outage alert deduplication/recovery. Version 1.3.1 also verifies that sensitive-approval notifications expose the complete target SHA. Version 1.3.2 adds coverage for restart-latch preservation and rollback manifest-read, copy, unlink, and escalation failures. Version 1.3.3 adds coverage for notification result reporting, delayed deduplication, bounded retry, and persistent fallback. The private-source publishing workflow runs these tests before publishing the app to the public repository.

## Activation

The deployer writes and validates files but deliberately does not reload or restart Home Assistant. After a successful deployment that changes packages or core configuration, activate it with a normal Home Assistant restart. Dashboard-only, theme-only, and reusable dashboard-component changes do not create a false restart-pending status.

## Updating the app

Version 1.2.0 and later are distributed from the Home Assistant app repository at:

```text
https://github.com/uberswimmer/home-assistant-git-deployer
```

The private `home-automation-config` repository remains the editable source of truth. Its publishing workflow copies approved Git Deployer source into the public repository automatically after changes reach `main` and the regression tests pass. Do not maintain a separate divergent implementation in the public repository.

Once that repository has been added to the Home Assistant App Store, future versions can be installed through Home Assistant's normal app update interface rather than by copying files into `/addons` manually.

### Recovering an AGENTS.md deployment block

Versions through 1.3.3 reject a commit that changes root `AGENTS.md` before staging or copying configuration. Install app version 1.3.4 or later after refreshing the app repository, then confirm the running version in the startup log or deployer status. The next poll retries from the last successful deployment and applies the normal drift, sensitive-approval, validation, and rollback checks. Activate Home Assistant changes after deployment succeeds as indicated by the restart status.

Merging changes under `local_apps/` publishes an app update; it does not upgrade the running app. Keep the existing deployment baseline and approval settings. Sensitive-commit approval cannot override a forbidden-path block.

## DROP observation adapter deployment (1.4.0)

Version 1.4.0 additionally manages exactly `__init__.py`, `manifest.json`,
`evidence.py` and `sensor.py` under `custom_components/drop_observation/`.
All changes to these files require approval of the exact target commit SHA and
latch a Core restart requirement. Other custom integrations/Python files remain
forbidden. The four files use the normal drift, baseline and rollback protections.

Install the published 1.4.0 app before deploying a commit containing these paths.
An older app blocks the commit; after upgrade it retries through the ordinary
checks. Do not reset the baseline, manually copy around drift checks or treat a
successful deployment as activation. Leave Water Protection commissioning disabled
until the operator has completed the source's controlled live checks.
