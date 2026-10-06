# Home Automation Git Deployer

<!-- Version history: 1.3.3 - 2026-08-29 - Documented delivery-aware alert deduplication, bounded retries, persistent fallback, and Home Assistant notification-health telemetry. -->
<!-- Version history: 1.3.0 - 2026-08-24 - Documented the flattened single-runtime architecture, repository-health monitoring, and automated regression coverage. -->

A Home Assistant app for safely deploying selected YAML configuration from a Git repository.

## Highlights

- Read-only SSH access to the managed Git repository
- Explicit path allowlisting
- Local-drift detection
- Exact-commit approval for sensitive changes
- Automatic rollback when Home Assistant configuration validation fails
- Home Assistant-visible deployment, validation, and repository-fetch health status
- Delivery-confirmed Pushover deduplication with bounded retry and Home Assistant persistent-notification fallback
- Delayed warning for a sustained repository-access outage, with recovery notification
- Single flattened Python runtime with automated regression tests for deployment safety invariants
- Repository-managed distribution for native Home Assistant app updates
- No automatic Home Assistant restart
- No write access to the managed configuration repository

See `DOCS.md` for installation, configuration, and operating details.

DROP observation adapter files require app 1.4.0 and exact-commit approval. See
[the operator guide](DOCS.md#drop-observation-adapter-deployment-140).
