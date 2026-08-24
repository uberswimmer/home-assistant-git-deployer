#!/usr/bin/env python3
from __future__ import annotations

# =============================================================================
# HOME AUTOMATION GIT DEPLOYER 1.3.0
# =============================================================================
# Version history:
# 1.3.0 - 2026-08-24 - Flattened the proven v1.2.1 runtime into one deployer, added persistent repository-poll health with delayed deduplicated outage/recovery notifications, and retained all existing deployment safety controls.
# =============================================================================

import hashlib
import json
import logging
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import time
from typing import Any

import requests

VERSION = "1.3.0"
FETCH_FAILURE_ALERT_SECONDS = 30 * 60

DATA_DIR = Path("/data")
HA_DIR = Path("/homeassistant")
REPO_DIR = DATA_DIR / "repository"
STATE_FILE = DATA_DIR / "state.json"
SSH_DIR = DATA_DIR / "ssh"
SSH_KEY = SSH_DIR / "id_ed25519"
KNOWN_HOSTS = SSH_DIR / "known_hosts"
ROLLBACK_ROOT = DATA_DIR / "rollback"
OPTIONS_FILE = DATA_DIR / "options.json"
STATUS_FILE = HA_DIR / ".git_deployer_status.json"

ROOT_ALLOWED = {
    "configuration.yaml",
    "automations.yaml",
    "scripts.yaml",
    "scenes.yaml",
    "comfort_dashboard.yaml",
    "iaq_dashboard.yaml",
    "security_dashboard.yaml",
}
IGNORED_PREFIXES = ("docs/", "hubitat/", "local_apps/", ".github/")
IGNORED_EXACT = {"README.md", ".gitignore"}
EMPTY_YAML_REPRESENTATIONS = {b"", b"[]", b"{}", b"null", b"~"}
EMPTY_UI_FILES = {"automations.yaml", "scripts.yaml", "scenes.yaml"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
LOG = logging.getLogger("git-deployer")


class DeployError(RuntimeError):
    pass


def run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    capture: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    LOG.debug("run: %s", " ".join(cmd))
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        check=check,
        text=True,
        capture_output=capture,
        env=merged_env,
    )


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except json.JSONDecodeError as err:
        raise DeployError(f"Invalid JSON in {path}: {err}") from err


def save_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temp, path)


def load_options() -> dict[str, Any]:
    options = load_json(OPTIONS_FILE, {})
    required = {
        "repository",
        "branch",
        "poll_seconds",
        "pushover_service",
        "approved_sensitive_commit",
    }
    missing = sorted(required - options.keys())
    if missing:
        raise DeployError(f"Missing app option(s): {', '.join(missing)}")
    return options


def state() -> dict[str, Any]:
    return load_json(STATE_FILE, {})


def set_state(data: dict[str, Any]) -> None:
    save_json_atomic(STATE_FILE, data)


def set_status_state(**updates: Any) -> dict[str, Any]:
    st = state()
    st.update(updates)
    set_state(st)
    publish_status(st)
    return st


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def content_matches(path: str, expected: bytes, actual: bytes) -> bool:
    if expected == actual:
        return True
    if path in EMPTY_UI_FILES:
        return (
            expected.strip() in EMPTY_YAML_REPRESENTATIONS
            and actual.strip() in EMPTY_YAML_REPRESENTATIONS
        )
    return False


def classify_path(path: str) -> str:
    p = PurePosixPath(path)
    if path in ROOT_ALLOWED:
        return "allowed"
    if (
        len(p.parts) == 1
        and p.suffix == ".yaml"
        and p.name.endswith("_dashboard.yaml")
    ):
        return "allowed"
    if (
        len(p.parts) == 2
        and p.parts[0] in {"packages", "themes", "dashboard_components"}
        and p.suffix == ".yaml"
    ):
        return "allowed"
    if path in IGNORED_EXACT or any(
        path.startswith(prefix) for prefix in IGNORED_PREFIXES
    ):
        return "ignored"
    return "forbidden"


