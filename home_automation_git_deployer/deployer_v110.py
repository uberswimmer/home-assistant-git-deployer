#!/usr/bin/env python3
from __future__ import annotations

# =============================================================================
# HOME AUTOMATION GIT DEPLOYER 1.1.0
# =============================================================================
# Version history:
# 1.1.0 - 2026-08-23 - Added Home Assistant-visible deployment/validation status publishing and allowlisted reusable dashboard components while preserving v1.0.3 reconciliation behavior.
# =============================================================================

import os
import subprocess
import sys
import time
from pathlib import PurePosixPath
from typing import Any

import deployer as base
import deployer_v103 as previous

VERSION = "1.1.0"
STATUS_FILE = base.HA_DIR / ".git_deployer_status.json"

ORIGINAL_CLASSIFY_PATH = base.classify_path
ORIGINAL_MANAGED_LOCAL_PATHS = base.managed_local_paths
ORIGINAL_APPLY_CHANGES = base.apply_changes
ORIGINAL_CHECK_CONFIG = base.check_config

ACTIVE_SHA: str | None = None
CHECK_PHASE = "idle"
APPLY_COMPLETED = False
LAST_DEPLOY_VALIDATION_AT = 0
LAST_DEPLOY_VALIDATION_RESULT = ""
LAST_DEPLOY_VALIDATION_ERRORS = ""
LAST_ROLLBACK_AT = 0
LAST_ROLLBACK_RESULT = ""


def classify_path(path: str) -> str:
    p = PurePosixPath(path)
    if (
        len(p.parts) == 2
        and p.parts[0] == "dashboard_components"
        and p.suffix == ".yaml"
    ):
        return "allowed"
    return ORIGINAL_CLASSIFY_PATH(path)


def managed_local_paths() -> set[str]:
    paths = ORIGINAL_MANAGED_LOCAL_PATHS()
    base_dir = base.HA_DIR / "dashboard_components"
    if base_dir.exists():
        for path in base_dir.glob("*.yaml"):
            if path.is_file():
                paths.add(f"dashboard_components/{path.name}")
    return paths


def restart_required_for_paths(paths: list[str]) -> bool:
    for path in paths:
        if path.startswith("dashboard_components/"):
            continue
        if path.startswith("themes/"):
            continue
        if path.endswith("_dashboard.yaml"):
            continue
        return True
    return False


def set_status_state(**updates: Any) -> dict[str, Any]:
    st = base.state()
    st.update(updates)
    base.set_state(st)
    publish_status(st)
    return st


def publish_status(st: dict[str, Any] | None = None) -> None:
    if st is None:
        st = base.state()

    payload = {
        "deployer_version": VERSION,
        "last_code_deploy_at": int(st.get("last_code_deploy_at") or 0),
        "last_code_deploy_sha": str(st.get("last_code_deploy_sha") or ""),
        "last_validation_at": int(st.get("last_validation_at") or 0),
        "last_validation_result": str(
            st.get("last_validation_result") or "unknown"
        ),
        "last_validation_errors": str(
            st.get("last_validation_errors") or ""
        )[:2000],
        "last_rollback_at": int(st.get("last_rollback_at") or 0),
        "last_rollback_result": str(
            st.get("last_rollback_result") or "unknown"
        ),
        "last_attempted_sha": str(st.get("last_attempted_sha") or ""),
        "last_attempted_files": list(st.get("last_attempted_files") or []),
        "last_changed_files": list(st.get("last_changed_files") or []),
        "restart_required": bool(st.get("restart_required", False)),
        "last_status_message": str(st.get("last_status_message") or ""),
    }

    base.save_json_atomic(STATUS_FILE, payload)
    os.chmod(STATUS_FILE, 0o644)


