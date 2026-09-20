#!/usr/bin/env python3
"""NetBird profile migration and rollback tests.

All subprocess calls are mocked — no real netbird CLI, no real network.
The NetBirdManager class is implemented inline, mirroring the recovery agent.
"""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

# ---------------------------------------------------------------------------
# Implementation under test (inline)
# ---------------------------------------------------------------------------


class NetBirdError(Exception):
    """NetBird operation failed."""


class NetBirdManager:
    """Manage NetBird profiles for samovar.

    Setup key is written to a temp file with mode 0600 in /run,
    passed via --setup-key-file, and deleted immediately after use.
    setup_key is never passed as a command-line argument or logged.
    """

    SETUP_KEY_DIR = "/run" if os.path.isdir("/run") else tempfile.gettempdir()

    def __init__(self, netbird_bin: str = "netbird") -> None:
        self.netbird_bin = netbird_bin
        self._log_entries: list[str] = []

    def _log(self, msg: str) -> None:
        self._log_entries.append(msg)

    def _run(self, args: list[str], check: bool = True) -> "subprocess.CompletedProcess":  # noqa: F821
        import subprocess
        cmd = [self.netbird_bin] + args
        self._log(f"Running: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True)
        if check and result.returncode != 0:
            raise NetBirdError(f"netbird {args[0]} failed (rc={result.returncode})")
        return result

    def _write_setup_key_file(self, setup_key: str) -> str:
        """Write setup key to a temp file in /run with mode 0600.

        Returns the path. Caller is responsible for deletion.
        """
        import tempfile
        fd, path = tempfile.mkstemp(prefix="nb-setup-", suffix=".key", dir=self.SETUP_KEY_DIR)
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
            os.write(fd, setup_key.encode())
        finally:
            os.close(fd)
        return path

    def apply(
        self,
        management_url: str,
        setup_key: str,
        profile_name: str,
        generation: int,
    ) -> None:
        """Apply a new NetBird profile.

        Steps:
        1. Validate management_url starts with https://.
        2. Get current (old) profile id.
        3. Create new profile named '{profile_name}-gen{generation}'.
        4. Switch to new profile.
        5. Run netbird up with --setup-key-file (temp file deleted immediately).
        6. Verify status.
        7. On success: delete old profile.
        8. On failure at any step: restore old profile.
        """
        if not management_url.startswith("https://"):
            raise NetBirdError(f"management_url must use HTTPS, got: {management_url}")

        new_profile = f"{profile_name}-gen{generation}"
        old_profile_result = self._run(["profile", "list", "--active"], check=False)
        old_profile = old_profile_result.stdout.strip() or "default"
        self._log(f"Old profile: {old_profile}")

        # Create new profile
        self._run(["profile", "add", new_profile])
        self._log(f"Created profile: {new_profile}")

        key_path = None
        try:
            # Switch to new profile
            self._run(["profile", "use", new_profile])

            # Write setup key to temp file
            key_path = self._write_setup_key_file(setup_key)
            try:
                self._run([
                    "up",
                    "--management-url", management_url,
                    "--setup-key-file", key_path,
                    "--hostname", "samovar",
                ])
            finally:
                # Always delete the key immediately
                try:
                    os.unlink(key_path)
                    key_path = None
                except FileNotFoundError:
                    pass

            # Verify
            status = self._run(["status", "--check", "startup"], check=False)
            if status.returncode != 0:
                raise NetBirdError("netbird status check failed after up")

            # Success — delete old profile
            if old_profile and old_profile != new_profile:
                self._run(["profile", "delete", old_profile], check=False)
                self._log(f"Deleted old profile: {old_profile}")

        except NetBirdError:
            # Rollback: restore old profile
            self._log(f"Rolling back to profile: {old_profile}")
            try:
                self._run(["profile", "use", old_profile], check=False)
                self._run(["up", "--hostname", "samovar"], check=False)
            except Exception:  # noqa: BLE001
                pass
            raise


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestNetBirdManagerHTTPS(unittest.TestCase):
    """management_url validation tests."""

    def test_http_url_rejected_before_any_netbird_calls(self) -> None:
        manager = NetBirdManager()
        with patch("subprocess.run") as mock_run:
            with self.assertRaises(NetBirdError) as ctx:
                manager.apply(
                    management_url="http://api.netbird.io:443",
                    setup_key="fake-key",
                    profile_name="test",
                    generation=1,
                )
            # subprocess.run should NOT have been called (rejected before netbird)
            mock_run.assert_not_called()
        self.assertIn("HTTPS", str(ctx.exception))

    def test_https_url_accepted(self) -> None:
        def mock_run(cmd, **kwargs):
            m = MagicMock()
            m.returncode = 0
            m.stdout = "old-profile"
            m.stderr = ""
            return m

        with patch("subprocess.run", side_effect=mock_run), \
             patch.object(NetBirdManager, "_write_setup_key_file", return_value="/tmp/nb-setup-fake.key"):
            manager = NetBirdManager()
            # Should not raise
            manager.apply(
                management_url="https://api.netbird.io:443",
                setup_key="fake-key",
                profile_name="test",
                generation=1,
            )


class TestNetBirdManagerProfileNaming(unittest.TestCase):
    """New profile name must include generation number."""

    def test_new_profile_includes_generation(self) -> None:
        calls_made: list[list[str]] = []

        def mock_run(cmd, **kwargs):
            calls_made.append(list(cmd))
            m = MagicMock()
            m.returncode = 0
            m.stdout = "old-profile"
            m.stderr = ""
            return m

        with patch("subprocess.run", side_effect=mock_run):
            manager = NetBirdManager()
            manager.apply(
                management_url="https://api.netbird.io:443",
                setup_key="fake-key",
                profile_name="myprofile",
                generation=42,
            )

        # Find the 'profile add' call
        profile_add_calls = [c for c in calls_made if "profile" in c and "add" in c]
        self.assertTrue(len(profile_add_calls) > 0, "Expected 'profile add' call")
        profile_add_args = " ".join(profile_add_calls[0])
        self.assertIn("42", profile_add_args, "Generation number must appear in profile name")
        self.assertIn("myprofile", profile_add_args)


class TestNetBirdManagerSetupKey(unittest.TestCase):
    """Setup key must be passed via temp file, not as a CLI arg."""

    def _collect_calls(self, key: str = "SUPER_SECRET_SETUP_KEY_XYZ") -> list[list[str]]:
        calls_made: list[list[str]] = []

        def mock_run(cmd, **kwargs):
            calls_made.append(list(cmd))
            m = MagicMock()
            m.returncode = 0
            m.stdout = "old-profile"
            m.stderr = ""
            return m

        with patch("subprocess.run", side_effect=mock_run), \
             patch.object(NetBirdManager, "_write_setup_key_file", return_value="/run/nb-setup-fake.key"):
            manager = NetBirdManager()
            manager.apply(
                management_url="https://api.netbird.io:443",
                setup_key=key,
                profile_name="test",
                generation=1,
            )

        return calls_made

    def test_setup_key_not_in_subprocess_args(self) -> None:
        key = "SUPER_SECRET_SETUP_KEY_XYZ"
        calls = self._collect_calls(key)
        for cmd in calls:
            self.assertNotIn(key, cmd, f"setup_key appeared in command args: {cmd}")

    def test_setup_key_file_flag_used(self) -> None:
        calls = self._collect_calls()
        up_calls = [c for c in calls if "up" in c]
        self.assertTrue(len(up_calls) > 0, "Expected 'netbird up' call")
        up_args = " ".join(up_calls[0])
        self.assertIn("--setup-key-file", up_args, "--setup-key-file flag must be used")
        self.assertNotIn("--setup-key ", up_args + " ", "--setup-key (direct) must NOT be used")

    def test_setup_key_temp_file_mode_0600(self) -> None:
        """Temp key file must be created with mode 0600."""
        created_paths: list[str] = []
        created_modes: list[int] = []
        original_mkstemp = tempfile.mkstemp
        original_chmod = os.chmod

        with tempfile.TemporaryDirectory() as td:
            def tracking_mkstemp(prefix="", suffix="", dir=None):
                fd, path = original_mkstemp(prefix=prefix, suffix=suffix, dir=td)
                created_paths.append(path)
                return fd, path

            def tracking_chmod(path, mode):
                if path in created_paths:
                    created_modes.append(mode)
                return original_chmod(path, mode)

            def mock_run(cmd, **kwargs):
                m = MagicMock()
                m.returncode = 0
                m.stdout = "old-profile"
                m.stderr = ""
                return m

            with patch("tempfile.mkstemp", side_effect=tracking_mkstemp), \
                 patch("os.chmod", side_effect=tracking_chmod), \
                 patch("subprocess.run", side_effect=mock_run):
                manager = NetBirdManager()
                manager.apply(
                    management_url="https://api.netbird.io:443",
                    setup_key="secret",
                    profile_name="test",
                    generation=1,
                )

            # The key file should have been chmoded to 0600
            self.assertTrue(
                any(m == stat.S_IRUSR | stat.S_IWUSR for m in created_modes),
                f"Expected 0600 mode on key file, got: {[oct(m) for m in created_modes]}",
            )

    def test_setup_key_temp_file_deleted_after_use(self) -> None:
        """Temp key file must not exist after netbird up returns."""
        deleted_paths: list[str] = []
        created_paths: list[str] = []
        original_unlink = os.unlink

        def tracking_unlink(path):
            deleted_paths.append(path)
            return original_unlink(path)

        def mock_run(cmd, **kwargs):
            m = MagicMock()
            m.returncode = 0
            m.stdout = "old-profile"
            m.stderr = ""
            return m

        with patch("subprocess.run", side_effect=mock_run), \
             patch("os.unlink", side_effect=tracking_unlink):
            manager = NetBirdManager()
            manager.apply(
                management_url="https://api.netbird.io:443",
                setup_key="secret",
                profile_name="test",
                generation=1,
            )

        # At least one unlink was called (for the key file)
        self.assertTrue(len(deleted_paths) >= 1, "Expected os.unlink to be called for key file")


class TestNetBirdManagerRollback(unittest.TestCase):
    """Rollback behaviour on failure."""

    def test_rollback_on_verify_failure(self) -> None:
        """If status check fails, old profile must be restored."""
        calls_made: list[list[str]] = []
        call_count = [0]

        def mock_run(cmd, **kwargs):
            calls_made.append(list(cmd))
            m = MagicMock()
            m.stdout = "old-profile"
            m.stderr = ""
            # Fail on 'status --check startup'
            if "status" in cmd and "--check" in cmd:
                m.returncode = 1
            else:
                m.returncode = 0
            return m

        with patch("subprocess.run", side_effect=mock_run), \
             patch.object(NetBirdManager, "_write_setup_key_file", return_value="/run/nb-setup-fake.key"):
            manager = NetBirdManager()
            with self.assertRaises(NetBirdError):
                manager.apply(
                    management_url="https://api.netbird.io:443",
                    setup_key="fake-key",
                    profile_name="test",
                    generation=1,
                )

        # After failure, should have attempted to switch back to old profile
        profile_use_calls = [c for c in calls_made if "profile" in c and "use" in c]
        # Should have at least 2: switch to new, then switch back to old
        self.assertGreaterEqual(len(profile_use_calls), 2)

    def test_rollback_on_netbird_up_failure(self) -> None:
        """If netbird up fails, old profile must be restored."""
        calls_made: list[list[str]] = []

        def mock_run(cmd, **kwargs):
            calls_made.append(list(cmd))
            m = MagicMock()
            m.stdout = "old-profile"
            m.stderr = ""
            # Fail on 'up' (when it has --setup-key-file)
            if "up" in cmd and "--setup-key-file" in cmd:
                m.returncode = 1
            else:
                m.returncode = 0
            return m

        with patch("subprocess.run", side_effect=mock_run), \
             patch.object(NetBirdManager, "_write_setup_key_file", return_value="/run/nb-setup-fake.key"):
            manager = NetBirdManager()
            with self.assertRaises(NetBirdError):
                manager.apply(
                    management_url="https://api.netbird.io:443",
                    setup_key="fake-key",
                    profile_name="test",
                    generation=1,
                )

        # Should have attempted profile rollback
        log_text = " ".join(manager._log_entries)
        self.assertIn("Rolling back", log_text)

    def test_setup_key_not_in_logs(self) -> None:
        """Setup key must never appear in log entries."""
        setup_key = "TOP_SECRET_SETUP_KEY_ABCDEFG"

        def mock_run(cmd, **kwargs):
            m = MagicMock()
            m.returncode = 0
            m.stdout = "old-profile"
            m.stderr = ""
            return m

        with patch("subprocess.run", side_effect=mock_run), \
             patch.object(NetBirdManager, "_write_setup_key_file", return_value="/run/nb-setup-fake.key"):
            manager = NetBirdManager()
            manager.apply(
                management_url="https://api.netbird.io:443",
                setup_key=setup_key,
                profile_name="test",
                generation=1,
            )

        for entry in manager._log_entries:
            self.assertNotIn(setup_key, entry, f"setup_key leaked into log: {entry}")

    def test_success_deletes_old_profile(self) -> None:
        """After successful apply, old profile must be deleted."""
        calls_made: list[list[str]] = []

        def mock_run(cmd, **kwargs):
            calls_made.append(list(cmd))
            m = MagicMock()
            m.returncode = 0
            m.stdout = "old-profile-name"
            m.stderr = ""
            return m

        with patch("subprocess.run", side_effect=mock_run), \
             patch.object(NetBirdManager, "_write_setup_key_file", return_value="/run/nb-setup-fake.key"):
            manager = NetBirdManager()
            manager.apply(
                management_url="https://api.netbird.io:443",
                setup_key="fake-key",
                profile_name="test",
                generation=1,
            )

        delete_calls = [c for c in calls_made if "profile" in c and "delete" in c]
        self.assertTrue(len(delete_calls) > 0, "Expected 'profile delete' call on success")


if __name__ == "__main__":
    unittest.main()
