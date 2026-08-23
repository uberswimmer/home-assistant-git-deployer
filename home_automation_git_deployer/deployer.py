#!/usr/bin/env python3
from __future__ import annotations

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

DATA_DIR = Path("/data")
HA_DIR = Path("/homeassistant")
REPO_DIR = DATA_DIR / "repository"
STATE_FILE = DATA_DIR / "state.json"
SSH_DIR = DATA_DIR / "ssh"
SSH_KEY = SSH_DIR / "id_ed25519"
KNOWN_HOSTS = SSH_DIR / "known_hosts"
ROLLBACK_ROOT = DATA_DIR / "rollback"
OPTIONS_FILE = DATA_DIR / "options.json"

ROOT_ALLOWED = {
    "configuration.yaml",
    "automations.yaml",
    "scripts.yaml",
    "scenes.yaml",
    "comfort_dashboard.yaml",
    "iaq_dashboard.yaml",
    "security_dashboard.yaml",
}
IGNORED_PREFIXES = ("docs/", "hubitat/", "local_apps/")
IGNORED_EXACT = {"README.md", ".gitignore"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
LOG = logging.getLogger("git-deployer")


class DeployError(RuntimeError):
    pass


def run(cmd: list[str], *, cwd: Path | None = None, check: bool = True,
        capture: bool = True, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
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
    temp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temp, path)


def load_options() -> dict[str, Any]:
    options = load_json(OPTIONS_FILE, {})
    required = {
        "repository", "branch", "poll_seconds", "pushover_service",
        "approved_sensitive_commit"
    }
    missing = sorted(required - options.keys())
    if missing:
        raise DeployError(f"Missing app option(s): {', '.join(missing)}")
    return options


def state() -> dict[str, Any]:
    return load_json(STATE_FILE, {})


def set_state(data: dict[str, Any]) -> None:
    save_json_atomic(STATE_FILE, data)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def classify_path(path: str) -> str:
    p = PurePosixPath(path)
    if path in ROOT_ALLOWED:
        return "allowed"
    if len(p.parts) == 1 and p.suffix == ".yaml" and p.name.endswith("_dashboard.yaml"):
        return "allowed"
    if len(p.parts) == 2 and p.parts[0] in {"packages", "themes"} and p.suffix == ".yaml":
        return "allowed"
    if path in IGNORED_EXACT or any(path.startswith(prefix) for prefix in IGNORED_PREFIXES):
        return "ignored"
    return "forbidden"


def managed_local_paths() -> set[str]:
    paths: set[str] = set()
    for name in ROOT_ALLOWED:
        if (HA_DIR / name).exists():
            paths.add(name)
    for p in HA_DIR.glob("*_dashboard.yaml"):
        if p.is_file():
            paths.add(p.name)
    for folder in ("packages", "themes"):
        base = HA_DIR / folder
        if base.exists():
            for p in base.glob("*.yaml"):
                if p.is_file():
                    paths.add(f"{folder}/{p.name}")
    return paths


def ensure_ssh_material() -> str:
    SSH_DIR.mkdir(parents=True, exist_ok=True)
    if not SSH_KEY.exists():
        run([
            "ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C",
            "home-assistant-git-deployer", "-f", str(SSH_KEY)
        ])
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
        run(["git", "clone", "--no-checkout", repository, str(REPO_DIR)], env=git_env())
    elif not (REPO_DIR / ".git").exists():
        raise DeployError(f"{REPO_DIR} exists but is not a Git repository")

    current_origin = run(
        ["git", "remote", "get-url", "origin"], cwd=REPO_DIR
    ).stdout.strip()
    if current_origin != repository:
        run(["git", "remote", "set-url", "origin", repository], cwd=REPO_DIR)

    run(["git", "fetch", "--prune", "origin", branch], cwd=REPO_DIR, env=git_env())
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


def initial_reconcile(commit: str) -> tuple[bool, list[str]]:
    repo_paths = git_allowed_paths(commit)
    local_paths = managed_local_paths()
    problems: list[str] = []

    for path in sorted(repo_paths | local_paths):
        if path not in repo_paths:
            problems.append(f"local-only managed file: {path}")
            continue
        if path not in local_paths:
            problems.append(f"missing locally: {path}")
            continue
        expected = git_file_bytes(commit, path)
        actual = (HA_DIR / path).read_bytes()
        if expected is None or expected != actual:
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


def local_drift(changes: list[tuple[str, str]], old: str) -> list[str]:
    problems: list[str] = []
    for _, path in changes:
        if classify_path(path) != "allowed":
            continue
        local = HA_DIR / path
        old_data = git_file_bytes(old, path)
        if old_data is None:
            if local.exists():
                problems.append(
                    f"{path}: exists locally but did not exist in last deployed commit"
                )
            continue
        if not local.exists():
            problems.append(f"{path}: missing locally")
            continue
        if local.read_bytes() != old_data:
            problems.append(
                f"{path}: local content differs from last deployed commit"
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


def prune_rollbacks(keep: int = 5) -> None:
    if not ROLLBACK_ROOT.exists():
        return
    dirs = sorted(
        (p for p in ROLLBACK_ROOT.iterdir() if p.is_dir()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for old in dirs[keep:]:
        shutil.rmtree(old, ignore_errors=True)


def deploy_once(options: dict[str, Any]) -> None:
    public_key = ensure_ssh_material()
    repository = str(options["repository"])
    branch = str(options["branch"])

    try:
        new_sha = ensure_repository(repository, branch)
    except subprocess.CalledProcessError as err:
        LOG.error(
            "Git access failed. Add this public key to the repository as a "
            "READ-ONLY deploy key:\n%s",
            public_key,
        )
        stderr = (err.stderr or "").strip()
        if stderr:
            LOG.error("Git error: %s", stderr)
        return

    st = state()
    old_sha = st.get("last_deployed_sha")

    if not old_sha:
        matches, problems = initial_reconcile(new_sha)
        if matches:
            st.update(
                {
                    "last_deployed_sha": new_sha,
                    "last_success_at": int(time.time()),
                    "initial_reconciled": True,
                }
            )
            set_state(st)
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
        return

    changes = diff_name_status(old_sha, new_sha)
    allowed = [(s, p) for s, p in changes if classify_path(p) == "allowed"]
    forbidden = [
        (s, p) for s, p in changes if classify_path(p) == "forbidden"
    ]
    ignored = [(s, p) for s, p in changes if classify_path(p) == "ignored"]

    LOG.info(
        "New commit %s detected: %d allowed, %d ignored, %d forbidden changes",
        new_sha,
        len(allowed),
        len(ignored),
        len(forbidden),
    )

    if forbidden:
        paths = ", ".join(p for _, p in forbidden)
        msg = (
            f"Commit {new_sha[:12]} changes non-allowlisted path(s): {paths}"
        )
        LOG.error(msg)
        if st.get("last_notified_problem") != msg:
            notify(options, "Git deploy blocked", msg, priority=1)
            st["last_notified_problem"] = msg
            set_state(st)
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
        changed = ", ".join(f"{s}:{p}" for s, p in allowed)
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
        return

    drift = local_drift(allowed, old_sha)
    if drift:
        msg = (
            f"Local drift blocks commit {new_sha[:12]}: " + "; ".join(drift)
        )
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
        return

    backup_root = backup_changes(allowed, new_sha)
    try:
        apply_changes(allowed, new_sha)
        valid, errors = check_config()
        if not valid:
            raise DeployError(
                f"Home Assistant configuration check failed: {errors}"
            )
    except Exception as err:
        LOG.error("Deployment %s failed: %s", new_sha, err)
        rollback(backup_root)
        try:
            rollback_valid, rollback_errors = check_config()
        except Exception as check_err:
            rollback_valid, rollback_errors = False, str(check_err)
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

    st.update(
        {
            "last_deployed_sha": new_sha,
            "last_success_at": int(time.time()),
            "last_notified_problem": None,
        }
    )
    set_state(st)
    prune_rollbacks()
    changed_names = ", ".join(path for _, path in allowed)
    LOG.info(
        "Deployment %s passed Home Assistant configuration validation", new_sha
    )
    notify(
        options,
        "Git deploy validated",
        f"Commit {new_sha[:12]} deployed and passed configuration validation. "
        f"Activation/restart is still manual. Files: {changed_names}",
    )


def main() -> int:
    LOG.info("Home Automation Git Deployer 1.0.0 starting")
    while True:
        try:
            options = load_options()
            deploy_once(options)
            delay = max(60, int(options.get("poll_seconds", 300)))
        except KeyboardInterrupt:
            return 0
        except Exception:
            LOG.exception("Unhandled deployer error")
            delay = 60
        time.sleep(delay)


if __name__ == "__main__":
    sys.exit(main())