def managed_local_paths() -> set[str]:
    paths: set[str] = set()
    for name in ROOT_ALLOWED:
        if (HA_DIR / name).exists():
            paths.add(name)
    for path in HA_DIR.glob("*_dashboard.yaml"):
        if path.is_file():
            paths.add(path.name)
    for folder in ("packages", "themes", "dashboard_components"):
        base_dir = HA_DIR / folder
        if base_dir.exists():
            for path in base_dir.glob("*.yaml"):
                if path.is_file():
                    paths.add(f"{folder}/{path.name}")
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


def ensure_ssh_material() -> str:
    SSH_DIR.mkdir(parents=True, exist_ok=True)
    if not SSH_KEY.exists():
        run(
            [
                "ssh-keygen",
                "-q",
                "-t",
                "ed25519",
                "-N",
                "",
                "-C",
                "home-assistant-git-deployer",
                "-f",
                str(SSH_KEY),
            ]
        )
        os.chmod(SSH_KEY, 0o600)
        LOG.warning(
            "Generated a new deployment key. Add the public key below to GitHub "
            "as a READ-ONLY deploy key."
        )
    if not KNOWN_HOSTS.exists():
        scan = run(["ssh-keyscan", "-t", "ed25519", "github.com"], check=True)
        if not scan.stdout.strip():
            raise DeployError("ssh-keyscan returned no github.com host key")
        KNOWN_HOSTS.write_text(scan.stdout, encoding="utf-8")
        os.chmod(KNOWN_HOSTS, 0o644)
    return SSH_KEY.with_suffix(".pub").read_text(encoding="utf-8").strip()


def git_env() -> dict[str, str]:
    return {
        "GIT_SSH_COMMAND": (
            f"ssh -i {SSH_KEY} -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes "
            f"-o UserKnownHostsFile={KNOWN_HOSTS}"
        )
    }


def ensure_repository(repository: str, branch: str) -> str:
    if not REPO_DIR.exists():
        REPO_DIR.parent.mkdir(parents=True, exist_ok=True)
        run(
            ["git", "clone", "--no-checkout", repository, str(REPO_DIR)],
            env=git_env(),
        )
    elif not (REPO_DIR / ".git").exists():
        raise DeployError(f"{REPO_DIR} exists but is not a Git repository")

    current_origin = run(
        ["git", "remote", "get-url", "origin"], cwd=REPO_DIR
    ).stdout.strip()
    if current_origin != repository:
        run(
            ["git", "remote", "set-url", "origin", repository],
            cwd=REPO_DIR,
        )

    run(
        ["git", "fetch", "--prune", "origin", branch],
        cwd=REPO_DIR,
        env=git_env(),
    )
    return run(
        ["git", "rev-parse", f"origin/{branch}"], cwd=REPO_DIR
    ).stdout.strip()


