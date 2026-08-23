# Home Automation Git Deployer

This Home Assistant app safely deploys selected YAML files from a Git repository to Home Assistant.

## Security model

- Repository access is read-only through a repository-scoped SSH deploy key.
- The private SSH key is generated and retained in the app's private `/data` directory.
- No network ports or ingress UI are exposed.
- Only explicitly allowlisted Home Assistant YAML paths can be written.
- Local drift blocks deployment rather than being overwritten.
- `configuration.yaml` changes and managed-file deletions require approval of the exact commit SHA.
- A rollback copy is created before every deployment.
- Every deployment must pass Home Assistant's own configuration check.
- Deployment and validation status is published to `/config/.git_deployer_status.json` for Home Assistant dashboards.
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

Repository metadata and development content under `docs/`, `hubitat/`, `local_apps/`, and `.github/` is ignored. Other unrecognized paths remain forbidden and block deployment until intentionally classified.

## Sensitive commit approval

If a commit changes `configuration.yaml` or deletes a managed YAML file, deployment pauses. The notification and log include the full commit SHA. Paste that full SHA into `approved_sensitive_commit` in the app configuration. Only that exact commit is authorized. A later sensitive commit requires its own approval.

## Home Assistant status publishing

The app writes a non-secret status document to `/config/.git_deployer_status.json`. It records the most recent successful managed-code deployment, the Home Assistant configuration-check result, rollback validation when applicable, changed managed paths, and whether the successful deployment contains configuration that requires activation.

Repository-only commits do not update the last managed-code deployment timestamp. The status document is runtime state and is intentionally not managed by Git.

## Activation

The deployer writes and validates files but deliberately does not reload or restart Home Assistant. After a successful deployment that changes packages or core configuration, activate it with a normal Home Assistant restart. Dashboard-only, theme-only, and reusable dashboard-component changes do not create a false restart-pending status.

## Updating the app

Version 1.2.0 and later are intended to be distributed from the Home Assistant app repository at:

```text
https://github.com/uberswimmer/home-assistant-git-deployer
```

Once that repository has been added to the Home Assistant App Store, future versions can be installed through Home Assistant's normal app update interface rather than by copying files into `/addons` manually.
