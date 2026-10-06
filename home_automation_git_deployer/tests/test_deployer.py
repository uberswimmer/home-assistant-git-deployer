# =============================================================================
# HOME AUTOMATION GIT DEPLOYER REGRESSION TESTS
# =============================================================================
# Version history:
# 1.4.0 - 2026-10-06 - Cover exact DROP integration paths, drift tracking and sensitive approvals.
# 1.3.0 - 2026-09-07 - Covered exact AGENTS.md metadata classification and retry of a blocked mixed configuration commit after an app upgrade.
# 1.2.0 - 2026-08-29 - Added regression coverage for notification delivery results, pending deduplication, bounded retry, and Home Assistant fallback telemetry.
# 1.1.0 - 2026-08-29 - Added regression coverage for latched restart requirements and rollback filesystem exceptions, including manifest-read, copy, and unlink failures.
# 1.0.0 - 2026-08-24 - Added regression coverage for the v1.3.0 flattened deployer safety invariants, rollback behavior, reconciliation, and repository-health alerting.
# =============================================================================

from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import tempfile
import types
import unittest
from unittest import mock

MODULE_PATH = Path(__file__).resolve().parents[1] / "deployer.py"
spec = importlib.util.spec_from_file_location("git_deployer_under_test", MODULE_PATH)
assert spec and spec.loader
deployer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deployer)


class GitDeployerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.data_dir = root / "data"
        self.ha_dir = root / "homeassistant"
        self.repo_dir = self.data_dir / "repository"
        self.rollback_root = self.data_dir / "rollback"
        self.ha_dir.mkdir(parents=True)
        self.data_dir.mkdir(parents=True)

        self.originals = {
            "DATA_DIR": deployer.DATA_DIR,
            "HA_DIR": deployer.HA_DIR,
            "REPO_DIR": deployer.REPO_DIR,
            "STATE_FILE": deployer.STATE_FILE,
            "SSH_DIR": deployer.SSH_DIR,
            "SSH_KEY": deployer.SSH_KEY,
            "KNOWN_HOSTS": deployer.KNOWN_HOSTS,
            "ROLLBACK_ROOT": deployer.ROLLBACK_ROOT,
            "OPTIONS_FILE": deployer.OPTIONS_FILE,
            "STATUS_FILE": deployer.STATUS_FILE,
        }
        deployer.DATA_DIR = self.data_dir
        deployer.HA_DIR = self.ha_dir
        deployer.REPO_DIR = self.repo_dir
        deployer.STATE_FILE = self.data_dir / "state.json"
        deployer.SSH_DIR = self.data_dir / "ssh"
        deployer.SSH_KEY = deployer.SSH_DIR / "id_ed25519"
        deployer.KNOWN_HOSTS = deployer.SSH_DIR / "known_hosts"
        deployer.ROLLBACK_ROOT = self.rollback_root
        deployer.OPTIONS_FILE = self.data_dir / "options.json"
        deployer.STATUS_FILE = self.ha_dir / ".git_deployer_status.json"

        self.options = {
            "repository": "git@github.com:example/config.git",
            "branch": "main",
            "poll_seconds": 300,
            "pushover_service": "notify.pushover",
            "approved_sensitive_commit": "",
            "bootstrap_base_commit": "",
        }

    def tearDown(self) -> None:
        for name, value in self.originals.items():
            setattr(deployer, name, value)
        self.temp.cleanup()

    def seed_state(self, **updates: object) -> None:
        state = {"last_deployed_sha": "oldsha"}
        state.update(updates)
        deployer.set_state(state)

    def test_path_classification_preserves_allowlist_and_ignored_metadata(self) -> None:
        self.assertEqual(deployer.classify_path("configuration.yaml"), "allowed")
        self.assertEqual(deployer.classify_path("packages/iaq.yaml"), "allowed")
        self.assertEqual(
            deployer.classify_path("dashboard_components/card.yaml"), "allowed"
        )
        self.assertEqual(deployer.classify_path(".github/workflows/test.yml"), "ignored")
        self.assertEqual(deployer.classify_path("local_apps/tool/file.py"), "ignored")
        self.assertEqual(deployer.classify_path("secrets.yaml"), "forbidden")
        self.assertEqual(deployer.classify_path("AGENTS.md"), "ignored")
        for path in ("agents.md", "OTHER.md", "packages/AGENTS.md", "../AGENTS.md"):
            with self.subTest(path=path):
                self.assertEqual(deployer.classify_path(path), "forbidden")

    def test_drop_adapter_exact_allowlist_and_local_tracking(self):
        for path in deployer.DROP_OBSERVATION_FILES:
            self.assertEqual(deployer.classify_path(path), 'allowed')
            f = deployer.HA_DIR / path
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text('test')
            self.assertIn(path, deployer.managed_local_paths())
            self.assertTrue(deployer.restart_required_for_paths([path]))
        for path in ['custom_components/other/sensor.py',
                     'custom_components/drop_observation/secrets.yaml',
                     'custom_components/drop_observation/new.py',
                     'custom_components/drop_observation/../sensor.py',
                     'custom_components/drop_observation/__pycache__/sensor.pyc']:
            self.assertEqual(deployer.classify_path(path), 'forbidden')

    def test_empty_ui_yaml_representations_are_equivalent(self) -> None:
        self.assertTrue(deployer.content_matches("automations.yaml", b"[]\n", b"\n"))
        self.assertTrue(deployer.content_matches("scenes.yaml", b"{}", b"null\n"))
        self.assertFalse(
            deployer.content_matches("packages/a.yaml", b"[]\n", b"\n")
        )
        self.assertFalse(
            deployer.content_matches("scripts.yaml", b"- alias: x\n", b"[]\n")
        )

    def test_restart_required_ignores_dashboard_theme_and_component_only_changes(self) -> None:
        self.assertFalse(
            deployer.restart_required_for_paths(
                [
                    "comfort_dashboard.yaml",
                    "themes/home.yaml",
                    "dashboard_components/card.yaml",
                ]
            )
        )
        self.assertTrue(
            deployer.restart_required_for_paths(["packages/security.yaml"])
        )

    def test_initial_reconcile_accepts_file_already_matching_target(self) -> None:
        local = self.ha_dir / "packages" / "a.yaml"
        local.parent.mkdir(parents=True)
        local.write_bytes(b"new")

        def file_bytes(commit: str, path: str) -> bytes | None:
            self.assertEqual(path, "packages/a.yaml")
            return b"old" if commit == "baseline" else b"new"

        with (
            mock.patch.object(deployer, "git_allowed_paths", return_value={"packages/a.yaml"}),
            mock.patch.object(deployer, "managed_local_paths", return_value={"packages/a.yaml"}),
            mock.patch.object(deployer, "git_file_bytes", side_effect=file_bytes),
        ):
            matches, problems = deployer.initial_reconcile("baseline", "target")

        self.assertTrue(matches)
        self.assertEqual(problems, [])

    def test_local_drift_accepts_file_already_matching_target(self) -> None:
        local = self.ha_dir / "packages" / "a.yaml"
        local.parent.mkdir(parents=True)
        local.write_bytes(b"new")

        def file_bytes(commit: str, path: str) -> bytes | None:
            return b"old" if commit == "baseline" else b"new"

        with mock.patch.object(deployer, "git_file_bytes", side_effect=file_bytes):
            problems = deployer.local_drift(
                [("M", "packages/a.yaml")], "baseline", "target"
            )
        self.assertEqual(problems, [])

    def test_non_fast_forward_is_refused(self) -> None:
        self.seed_state()
        with (
            mock.patch.object(deployer, "is_ancestor", return_value=False),
            mock.patch.object(deployer, "diff_name_status") as diff_mock,
            mock.patch.object(deployer, "notify") as notify_mock,
        ):
            deployer.deploy_once(self.options, "newsha")

        diff_mock.assert_not_called()
        self.assertEqual(deployer.state()["last_deployed_sha"], "oldsha")
        self.assertEqual(notify_mock.call_args.args[1], "Git deploy blocked")

    def test_sensitive_change_requires_exact_target_sha(self) -> None:
        self.seed_state()
        changes = [("M", "configuration.yaml")]
        with (
            mock.patch.object(deployer, "is_ancestor", return_value=True),
            mock.patch.object(deployer, "diff_name_status", return_value=changes),
            mock.patch.object(deployer, "apply_changes") as apply_mock,
            mock.patch.object(deployer, "notify") as notify_mock,
        ):
            deployer.deploy_once(self.options, "newsha")

        apply_mock.assert_not_called()
        self.assertEqual(deployer.state()["last_deployed_sha"], "oldsha")
        self.assertEqual(notify_mock.call_args.args[1], "Git deploy approval required")

    def test_drop_adapter_change_requires_exact_sha_approval(self):
        for path in deployer.DROP_OBSERVATION_FILES:
            self.seed_state()
            with (
                mock.patch.object(deployer, "is_ancestor", return_value=True),
                mock.patch.object(deployer, "diff_name_status", return_value=[("M", path)]),
                mock.patch.object(deployer, "apply_changes") as apply_mock,
                mock.patch.object(deployer, "notify") as notify_mock,
            ):
                deployer.deploy_once(self.options, "newsha")
            apply_mock.assert_not_called()
            self.assertEqual(deployer.state()["last_deployed_sha"], "oldsha")
            self.assertEqual(notify_mock.call_args.args[1], "Git deploy approval required")

    def test_local_drift_blocks_managed_deployment(self) -> None:
        self.seed_state()
        changes = [("M", "packages/a.yaml")]
        with (
            mock.patch.object(deployer, "is_ancestor", return_value=True),
            mock.patch.object(deployer, "diff_name_status", return_value=changes),
            mock.patch.object(deployer, "local_drift", return_value=["packages/a.yaml: drift"]),
            mock.patch.object(deployer, "apply_changes") as apply_mock,
            mock.patch.object(deployer, "notify") as notify_mock,
        ):
            deployer.deploy_once(self.options, "newsha")

        apply_mock.assert_not_called()
        self.assertEqual(deployer.state()["last_deployed_sha"], "oldsha")
        self.assertEqual(notify_mock.call_args.args[1], "Git deploy blocked by local drift")

    def test_repository_only_commit_advances_baseline_without_config_check(self) -> None:
        self.seed_state(last_code_deploy_sha="previous-code")
        changes = [("M", "docs/readme.md"), ("M", "AGENTS.md")]
        with (
            mock.patch.object(deployer, "is_ancestor", return_value=True),
            mock.patch.object(deployer, "diff_name_status", return_value=changes),
            mock.patch.object(deployer, "check_config") as check_mock,
        ):
            deployer.deploy_once(self.options, "newsha")

        check_mock.assert_not_called()
        state = deployer.state()
        self.assertEqual(state["last_deployed_sha"], "newsha")
        self.assertEqual(state["last_code_deploy_sha"], "previous-code")

    def test_app_upgrade_retries_blocked_agents_commit_without_copying_metadata(self) -> None:
        self.seed_state()
        target = self.ha_dir / "packages" / "a.yaml"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"old\n")
        changes = [("A", "AGENTS.md"), ("M", "packages/a.yaml")]

        def file_bytes(commit: str, path: str) -> bytes:
            self.assertEqual(path, "packages/a.yaml")
            return b"old\n" if commit == "oldsha" else b"new\n"

        with (
            mock.patch.object(deployer, "is_ancestor", return_value=True),
            mock.patch.object(deployer, "diff_name_status", return_value=changes),
            mock.patch.object(deployer, "git_file_bytes", side_effect=file_bytes),
            mock.patch.object(deployer, "check_config", return_value=(True, "")) as check_mock,
            mock.patch.object(deployer, "notify", return_value=True),
        ):
            # Reproduce the installed pre-upgrade policy, then retry the same
            # commit with the updated policy and the persisted blocked state.
            with mock.patch.object(deployer, "IGNORED_EXACT", {"README.md", ".gitignore"}):
                deployer.deploy_once(self.options, "newsha")
            self.assertEqual(deployer.state()["last_deployed_sha"], "oldsha")
            self.assertEqual(target.read_bytes(), b"old\n")
            self.assertFalse(self.rollback_root.exists())
            check_mock.assert_not_called()

            deployer.deploy_once(self.options, "newsha")

        check_mock.assert_called_once()
        self.assertEqual(target.read_bytes(), b"new\n")
        self.assertFalse((self.ha_dir / "AGENTS.md").exists())
        state = deployer.state()
        self.assertEqual(state["last_deployed_sha"], "newsha")
        self.assertEqual(state["last_code_deploy_sha"], "newsha")
        self.assertEqual(state["last_validation_result"], "valid")
        self.assertTrue(state["restart_required"])
        self.assertFalse(state["notification_pending"])

    def test_delete_apply_and_rollback_restore_original_file(self) -> None:
        target = self.ha_dir / "packages" / "a.yaml"
        target.parent.mkdir(parents=True)
        target.write_text("old\n", encoding="utf-8")
        changes = [("D", "packages/a.yaml")]
        backup = deployer.backup_changes(changes, "newsha")
        deployer.apply_changes(changes, "newsha")
        self.assertFalse(target.exists())
        deployer.rollback(backup)
        self.assertEqual(target.read_text(encoding="utf-8"), "old\n")

    def test_rollback_manifest_read_failure_is_raised(self) -> None:
        backup = self.rollback_root / "newsha"
        backup.mkdir(parents=True)
        (backup / "manifest.json").write_text("not json\n", encoding="utf-8")

        with self.assertRaises(deployer.DeployError):
            deployer.rollback(backup)

    def test_rollback_copy_failure_is_raised(self) -> None:
        backup = self.rollback_root / "newsha"
        source = backup / "files" / "packages" / "a.yaml"
        source.parent.mkdir(parents=True)
        source.write_text("old\n", encoding="utf-8")
        (backup / "manifest.json").write_text(
            '{"files": {"packages/a.yaml": {"existed": true}}}\n',
            encoding="utf-8",
        )

        with (
            mock.patch.object(
                deployer.shutil,
                "copy2",
                side_effect=OSError("filesystem read-only"),
            ),
            self.assertRaises(OSError),
        ):
            deployer.rollback(backup)

    def test_rollback_unlink_failure_is_raised(self) -> None:
        backup = self.rollback_root / "newsha"
        backup.mkdir(parents=True)
        (backup / "manifest.json").write_text(
            '{"files": {"packages/a.yaml": {"existed": false}}}\n',
            encoding="utf-8",
        )
        target = self.ha_dir / "packages" / "a.yaml"
        target.parent.mkdir(parents=True)
        target.write_text("new\n", encoding="utf-8")

        with (
            mock.patch.object(
                Path,
                "unlink",
                side_effect=OSError("filesystem read-only"),
            ),
            self.assertRaises(OSError),
        ):
            deployer.rollback(backup)

    def test_failed_validation_rolls_back_and_records_valid_rollback(self) -> None:
        self.seed_state()
        changes = [("M", "packages/a.yaml")]
        backup = self.rollback_root / "newsha"
        backup.mkdir(parents=True)
        (backup / "manifest.json").write_text('{"files": {}}\n', encoding="utf-8")

        with (
            mock.patch.object(deployer, "is_ancestor", return_value=True),
            mock.patch.object(deployer, "diff_name_status", return_value=changes),
            mock.patch.object(deployer, "local_drift", return_value=[]),
            mock.patch.object(deployer, "backup_changes", return_value=backup),
            mock.patch.object(deployer, "apply_changes"),
            mock.patch.object(deployer, "check_config", side_effect=[(False, "bad yaml"), (True, "")]),
            mock.patch.object(deployer, "rollback") as rollback_mock,
            mock.patch.object(deployer, "notify") as notify_mock,
        ):
            deployer.deploy_once(self.options, "newsha")

        rollback_mock.assert_called_once_with(backup)
        state = deployer.state()
        self.assertEqual(state["last_deployed_sha"], "oldsha")
        self.assertEqual(state["last_validation_result"], "invalid")
        self.assertEqual(state["last_rollback_result"], "valid")
        self.assertEqual(notify_mock.call_args.args[1], "Git deploy rolled back")

    def test_failed_rollback_escalates_critical_notification(self) -> None:
        self.seed_state()
        changes = [("M", "packages/a.yaml")]
        backup = self.rollback_root / "newsha"
        backup.mkdir(parents=True)
        (backup / "manifest.json").write_text('{"files": {}}\n', encoding="utf-8")

        with (
            mock.patch.object(deployer, "is_ancestor", return_value=True),
            mock.patch.object(deployer, "diff_name_status", return_value=changes),
            mock.patch.object(deployer, "local_drift", return_value=[]),
            mock.patch.object(deployer, "backup_changes", return_value=backup),
            mock.patch.object(deployer, "apply_changes"),
            mock.patch.object(deployer, "check_config", side_effect=[(False, "bad"), (False, "still bad")]),
            mock.patch.object(deployer, "rollback"),
            mock.patch.object(deployer, "notify") as notify_mock,
            mock.patch.object(deployer, "persistent_notification") as persistent_mock,
        ):
            deployer.deploy_once(self.options, "newsha")

        self.assertEqual(deployer.state()["last_rollback_result"], "invalid")
        self.assertEqual(deployer.state()["last_rollback_errors"], "still bad")
        self.assertEqual(notify_mock.call_args.args[1], "Git deploy CRITICAL")
        persistent_mock.assert_called_once()

    def test_rollback_exception_records_error_and_escalates(self) -> None:
        self.seed_state()
        changes = [("M", "packages/a.yaml")]
        backup = self.rollback_root / "newsha"
        backup.mkdir(parents=True)

        with (
            mock.patch.object(deployer, "is_ancestor", return_value=True),
            mock.patch.object(deployer, "diff_name_status", return_value=changes),
            mock.patch.object(deployer, "local_drift", return_value=[]),
            mock.patch.object(deployer, "backup_changes", return_value=backup),
            mock.patch.object(deployer, "apply_changes"),
            mock.patch.object(deployer, "check_config", return_value=(False, "bad yaml")) as check_mock,
            mock.patch.object(
                deployer,
                "rollback",
                side_effect=OSError("filesystem read-only"),
            ),
            mock.patch.object(deployer, "notify") as notify_mock,
            mock.patch.object(deployer, "persistent_notification") as persistent_mock,
        ):
            deployer.deploy_once(self.options, "newsha")

        state = deployer.state()
        self.assertEqual(state["last_deployed_sha"], "oldsha")
        self.assertEqual(state["last_validation_result"], "invalid")
        self.assertEqual(state["last_validation_errors"], "bad yaml")
        self.assertEqual(state["last_rollback_result"], "error")
        self.assertIn("filesystem read-only", state["last_rollback_errors"])
        self.assertEqual(check_mock.call_count, 1)
        self.assertEqual(notify_mock.call_args.args[1], "Git deploy CRITICAL")
        persistent_mock.assert_called_once()

    def test_successful_managed_deployment_records_code_status(self) -> None:
        self.seed_state()
        changes = [("M", "packages/a.yaml")]
        backup = self.rollback_root / "newsha"
        backup.mkdir(parents=True)
        (backup / "manifest.json").write_text('{"files": {}}\n', encoding="utf-8")

        with (
            mock.patch.object(deployer, "is_ancestor", return_value=True),
            mock.patch.object(deployer, "diff_name_status", return_value=changes),
            mock.patch.object(deployer, "local_drift", return_value=[]),
            mock.patch.object(deployer, "backup_changes", return_value=backup),
            mock.patch.object(deployer, "apply_changes"),
            mock.patch.object(deployer, "check_config", return_value=(True, "")),
            mock.patch.object(deployer, "prune_rollbacks"),
            mock.patch.object(deployer, "notify"),
        ):
            deployer.deploy_once(self.options, "newsha")

        state = deployer.state()
        self.assertEqual(state["last_deployed_sha"], "newsha")
        self.assertEqual(state["last_code_deploy_sha"], "newsha")
        self.assertEqual(state["last_validation_result"], "valid")
        self.assertTrue(state["restart_required"])
        self.assertGreater(state["last_restart_required_deploy_at"], 0)
        self.assertEqual(state["last_restart_required_deploy_sha"], "newsha")

    def test_dashboard_only_deployment_migrates_and_preserves_restart_latch(self) -> None:
        self.seed_state(
            restart_required=True,
            last_code_deploy_at=123,
            last_code_deploy_sha="package-sha",
        )
        changes = [("M", "iaq_dashboard.yaml")]
        backup = self.rollback_root / "newsha"
        backup.mkdir(parents=True)

        with (
            mock.patch.object(deployer, "is_ancestor", return_value=True),
            mock.patch.object(deployer, "diff_name_status", return_value=changes),
            mock.patch.object(deployer, "local_drift", return_value=[]),
            mock.patch.object(deployer, "backup_changes", return_value=backup),
            mock.patch.object(deployer, "apply_changes"),
            mock.patch.object(deployer, "check_config", return_value=(True, "")),
            mock.patch.object(deployer, "prune_rollbacks"),
            mock.patch.object(deployer, "notify"),
        ):
            deployer.deploy_once(self.options, "newsha")

        state = deployer.state()
        self.assertEqual(state["last_code_deploy_sha"], "newsha")
        self.assertTrue(state["restart_required"])
        self.assertEqual(state["last_restart_required_deploy_at"], 123)
        self.assertEqual(state["last_restart_required_deploy_sha"], "package-sha")

    def test_bootstrap_success_establishes_historical_baseline(self) -> None:
        self.options["bootstrap_base_commit"] = "basesha"
        fake_proc = types.SimpleNamespace(returncode=0)
        with (
            mock.patch.object(subprocess, "run", return_value=fake_proc),
            mock.patch.object(deployer, "is_ancestor", return_value=True),
            mock.patch.object(deployer, "initial_reconcile", return_value=(True, [])),
            mock.patch.object(deployer, "notify"),
        ):
            result = deployer.bootstrap_if_needed(self.options, "targetsha")

        self.assertTrue(result)
        self.assertEqual(deployer.state()["last_deployed_sha"], "basesha")

    def test_notify_returns_false_when_service_call_fails(self) -> None:
        with (
            mock.patch.object(deployer, "ha_headers", return_value={}),
            mock.patch.object(
                deployer.requests,
                "post",
                side_effect=RuntimeError("service unavailable"),
            ),
        ):
            self.assertFalse(
                deployer.notify(self.options, "Test title", "Test message")
            )

    def test_problem_notification_retries_before_setting_dedupe_marker(self) -> None:
        st: dict[str, object] = {}
        with (
            mock.patch.object(
                deployer,
                "notify",
                side_effect=[False, True],
            ) as notify_mock,
            mock.patch.object(deployer, "persistent_notification") as persistent_mock,
            mock.patch.object(
                deployer, "dismiss_persistent_notification"
            ) as dismiss_mock,
        ):
            self.assertFalse(
                deployer.send_problem_notification(
                    self.options,
                    st,
                    "problem:key",
                    "Problem title",
                    "Problem message",
                    priority=1,
                    now_ts=1000,
                )
            )
            failed_state = deployer.state()
            self.assertNotEqual(
                failed_state.get("last_notified_problem"), "problem:key"
            )
            self.assertTrue(failed_state["notification_pending"])
            self.assertEqual(failed_state["notification_retry_at"], 1900)
            self.assertEqual(
                deployer.load_json(deployer.STATUS_FILE, {})[
                    "last_notification_result"
                ],
                "error",
            )
            persistent_mock.assert_called_once()

            self.assertFalse(
                deployer.send_problem_notification(
                    self.options,
                    failed_state,
                    "problem:key",
                    "Problem title",
                    "Problem message",
                    priority=1,
                    now_ts=1899,
                )
            )
            self.assertEqual(notify_mock.call_count, 1)

            self.assertTrue(
                deployer.send_problem_notification(
                    self.options,
                    failed_state,
                    "problem:key",
                    "Problem title",
                    "Problem message",
                    priority=1,
                    now_ts=1900,
                )
            )
            self.assertEqual(notify_mock.call_count, 2)
            delivered_state = deployer.state()
            self.assertEqual(
                delivered_state["last_notified_problem"], "problem:key"
            )
            self.assertFalse(delivered_state["notification_pending"])
            self.assertEqual(delivered_state["last_notification_result"], "success")
            dismiss_mock.assert_called_once_with(
                deployer.NOTIFICATION_FALLBACK_ID
            )

    def test_fetch_failure_alert_retries_after_delivery_failure(self) -> None:
        with (
            mock.patch.object(
                deployer,
                "notify",
                side_effect=[False, True],
            ) as notify_mock,
            mock.patch.object(deployer, "persistent_notification"),
            mock.patch.object(deployer, "dismiss_persistent_notification"),
        ):
            deployer.record_fetch_failure(self.options, "network down", now_ts=1000)
            deployer.record_fetch_failure(self.options, "network down", now_ts=2800)
            self.assertFalse(deployer.state().get("fetch_failure_notified", False))
            deployer.record_fetch_failure(self.options, "network down", now_ts=3699)
            self.assertEqual(notify_mock.call_count, 1)
            deployer.record_fetch_failure(self.options, "network down", now_ts=3700)
            self.assertEqual(notify_mock.call_count, 2)

        state = deployer.state()
        self.assertTrue(state["fetch_failure_notified"])
        self.assertFalse(state["notification_pending"])
        self.assertEqual(state["last_notification_result"], "success")

    def test_fetch_failure_alert_is_delayed_deduplicated_and_recovers(self) -> None:
        with mock.patch.object(deployer, "notify") as notify_mock:
            deployer.record_fetch_failure(self.options, "network down", now_ts=1000)
            deployer.record_fetch_failure(self.options, "network down", now_ts=2799)
            self.assertEqual(notify_mock.call_count, 0)
            deployer.record_fetch_failure(self.options, "network down", now_ts=2800)
            self.assertEqual(notify_mock.call_count, 1)
            self.assertEqual(
                notify_mock.call_args.args[1], "Git deployer repository access problem"
            )
            deployer.record_fetch_failure(self.options, "network down", now_ts=4000)
            self.assertEqual(notify_mock.call_count, 1)
            deployer.record_fetch_success(self.options, now_ts=4100)
            self.assertEqual(notify_mock.call_count, 2)
            self.assertEqual(
                notify_mock.call_args.args[1], "Git deployer repository access restored"
            )

        state = deployer.state()
        self.assertEqual(state["last_fetch_result"], "success")
        self.assertEqual(state["fetch_failure_started_at"], 0)

    def test_poll_target_fetches_repository_once_and_records_success(self) -> None:
        with (
            mock.patch.object(deployer, "ensure_ssh_material", return_value="ssh-ed25519 AAA"),
            mock.patch.object(deployer, "ensure_repository", return_value="targetsha") as ensure_mock,
            mock.patch.object(deployer, "record_fetch_success") as success_mock,
            mock.patch.object(deployer, "record_fetch_failure") as failure_mock,
        ):
            result = deployer.poll_target(self.options)

        self.assertEqual(result, "targetsha")
        ensure_mock.assert_called_once_with(self.options["repository"], "main")
        success_mock.assert_called_once()
        failure_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