def git_file_bytes(commit: str, path: str) -> bytes | None:
    proc = subprocess.run(
        ["git", "show", f"{commit}:{path}"],
        cwd=str(REPO_DIR),
        check=False,
        capture_output=True,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout


def git_allowed_paths(commit: str) -> set[str]:
    listing = run(
        ["git", "ls-tree", "-r", "--name-only", commit], cwd=REPO_DIR
    ).stdout.splitlines()
    return {path for path in listing if classify_path(path) == "allowed"}


def target_file_bytes(target_sha: str | None, path: str) -> bytes | None:
    if not target_sha:
        return None
    return git_file_bytes(target_sha, path)


def initial_reconcile(
    commit: str, target_sha: str | None = None
) -> tuple[bool, list[str]]:
    repo_paths = git_allowed_paths(commit)
    local_paths = managed_local_paths()
    problems: list[str] = []

    for path in sorted(repo_paths | local_paths):
        local = HA_DIR / path
        old_data = git_file_bytes(commit, path)
        target_data = target_file_bytes(target_sha, path)

        if path not in repo_paths:
            if (
                local.exists()
                and target_data is not None
                and content_matches(path, target_data, local.read_bytes())
            ):
                continue
            problems.append(f"local-only managed file: {path}")
            continue

        if not local.exists():
            problems.append(f"missing locally: {path}")
            continue

        actual = local.read_bytes()
        if old_data is not None and content_matches(path, old_data, actual):
            continue
        if target_data is not None and content_matches(path, target_data, actual):
            continue
        problems.append(f"content differs: {path}")

    return not problems, problems


def is_ancestor(old: str, new: str) -> bool:
    proc = run(
        ["git", "merge-base", "--is-ancestor", old, new],
        cwd=REPO_DIR,
        check=False,
    )
    return proc.returncode == 0


def diff_name_status(old: str, new: str) -> list[tuple[str, str]]:
    output = run(
        ["git", "diff", "--name-status", "--no-renames", old, new],
        cwd=REPO_DIR,
    ).stdout
    changes: list[tuple[str, str]] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t", 1)
        if len(parts) != 2:
            raise DeployError(f"Unexpected git diff line: {line}")
        changes.append((parts[0], parts[1]))
    return changes


def local_drift(
    changes: list[tuple[str, str]], old: str, target_sha: str | None = None
) -> list[str]:
    problems: list[str] = []

    for status_code, path in changes:
        if classify_path(path) != "allowed":
            continue

        local = HA_DIR / path
        old_data = git_file_bytes(old, path)
        target_data = target_file_bytes(target_sha, path)

        if not local.exists():
            if old_data is None:
                continue
            if status_code == "D" and target_data is None:
                continue
            problems.append(f"{path}: missing locally")
            continue

        actual = local.read_bytes()
        if old_data is not None and content_matches(path, old_data, actual):
            continue
        if target_data is not None and content_matches(path, target_data, actual):
            continue

        if old_data is None:
            problems.append(
                f"{path}: exists locally but matches neither the baseline nor target commit"
            )
        else:
            problems.append(
                f"{path}: local content matches neither the baseline nor target commit"
            )

    return problems


def backup_changes(changes: list[tuple[str, str]], commit: str) -> Path:
    root = ROLLBACK_ROOT / commit
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {"commit": commit, "files": {}}
    for _, path in changes:
        if classify_path(path) != "allowed":
            continue
        src = HA_DIR / path
        existed = src.exists()
        manifest["files"][path] = {"existed": existed}
        if existed:
            dst = root / "files" / path
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    save_json_atomic(root / "manifest.json", manifest)
    return root


def apply_changes(changes: list[tuple[str, str]], commit: str) -> None:
    for status_code, path in changes:
        if classify_path(path) != "allowed":
            continue
        dest = HA_DIR / path
        if status_code == "D":
            if dest.exists():
                dest.unlink()
            LOG.info("Deleted %s", path)
            continue
        data = git_file_bytes(commit, path)
        if data is None:
            raise DeployError(f"Unable to read {path} from commit {commit}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".gitdeploy.tmp")
        tmp.write_bytes(data)
        os.chmod(tmp, 0o644)
        os.replace(tmp, dest)
        LOG.info("Deployed %s", path)


def rollback(backup_root: Path) -> None:
    manifest = load_json(backup_root / "manifest.json", {})
    for path, meta in manifest.get("files", {}).items():
        dest = HA_DIR / path
        if meta.get("existed"):
            src = backup_root / "files" / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
        elif dest.exists():
            dest.unlink()
        LOG.info("Rolled back %s", path)


def ha_headers() -> dict[str, str]:
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not token:
        raise DeployError("SUPERVISOR_TOKEN is not available")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def check_config() -> tuple[bool, str]:
    url = "http://supervisor/core/api/config/core/check_config"
    response = requests.post(url, headers=ha_headers(), json={}, timeout=180)
    response.raise_for_status()
    result = response.json()
    valid = result.get("result") == "valid"
    return valid, str(result.get("errors") or "")


def notify(
    options: dict[str, Any], title: str, message: str, *, priority: int = 0
) -> None:
    service = str(options.get("pushover_service", "notify.pushover"))
    if "." not in service:
        LOG.warning(
            "Invalid Pushover service name %r; expected notify.pushover", service
        )
        return
    domain, service_name = service.split(".", 1)
    url = f"http://supervisor/core/api/services/{domain}/{service_name}"
    payload = {
        "title": title,
        "message": message,
        "data": {"priority": priority, "sound": "intermission"},
    }
    try:
        response = requests.post(
            url, headers=ha_headers(), json=payload, timeout=30
        )
        response.raise_for_status()
    except Exception as err:
        LOG.error("Unable to send Pushover notification: %s", err)


def publish_status(st: dict[str, Any] | None = None) -> None:
    if st is None:
        st = state()

    payload = {
        "deployer_version": VERSION,
        "last_code_deploy_at": int(st.get("last_code_deploy_at") or 0),
        "last_code_deploy_sha": str(st.get("last_code_deploy_sha") or ""),
        "last_validation_at": int(st.get("last_validation_at") or 0),
        "last_validation_result": str(
            st.get("last_validation_result") or "unknown"
        ),
        "last_validation_errors": str(st.get("last_validation_errors") or "")[:2000],
        "last_rollback_at": int(st.get("last_rollback_at") or 0),
        "last_rollback_result": str(
            st.get("last_rollback_result") or "unknown"
        ),
        "last_attempted_sha": str(st.get("last_attempted_sha") or ""),
        "last_attempted_files": list(st.get("last_attempted_files") or []),
        "last_changed_files": list(st.get("last_changed_files") or []),
        "restart_required": bool(st.get("restart_required", False)),
        "last_status_message": str(st.get("last_status_message") or ""),
        "last_poll_at": int(st.get("last_poll_at") or 0),
        "last_fetch_success_at": int(st.get("last_fetch_success_at") or 0),
        "last_fetch_result": str(st.get("last_fetch_result") or "unknown"),
        "last_fetch_error": str(st.get("last_fetch_error") or "")[:2000],
        "fetch_failure_started_at": int(st.get("fetch_failure_started_at") or 0),
    }

    save_json_atomic(STATUS_FILE, payload)
    os.chmod(STATUS_FILE, 0o644)


def record_fetch_success(options: dict[str, Any], now_ts: int | None = None) -> None:
    if now_ts is None:
        now_ts = int(time.time())
    st = state()
    was_notified = bool(st.get("fetch_failure_notified", False))
    st.update(
        {
            "last_poll_at": now_ts,
            "last_fetch_success_at": now_ts,
            "last_fetch_result": "success",
            "last_fetch_error": "",
            "fetch_failure_started_at": 0,
            "fetch_failure_notified": False,
        }
    )
    set_state(st)
    publish_status(st)
    if was_notified:
        notify(
            options,
            "Git deployer repository access restored",
            "Repository access has recovered and deployment monitoring is active again.",
        )


def record_fetch_failure(
    options: dict[str, Any], error: Exception | str, now_ts: int | None = None
) -> None:
    if now_ts is None:
        now_ts = int(time.time())
    st = state()
    started_at = int(st.get("fetch_failure_started_at") or 0)
    if started_at <= 0:
        started_at = now_ts
    notified = bool(st.get("fetch_failure_notified", False))
    if isinstance(error, Exception):
        error_text = str(error).strip() or error.__class__.__name__
    else:
        error_text = str(error)
    st.update(
        {
            "last_poll_at": now_ts,
            "last_fetch_result": "error",
            "last_fetch_error": error_text,
            "fetch_failure_started_at": started_at,
        }
    )
    if not notified and now_ts - started_at >= FETCH_FAILURE_ALERT_SECONDS:
        notify(
            options,
            "Git deployer repository access problem",
            "Repository access has failed continuously for at least 30 minutes. "
            "No configuration updates can be detected until access recovers. "
            "Check the Git Deployer app log.",
            priority=1,
        )
        st["fetch_failure_notified"] = True
    set_state(st)
    publish_status(st)


def prune_rollbacks(keep: int = 5) -> None:
    if not ROLLBACK_ROOT.exists():
        return
    dirs = sorted(
        (path for path in ROLLBACK_ROOT.iterdir() if path.is_dir()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for old in dirs[keep:]:
        shutil.rmtree(old, ignore_errors=True)


def bootstrap_if_needed(
    options: dict[str, Any], target_sha: str
) -> bool:
    st = state()
    if st.get("last_deployed_sha"):
        return True

    bootstrap_sha = str(options.get("bootstrap_base_commit", "")).strip()
    if not bootstrap_sha:
        return True

    exists = subprocess.run(
        ["git", "cat-file", "-e", f"{bootstrap_sha}^{{commit}}"],
        cwd=str(REPO_DIR),
        check=False,
        capture_output=True,
    )
    if exists.returncode != 0:
        msg = f"Bootstrap commit does not exist locally: {bootstrap_sha}"
        LOG.error(msg)
        marker = "bootstrap-missing:" + bootstrap_sha
        if st.get("last_notified_problem") != marker:
            notify(options, "Git deploy bootstrap blocked", msg, priority=1)
            st["last_notified_problem"] = marker
            set_state(st)
        return False

    if not is_ancestor(bootstrap_sha, target_sha):
        branch = str(options["branch"])
        msg = (
            f"Bootstrap commit {bootstrap_sha[:12]} is not an ancestor of "
            f"current {branch} {target_sha[:12]}"
        )
        LOG.error(msg)
        marker = "bootstrap-ancestor:" + bootstrap_sha + ":" + target_sha
        if st.get("last_notified_problem") != marker:
            notify(options, "Git deploy bootstrap blocked", msg, priority=1)
            st["last_notified_problem"] = marker
            set_state(st)
        return False

    matches, problems = initial_reconcile(bootstrap_sha, target_sha)
    if not matches:
        LOG.error(
            "Bootstrap reconciliation against %s failed; no files were changed:\n- %s",
            bootstrap_sha,
            "\n- ".join(problems),
        )
        key = "bootstrap:" + hashlib.sha256(
            (bootstrap_sha + "\n" + "\n".join(problems)).encode()
        ).hexdigest()
        if st.get("last_notified_problem") != key:
            notify(
                options,
                "Git deployer needs reconciliation",
                f"Production does not match bootstrap commit {bootstrap_sha[:12]} "
                "or current target files. No files were changed. Check the app log.",
                priority=1,
            )
            st["last_notified_problem"] = key
            set_state(st)
        return False

    st.update(
        {
            "last_deployed_sha": bootstrap_sha,
            "last_success_at": int(time.time()),
            "initial_reconciled": True,
            "bootstrap_base_commit": bootstrap_sha,
            "last_notified_problem": None,
        }
    )
    set_state(st)
    publish_status(st)
    LOG.info(
        "Bootstrap reconciliation succeeded. Production is consistent with baseline %s "
        "and current target %s; continuing deployment.",
        bootstrap_sha,
        target_sha,
    )
    notify(
        options,
        "Git deployer baseline established",
        f"Production is consistent with historical baseline {bootstrap_sha[:12]} "
        f"and current target {target_sha[:12]}. Continuing deployment.",
    )
    return True


def _set_validation_state(
    *,
    result: str,
    errors: str,
    message: str,
    rollback_result: str | None = None,
) -> dict[str, Any]:
    now_ts = int(time.time())
    updates: dict[str, Any] = {
        "last_validation_at": now_ts,
        "last_validation_result": result,
        "last_validation_errors": errors,
        "last_status_message": message,
    }
    if rollback_result is not None:
        updates["last_rollback_at"] = 0
        updates["last_rollback_result"] = rollback_result
    return set_status_state(**updates)


def _record_rollback_result(result: str, message: str) -> dict[str, Any]:
    return set_status_state(
        last_rollback_at=int(time.time()),
        last_rollback_result=result,
        last_status_message=message,
    )


def deploy_once(options: dict[str, Any], new_sha: str) -> None:
    st = state()
    old_sha = str(st.get("last_deployed_sha") or "")

    if not old_sha:
        matches, problems = initial_reconcile(new_sha, new_sha)
        if matches:
            st.update(
                {
                    "last_deployed_sha": new_sha,
                    "last_success_at": int(time.time()),
                    "initial_reconciled": True,
                }
            )
            set_state(st)
            publish_status(st)
            LOG.info(
                "Initial reconciliation succeeded. Baseline commit is %s", new_sha
            )
            notify(
                options,
                "Git deployer ready",
                f"Production matches GitHub baseline {new_sha[:12]}. "
                "Monitoring main for new commits.",
            )
        else:
            LOG.error(
                "Initial reconciliation failed; no files were changed:\n- %s",
                "\n- ".join(problems),
            )
            marker = st.get("last_notified_problem")
            key = "initial:" + sha256_bytes("\n".join(problems).encode())
            if marker != key:
                notify(
                    options,
                    "Git deployer needs reconciliation",
                    "Initial production/GitHub comparison failed. No files were "
                    "changed. Check the app log.",
                    priority=1,
                )
                st["last_notified_problem"] = key
                set_state(st)
                publish_status(st)
        return

    if old_sha == new_sha:
        return

    if not is_ancestor(old_sha, new_sha):
        msg = (
            f"Refusing non-fast-forward deployment: "
            f"{old_sha[:12]} -> {new_sha[:12]}"
        )
        LOG.error(msg)
        if st.get("last_notified_problem") != msg:
            notify(options, "Git deploy blocked", msg, priority=1)
            st["last_notified_problem"] = msg
            set_state(st)
            publish_status(st)
        return

    changes = diff_name_status(old_sha, new_sha)
    allowed = [(status, path) for status, path in changes if classify_path(path) == "allowed"]
    forbidden = [
        (status, path)
        for status, path in changes
        if classify_path(path) == "forbidden"
    ]
    ignored = [(status, path) for status, path in changes if classify_path(path) == "ignored"]

    LOG.info(
        "New commit %s detected: %d allowed, %d ignored, %d forbidden changes",
        new_sha,
        len(allowed),
        len(ignored),
        len(forbidden),
    )

    changed_files = [path for _, path in allowed]
    if changed_files:
        set_status_state(
            last_attempted_sha=new_sha,
            last_attempted_files=changed_files,
            last_status_message=f"Preparing managed deployment {new_sha[:12]}.",
        )
        st = state()

    if forbidden:
        paths = ", ".join(path for _, path in forbidden)
        msg = f"Commit {new_sha[:12]} changes non-allowlisted path(s): {paths}"
        LOG.error(msg)
        if st.get("last_notified_problem") != msg:
            notify(options, "Git deploy blocked", msg, priority=1)
            st["last_notified_problem"] = msg
            set_state(st)
            publish_status(st)
        return

    if not allowed:
        st.update(
            {
                "last_deployed_sha": new_sha,
                "last_success_at": int(time.time()),
                "last_notified_problem": None,
            }
        )
        set_state(st)
        publish_status(st)
        LOG.info(
            "Commit %s contained repository-only changes; advanced baseline "
            "without touching Home Assistant",
            new_sha,
        )
        return

    sensitive = any(
        path == "configuration.yaml" or status_code == "D"
        for status_code, path in allowed
    )
    approved = str(options.get("approved_sensitive_commit", "")).strip()
    if sensitive and approved != new_sha:
        changed = ", ".join(f"{status}:{path}" for status, path in allowed)
        msg = (
            f"Commit {new_sha} requires explicit approval because it changes "
            f"configuration.yaml and/or deletes a managed file. Changes: {changed}"
        )
        LOG.warning(msg)
        if st.get("last_notified_problem") != msg:
            notify(
                options,
                "Git deploy approval required",
                f"Commit {new_sha[:12]} is pending. Paste the full SHA into "
                "approved_sensitive_commit to authorize this exact commit.",
            )
            st["last_notified_problem"] = msg
            set_state(st)
            publish_status(st)
        return

    drift = local_drift(allowed, old_sha, new_sha)
    if drift:
        msg = f"Local drift blocks commit {new_sha[:12]}: " + "; ".join(drift)
        LOG.error(msg)
        if st.get("last_notified_problem") != msg:
            notify(
                options,
                "Git deploy blocked by local drift",
                f"Commit {new_sha[:12]} was not deployed because local managed "
                "files differ from the last GitHub version. Check app logs.",
                priority=1,
            )
            st["last_notified_problem"] = msg
            set_state(st)
            publish_status(st)
        return

    backup_root = backup_changes(allowed, new_sha)
    validation_result = ""
    validation_errors = ""
    try:
        try:
            apply_changes(allowed, new_sha)
        except Exception as err:
            validation_result = "error"
            validation_errors = f"File deployment failed: {err}"
            _set_validation_state(
                result=validation_result,
                errors=validation_errors,
                message=f"Deployment {new_sha[:12]} failed while applying files.",
                rollback_result="pending",
            )
            raise

        try:
            valid, errors = check_config()
        except Exception as err:
            validation_result = "error"
            validation_errors = str(err)
            _set_validation_state(
                result=validation_result,
                errors=validation_errors,
                message=(
                    f"Deployment {new_sha[:12]} configuration check raised an error."
                ),
                rollback_result="pending",
            )
            raise

        validation_result = "valid" if valid else "invalid"
        validation_errors = errors
        _set_validation_state(
            result=validation_result,
            errors=validation_errors,
            message=(
                f"Deployment {new_sha[:12]} configuration validation "
                f"{'passed' if valid else 'failed'}."
            ),
            rollback_result="not_needed" if valid else "pending",
        )
        if not valid:
            raise DeployError(
                f"Home Assistant configuration check failed: {errors}"
            )
    except Exception as err:
        LOG.error("Deployment %s failed: %s", new_sha, err)
        rollback(backup_root)
        try:
            rollback_valid, rollback_errors = check_config()
            rollback_result = "valid" if rollback_valid else "invalid"
        except Exception as check_err:
            rollback_valid = False
            rollback_errors = str(check_err)
            rollback_result = "error"

        _record_rollback_result(
            rollback_result,
            (
                f"Rollback after {new_sha[:12]} configuration validation "
                f"{'passed' if rollback_valid else 'failed'}."
            ),
        )

        if rollback_valid:
            LOG.info("Rollback configuration check passed")
            notify(
                options,
                "Git deploy rolled back",
                f"Commit {new_sha[:12]} failed validation and was automatically "
                f"rolled back. Error: {err}",
                priority=1,
            )
        else:
            LOG.critical(
                "Rollback configuration check also failed: %s", rollback_errors
            )
            notify(
                options,
                "Git deploy CRITICAL",
                f"Commit {new_sha[:12]} failed and rollback validation also "
                f"failed. Manual intervention is required. Rollback error: "
                f"{rollback_errors}",
                priority=1,
            )
        return

    st = state()
    deployed_at = int(time.time())
    st.update(
        {
            "last_deployed_sha": new_sha,
            "last_success_at": deployed_at,
            "last_notified_problem": None,
            "last_code_deploy_at": deployed_at,
            "last_code_deploy_sha": new_sha,
            "last_changed_files": changed_files,
            "restart_required": restart_required_for_paths(changed_files),
            "last_rollback_at": 0,
            "last_rollback_result": "not_needed",
            "last_status_message": (
                f"Deployment {new_sha[:12]} passed configuration validation."
            ),
        }
    )
    set_state(st)
    publish_status(st)
    prune_rollbacks()
    changed_names = ", ".join(changed_files)
    LOG.info(
        "Deployment %s passed Home Assistant configuration validation", new_sha
    )
    notify(
        options,
        "Git deploy validated",
        f"Commit {new_sha[:12]} deployed and passed configuration validation. "
        f"Activation/restart is still manual. Files: {changed_names}",
    )


def poll_target(options: dict[str, Any]) -> str | None:
    repository = str(options["repository"])
    branch = str(options["branch"])
    poll_at = int(time.time())
    set_status_state(last_poll_at=poll_at)

    public_key = ""
    try:
        public_key = ensure_ssh_material()
        target_sha = ensure_repository(repository, branch)
    except subprocess.CalledProcessError as err:
        LOG.error(
            "Git access failed. Add this public key to the repository as a "
            "READ-ONLY deploy key:\n%s",
            public_key or "(public key unavailable)",
        )
        stderr = (err.stderr or "").strip()
        if stderr:
            LOG.error("Git error: %s", stderr)
        record_fetch_failure(options, stderr or err, poll_at)
        return None
    except Exception as err:
        LOG.error("Git repository poll failed: %s", err)
        record_fetch_failure(options, err, poll_at)
        return None

    record_fetch_success(options, int(time.time()))
    return target_sha


def main() -> int:
    publish_status()
    LOG.info("Home Automation Git Deployer %s starting", VERSION)

    while True:
        try:
            options = load_options()
            target_sha = poll_target(options)
            if target_sha and bootstrap_if_needed(options, target_sha):
                deploy_once(options, target_sha)
            delay = max(60, int(options.get("poll_seconds", 300)))
        except KeyboardInterrupt:
            return 0
        except Exception:
            LOG.exception("Unhandled deployer error")
            try:
                publish_status()
            except Exception:
                LOG.exception("Unable to publish deployer status")
            delay = 60

        time.sleep(delay)


if __name__ == "__main__":
    sys.exit(main())
