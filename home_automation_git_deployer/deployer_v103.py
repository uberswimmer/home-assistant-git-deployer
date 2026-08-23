#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import subprocess
import sys
import time
from typing import Any

import deployer as base

EMPTY_YAML_REPRESENTATIONS = {b"", b"[]", b"{}", b"null", b"~"}
EMPTY_UI_FILES = {"automations.yaml", "scripts.yaml", "scenes.yaml"}
TARGET_SHA: str | None = None


def content_matches(path: str, expected: bytes, actual: bytes) -> bool:
    if expected == actual:
        return True
    if path in EMPTY_UI_FILES:
        return (
            expected.strip() in EMPTY_YAML_REPRESENTATIONS
            and actual.strip() in EMPTY_YAML_REPRESENTATIONS
        )
    return False


def target_file_bytes(path: str) -> bytes | None:
    if not TARGET_SHA:
        return None
    return base.git_file_bytes(TARGET_SHA, path)


def initial_reconcile(commit: str) -> tuple[bool, list[str]]:
    repo_paths = base.git_allowed_paths(commit)
    local_paths = base.managed_local_paths()
    problems: list[str] = []

    for path in sorted(repo_paths | local_paths):
        local = base.HA_DIR / path
        old_data = base.git_file_bytes(commit, path)
        target_data = target_file_bytes(path)

        if path not in repo_paths:
            if local.exists() and target_data is not None and content_matches(
                path, target_data, local.read_bytes()
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


def local_drift(changes: list[tuple[str, str]], old: str) -> list[str]:
    problems: list[str] = []

    for status_code, path in changes:
        if base.classify_path(path) != "allowed":
            continue

        local = base.HA_DIR / path
        old_data = base.git_file_bytes(old, path)
        target_data = target_file_bytes(path)

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


def bootstrap_if_needed(options: dict[str, Any]) -> bool:
    global TARGET_SHA

    st = base.state()
    if st.get("last_deployed_sha"):
        return True

    bootstrap_sha = str(options.get("bootstrap_base_commit", "")).strip()
    if not bootstrap_sha:
        return True

    repository = str(options["repository"])
    branch = str(options["branch"])

    try:
        base.ensure_ssh_material()
        TARGET_SHA = base.ensure_repository(repository, branch)
    except subprocess.CalledProcessError:
        return False

    exists = subprocess.run(
        ["git", "cat-file", "-e", f"{bootstrap_sha}^{{commit}}"],
        cwd=str(base.REPO_DIR),
        check=False,
        capture_output=True,
    )
    if exists.returncode != 0:
        msg = f"Bootstrap commit does not exist locally: {bootstrap_sha}"
        base.LOG.error(msg)
        marker = "bootstrap-missing:" + bootstrap_sha
        if st.get("last_notified_problem") != marker:
            base.notify(options, "Git deploy bootstrap blocked", msg, priority=1)
            st["last_notified_problem"] = marker
            base.set_state(st)
        return False

    if not base.is_ancestor(bootstrap_sha, TARGET_SHA):
        msg = (
            f"Bootstrap commit {bootstrap_sha[:12]} is not an ancestor of "
            f"current {branch} {TARGET_SHA[:12]}"
        )
        base.LOG.error(msg)
        marker = "bootstrap-ancestor:" + bootstrap_sha + ":" + TARGET_SHA
        if st.get("last_notified_problem") != marker:
            base.notify(options, "Git deploy bootstrap blocked", msg, priority=1)
            st["last_notified_problem"] = marker
            base.set_state(st)
        return False

    matches, problems = initial_reconcile(bootstrap_sha)
    if not matches:
        base.LOG.error(
            "Bootstrap reconciliation against %s failed; no files were changed:\n- %s",
            bootstrap_sha,
            "\n- ".join(problems),
        )
        key = "bootstrap:" + hashlib.sha256(
            (bootstrap_sha + "\n" + "\n".join(problems)).encode()
        ).hexdigest()
        if st.get("last_notified_problem") != key:
            base.notify(
                options,
                "Git deployer needs reconciliation",
                f"Production does not match bootstrap commit {bootstrap_sha[:12]} "
                "or current target files. No files were changed. Check the app log.",
                priority=1,
            )
            st["last_notified_problem"] = key
            base.set_state(st)
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
    base.set_state(st)
    base.LOG.info(
        "Bootstrap reconciliation succeeded. Production is consistent with baseline %s "
        "and current target %s; continuing deployment.",
        bootstrap_sha,
        TARGET_SHA,
    )
    base.notify(
        options,
        "Git deployer baseline established",
        f"Production is consistent with historical baseline {bootstrap_sha[:12]} "
        f"and current target {TARGET_SHA[:12]}. Continuing deployment.",
    )
    return True


def main() -> int:
    global TARGET_SHA

    base.initial_reconcile = initial_reconcile
    base.local_drift = local_drift

    base.LOG.info("Home Automation Git Deployer 1.0.3 starting")
    while True:
        try:
            options = base.load_options()
            repository = str(options["repository"])
            branch = str(options["branch"])
            try:
                base.ensure_ssh_material()
                TARGET_SHA = base.ensure_repository(repository, branch)
            except subprocess.CalledProcessError:
                TARGET_SHA = None

            if bootstrap_if_needed(options):
                base.deploy_once(options)
            delay = max(60, int(options.get("poll_seconds", 300)))
        except KeyboardInterrupt:
            return 0
        except Exception:
            base.LOG.exception("Unhandled deployer error")
            delay = 60
        time.sleep(delay)


if __name__ == "__main__":
    sys.exit(main())
