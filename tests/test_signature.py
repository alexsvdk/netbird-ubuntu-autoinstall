#!/usr/bin/env python3
"""SSH signature verification logic tests.

All subprocess calls are mocked — no real ssh-keygen, no real keys.
The verify_signature() function is implemented inline to test the algorithm
pattern that will be used by the recovery agent.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Implementation under test (inline — mirrors what the recovery agent will do)
# ---------------------------------------------------------------------------

NAMESPACE = "samovar-recovery"


def verify_signature(json_bytes: bytes, sig_bytes: bytes, allowed_signers_path: str) -> bool:
    """Verify an SSH detached signature over json_bytes.

    Writes json_bytes to a temporary file, runs:
        ssh-keygen -Y verify -f <allowed_signers> -I <principal> -n <namespace> -s <sig_file>
    Cleans up the temp file regardless of outcome.

    Returns True iff ssh-keygen exits 0.
    Catches FileNotFoundError (ssh-keygen not installed) and returns False.
    The setup_key / password are NOT passed to this function and are never
    present in the subprocess call arguments.
    """
    sig_fd, sig_path = tempfile.mkstemp(suffix=".sig", dir="/tmp")
    json_fd, json_path = tempfile.mkstemp(suffix=".json", dir="/tmp")
    try:
        # Write content to temp files
        os.write(sig_fd, sig_bytes)
        os.close(sig_fd)
        os.write(json_fd, json_bytes)
        os.close(json_fd)

        cmd = [
            "ssh-keygen",
            "-Y", "verify",
            "-f", allowed_signers_path,
            "-I", "samovar-owner",
            "-n", NAMESPACE,
            "-s", sig_path,
        ]
        result = subprocess.run(
            cmd,
            stdin=open(json_path, "rb"),
            capture_output=True,
        )
        return result.returncode == 0
    except FileNotFoundError:
        return False
    finally:
        for p in (sig_path, json_path):
            try:
                os.unlink(p)
            except FileNotFoundError:
                pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestVerifySignatureReturnValues(unittest.TestCase):
    """verify_signature() returns True/False based on ssh-keygen exit code."""

    def _make_mock_result(self, returncode: int) -> MagicMock:
        m = MagicMock()
        m.returncode = returncode
        return m

    def test_returns_true_when_ssh_keygen_exits_0(self) -> None:
        with patch("subprocess.run", return_value=self._make_mock_result(0)) as mock_run:
            result = verify_signature(b'{"schema":1}', b"fakesig", "/etc/samovar/allowed_signers")
        self.assertTrue(result)
        mock_run.assert_called_once()

    def test_returns_false_when_ssh_keygen_exits_nonzero(self) -> None:
        with patch("subprocess.run", return_value=self._make_mock_result(1)):
            result = verify_signature(b'{"schema":1}', b"badsig", "/etc/samovar/allowed_signers")
        self.assertFalse(result)

    def test_returns_false_when_ssh_keygen_exits_2(self) -> None:
        with patch("subprocess.run", return_value=self._make_mock_result(2)):
            result = verify_signature(b'{}', b"badsig", "/etc/samovar/allowed_signers")
        self.assertFalse(result)

    def test_returns_false_when_ssh_keygen_not_found(self) -> None:
        with patch("subprocess.run", side_effect=FileNotFoundError("ssh-keygen not found")):
            result = verify_signature(b'{"schema":1}', b"sig", "/etc/samovar/allowed_signers")
        self.assertFalse(result)


class TestVerifySignatureNamespace(unittest.TestCase):
    """Verify uses the correct namespace 'samovar-recovery'."""

    def test_namespace_is_samovar_recovery(self) -> None:
        captured_cmd: list[list[str]] = []

        def capture_run(cmd, **kwargs):  # noqa: ANN001
            captured_cmd.append(cmd)
            m = MagicMock()
            m.returncode = 0
            return m

        with patch("subprocess.run", side_effect=capture_run):
            verify_signature(b'{}', b"sig", "/signers")

        self.assertTrue(len(captured_cmd) == 1, "subprocess.run should be called once")
        cmd = captured_cmd[0]
        idx = cmd.index("-n")
        self.assertEqual(cmd[idx + 1], "samovar-recovery")

    def test_wrong_namespace_would_give_nonzero(self) -> None:
        """Simulate what happens when ssh-keygen rejects the wrong namespace."""
        with patch("subprocess.run", return_value=MagicMock(returncode=255)):
            result = verify_signature(b'{}', b"sig_signed_with_wrong_ns", "/signers")
        self.assertFalse(result)


class TestVerifySignatureSubprocessArgs(unittest.TestCase):
    """Check subprocess is called with the expected flags."""

    def _run_and_capture(self) -> list[str]:
        captured: list[list[str]] = []

        def fake_run(cmd, **kwargs):  # noqa: ANN001
            captured.append(list(cmd))
            m = MagicMock()
            m.returncode = 0
            return m

        with patch("subprocess.run", side_effect=fake_run):
            verify_signature(b'data', b"sig", "/my/signers")

        return captured[0]

    def test_calls_ssh_keygen(self) -> None:
        cmd = self._run_and_capture()
        self.assertEqual(cmd[0], "ssh-keygen")

    def test_uses_verify_subcommand(self) -> None:
        cmd = self._run_and_capture()
        self.assertIn("-Y", cmd)
        self.assertIn("verify", cmd)

    def test_passes_allowed_signers_path(self) -> None:
        cmd = self._run_and_capture()
        idx = cmd.index("-f")
        self.assertEqual(cmd[idx + 1], "/my/signers")

    def test_setup_key_not_in_subprocess_args(self) -> None:
        """setup_key / secrets must never appear in the ssh-keygen command."""
        setup_key = "SUPER_SECRET_SETUP_KEY_12345"
        captured: list[list[str]] = []

        def fake_run(cmd, **kwargs):  # noqa: ANN001
            captured.append(list(cmd))
            m = MagicMock()
            m.returncode = 0
            return m

        with patch("subprocess.run", side_effect=fake_run):
            # Even if json_bytes happened to contain the key text, it must
            # not appear in the subprocess *argument list*.
            json_bytes = f'{{"setup_key": "{setup_key}"}}'.encode()
            verify_signature(json_bytes, b"sig", "/signers")

        full_cmd_str = " ".join(captured[0])
        self.assertNotIn(setup_key, full_cmd_str)


class TestVerifySignatureTempFileCleanup(unittest.TestCase):
    """Temp files must not leak after verify_signature() returns."""

    def test_temp_file_deleted_after_success(self) -> None:
        created_paths: list[str] = []
        original_mkstemp = tempfile.mkstemp

        def tracking_mkstemp(**kwargs):  # noqa: ANN001
            fd, path = original_mkstemp(**kwargs)
            created_paths.append(path)
            return fd, path

        with patch("tempfile.mkstemp", side_effect=tracking_mkstemp), \
             patch("subprocess.run", return_value=MagicMock(returncode=0)):
            verify_signature(b'data', b"sig", "/signers")

        for p in created_paths:
            self.assertFalse(
                Path(p).exists(),
                f"Temp file was not cleaned up: {p}",
            )

    def test_temp_file_deleted_after_failure(self) -> None:
        created_paths: list[str] = []
        original_mkstemp = tempfile.mkstemp

        def tracking_mkstemp(**kwargs):  # noqa: ANN001
            fd, path = original_mkstemp(**kwargs)
            created_paths.append(path)
            return fd, path

        with patch("tempfile.mkstemp", side_effect=tracking_mkstemp), \
             patch("subprocess.run", return_value=MagicMock(returncode=1)):
            verify_signature(b'data', b"badsig", "/signers")

        for p in created_paths:
            self.assertFalse(
                Path(p).exists(),
                f"Temp file was not cleaned up on failure: {p}",
            )

    def test_temp_file_deleted_when_ssh_keygen_missing(self) -> None:
        created_paths: list[str] = []
        original_mkstemp = tempfile.mkstemp

        def tracking_mkstemp(**kwargs):  # noqa: ANN001
            fd, path = original_mkstemp(**kwargs)
            created_paths.append(path)
            return fd, path

        with patch("tempfile.mkstemp", side_effect=tracking_mkstemp), \
             patch("subprocess.run", side_effect=FileNotFoundError()):
            verify_signature(b'data', b"sig", "/signers")

        for p in created_paths:
            self.assertFalse(
                Path(p).exists(),
                f"Temp file was not cleaned up when ssh-keygen missing: {p}",
            )


class TestVerifySignatureContentPassing(unittest.TestCase):
    """The actual json_bytes content is what gets verified, not something else."""

    def test_different_content_bytes_are_passed(self) -> None:
        """Two calls with different content must pass different data."""
        contents_seen: list[bytes] = []

        def fake_run(cmd, stdin=None, **kwargs):  # noqa: ANN001
            if stdin is not None:
                contents_seen.append(stdin.read())
            m = MagicMock()
            m.returncode = 0
            return m

        with patch("subprocess.run", side_effect=fake_run):
            verify_signature(b'content_A', b"sig", "/signers")
            verify_signature(b'content_B', b"sig", "/signers")

        self.assertEqual(len(contents_seen), 2)
        self.assertNotEqual(contents_seen[0], contents_seen[1])


if __name__ == "__main__":
    unittest.main()
