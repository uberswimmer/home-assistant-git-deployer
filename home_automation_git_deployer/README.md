# Home Automation Git Deployer

A Home Assistant app for safely deploying selected YAML configuration from a Git repository.

## Highlights

- Read-only SSH access to the managed Git repository
- Explicit path allowlisting
- Local-drift detection
- Exact-commit approval for sensitive changes
- Automatic rollback when Home Assistant configuration validation fails
- Home Assistant-visible deployment and validation status
- Repository-managed distribution for native Home Assistant app updates
- No automatic Home Assistant restart
- No write access to the managed configuration repository

See `DOCS.md` for installation, configuration, and operating details.