def apply_changes(changes: list[tuple[str, str]], commit: str) -> None:
    global APPLY_COMPLETED
    global CHECK_PHASE
    global LAST_DEPLOY_VALIDATION_AT
    global LAST_DEPLOY_VALIDATION_RESULT
    global LAST_DEPLOY_VALIDATION_ERRORS

    APPLY_COMPLETED = False
    try:
        ORIGINAL_APPLY_CHANGES(changes, commit)
    except Exception as err:
        check_time = int(time.time())
        LAST_DEPLOY_VALIDATION_AT = check_time
        LAST_DEPLOY_VALIDATION_RESULT = "error"
        LAST_DEPLOY_VALIDATION_ERRORS = f"File deployment failed: {err}"
        CHECK_PHASE = "rollback"
        set_status_state(
            last_validation_at=check_time,
            last_validation_result="error",
            last_validation_errors=LAST_DEPLOY_VALIDATION_ERRORS,
            last_status_message=(
                f"Deployment {commit[:12]} failed while applying files."
            ),
        )
        raise
    APPLY_COMPLETED = True


def check_config() -> tuple[bool, str]:
    global CHECK_PHASE
    global LAST_DEPLOY_VALIDATION_AT
    global LAST_DEPLOY_VALIDATION_RESULT
    global LAST_DEPLOY_VALIDATION_ERRORS
    global LAST_ROLLBACK_AT
    global LAST_ROLLBACK_RESULT

    check_time = int(time.time())
    is_deployment_check = APPLY_COMPLETED and CHECK_PHASE == "deployment"

    try:
        valid, errors = ORIGINAL_CHECK_CONFIG()
    except Exception as err:
        if is_deployment_check:
            LAST_DEPLOY_VALIDATION_AT = check_time
            LAST_DEPLOY_VALIDATION_RESULT = "error"
            LAST_DEPLOY_VALIDATION_ERRORS = str(err)
            set_status_state(
                last_validation_at=check_time,
                last_validation_result="error",
                last_validation_errors=str(err),
                last_status_message=(
                    f"Deployment {ACTIVE_SHA[:12] if ACTIVE_SHA else ''} "
                    "configuration check raised an error."
                ).strip(),
            )
            CHECK_PHASE = "rollback"
        else:
            LAST_ROLLBACK_AT = check_time
            LAST_ROLLBACK_RESULT = "error"
            set_status_state(
                last_rollback_at=check_time,
                last_rollback_result="error",
                last_status_message=(
                    f"Rollback after {ACTIVE_SHA[:12] if ACTIVE_SHA else ''} "
                    "could not be validated."
                ).strip(),
            )
        raise

    if is_deployment_check:
        LAST_DEPLOY_VALIDATION_AT = check_time
        LAST_DEPLOY_VALIDATION_RESULT = "valid" if valid else "invalid"
        LAST_DEPLOY_VALIDATION_ERRORS = errors
        set_status_state(
            last_validation_at=check_time,
            last_validation_result=LAST_DEPLOY_VALIDATION_RESULT,
            last_validation_errors=errors,
            last_rollback_at=0,
            last_rollback_result="not_needed" if valid else "pending",
            last_status_message=(
                f"Deployment {ACTIVE_SHA[:12] if ACTIVE_SHA else ''} "
                f"configuration validation {'passed' if valid else 'failed'}."
            ).strip(),
        )
        CHECK_PHASE = "rollback"
    else:
        LAST_ROLLBACK_AT = check_time
        LAST_ROLLBACK_RESULT = "valid" if valid else "invalid"
        set_status_state(
            last_rollback_at=check_time,
            last_rollback_result=LAST_ROLLBACK_RESULT,
            last_status_message=(
                f"Rollback after {ACTIVE_SHA[:12] if ACTIVE_SHA else ''} "
                f"configuration validation {'passed' if valid else 'failed'}."
            ).strip(),
        )

    return valid, errors


