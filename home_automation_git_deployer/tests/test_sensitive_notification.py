# =============================================================================
# HOME AUTOMATION GIT DEPLOYER SENSITIVE-NOTIFICATION TESTS
# =============================================================================
# Version history:
# 1.0.0 - 2026-08-27 - Added regression coverage verifying that sensitive-commit approval notifications include the complete target SHA and approval option name.
# =============================================================================

from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest
from unittest import mock

MODULE_PATH = Path(__file__).resolve().parents[1] / "deployer.py"
spec = importlib.util.spec_from_file_location("git_deployer_sensitive_test", MODULE_PATH)
assert spec and spec.loader
deployer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deployer)


class SensitiveApprovalNotificationTests(unittest.TestCase):
    def test_notification_includes_full_target_sha(self) -> None:
        target_sha = "0123456789abcdef0123456789abcdef01234567"
        options = {
            "repository": "git@github.com:example/config.git",
            "branch": "main",
            "poll_seconds": 300,
            "pushover_service": "notify.pushover",
            "approved_sensitive_commit": "",
            "bootstrap_base_commit": "",
        }
        state = {
            "last_deployed_sha": "oldsha",
            "last_notified_problem": None,
        }

        with (
            mock.patch.object(deployer, "state", return_value=state),
            mock.patch.object(deployer, "is_ancestor", return_value=True),
            mock.patch.object(
                deployer,
                "diff_name_status",
                return_value=[("D", "packages/example.yaml")],
            ),
            mock.patch.object(deployer, "set_status_state"),
            mock.patch.object(deployer, "set_state"),
            mock.patch.object(deployer, "publish_status"),
            mock.patch.object(deployer, "notify") as notify_mock,
        ):
            deployer.deploy_once(options, target_sha)

        notify_mock.assert_called_once()
        self.assertEqual(notify_mock.call_args.args[1], "Git deploy approval required")
        message = notify_mock.call_args.args[2]
        self.assertIn(f"Full SHA: {target_sha}", message)
        self.assertIn("approved_sensitive_commit", message)
        self.assertIn(target_sha, message)


if __name__ == "__main__":
    unittest.main()