def deploy_once_with_status(options: dict[str, Any], target_sha: str | None) -> None:
    global ACTIVE_SHA, CHECK_PHASE, APPLY_COMPLETED
    global LAST_DEPLOY_VALIDATION_AT
    global LAST_DEPLOY_VALIDATION_RESULT
    global LAST_DEPLOY_VALIDATION_ERRORS
    global LAST_ROLLBACK_AT
    global LAST_ROLLBACK_RESULT

    st_before = base.state()
    old_sha = str(st_before.get("last_deployed_sha") or "")

    changed_files: list[str] = []
    if target_sha and old_sha and old_sha != target_sha:
        try:
            changed_files = [
                path
                for _, path in base.diff_name_status(old_sha, target_sha)
                if classify_path(path) == "allowed"
            ]
        except Exception:
            base.LOG.exception(
                "Unable to precompute managed paths for deployment status"
            )

    ACTIVE_SHA = target_sha
    CHECK_PHASE = "deployment"
    APPLY_COMPLETED = False
    LAST_DEPLOY_VALIDATION_AT = 0
    LAST_DEPLOY_VALIDATION_RESULT = ""
    LAST_DEPLOY_VALIDATION_ERRORS = ""
    LAST_ROLLBACK_AT = 0
    LAST_ROLLBACK_RESULT = ""

    if target_sha and changed_files:
        set_status_state(
            last_attempted_sha=target_sha,
            last_attempted_files=changed_files,
            last_status_message=(
                f"Preparing managed deployment {target_sha[:12]}."
            ),
        )

    base.deploy_once(options)

    st_after = base.state()
    new_recorded_sha = str(st_after.get("last_deployed_sha") or "")

    if LAST_DEPLOY_VALIDATION_RESULT:
        st_after.update(
            {
                "last_validation_at": LAST_DEPLOY_VALIDATION_AT,
                "last_validation_result": LAST_DEPLOY_VALIDATION_RESULT,
                "last_validation_errors": LAST_DEPLOY_VALIDATION_ERRORS,
            }
        )
    if LAST_ROLLBACK_RESULT:
        st_after.update(
            {
                "last_rollback_at": LAST_ROLLBACK_AT,
                "last_rollback_result": LAST_ROLLBACK_RESULT,
            }
        )

    if (
        target_sha
        and old_sha
        and old_sha != target_sha
        and new_recorded_sha == target_sha
        and changed_files
        and LAST_DEPLOY_VALIDATION_RESULT == "valid"
    ):
        deployed_at = int(time.time())
        st_after.update(
            {
                "last_code_deploy_at": deployed_at,
                "last_code_deploy_sha": target_sha,
                "last_changed_files": changed_files,
                "restart_required": restart_required_for_paths(changed_files),
                "last_rollback_result": "not_needed",
                "last_status_message": (
                    f"Deployment {target_sha[:12]} passed configuration "
                    "validation."
                ),
            }
        )
        base.set_state(st_after)
    elif LAST_DEPLOY_VALIDATION_RESULT or LAST_ROLLBACK_RESULT:
        base.set_state(st_after)

    publish_status(base.state())

    ACTIVE_SHA = None
    CHECK_PHASE = "idle"
    APPLY_COMPLETED = False


def main() -> int:
    base.classify_path = classify_path
    base.managed_local_paths = managed_local_paths
    base.initial_reconcile = previous.initial_reconcile
    base.local_drift = previous.local_drift
    base.apply_changes = apply_changes
    base.check_config = check_config

    publish_status()
    base.LOG.info("Home Automation Git Deployer %s starting", VERSION)

    while True:
        try:
            options = base.load_options()
            repository = str(options["repository"])
            branch = str(options["branch"])

            try:
                base.ensure_ssh_material()
                previous.TARGET_SHA = base.ensure_repository(repository, branch)
            except subprocess.CalledProcessError:
                previous.TARGET_SHA = None

            if previous.bootstrap_if_needed(options):
                deploy_once_with_status(options, previous.TARGET_SHA)

            delay = max(60, int(options.get("poll_seconds", 300)))
        except KeyboardInterrupt:
            return 0
        except Exception:
            base.LOG.exception("Unhandled deployer error")
            try:
                publish_status()
            except Exception:
                base.LOG.exception("Unable to publish deployer status")
            delay = 60

        time.sleep(delay)


if __name__ == "__main__":
    sys.exit(main())
