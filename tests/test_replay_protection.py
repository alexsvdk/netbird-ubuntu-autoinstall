#!/usr/bin/env python3
"""Replay protection logic tests.

check_generation() is implemented inline, mirroring what the recovery agent
will use. All tests are fully synthetic — no filesystem side-effects outside
of temporary directories.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

# ---------------------------------------------------------------------------
# Implementation under test (inline)
# ---------------------------------------------------------------------------

STATE_FILE_VERSION = 1


def check_generation(state_file_path: str, new_generation: int) -> bool:
    """Return True iff new_generation > last applied generation.

    Rules:
    - new_generation must be >= 1.
    - If no state file exists, any generation >= 1 is accepted.
    - If the state file is corrupt / missing 'generation' key: rejected safely.
    - new_generation == last: rejected (idempotency guard).
    - new_generation < last: rejected (replay).
    """
    if new_generation < 1:
        return False

    state_path = Path(state_file_path)
    if not state_path.exists():
        return True  # First run

    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        last = data["generation"]
        if not isinstance(last, int):
            return False
        return new_generation > last
    except (json.JSONDecodeError, KeyError, OSError, TypeError):
        return False


def write_state(state_file_path: str, generation: int, **extra: object) -> None:
    """Persist applied generation to state file."""
    state = {"generation": generation, **extra}
    state_path = Path(state_file_path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(state_path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    os.replace(tmp, str(state_path))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestCheckGenerationFirstRun(unittest.TestCase):
    """Behaviour when no state file exists (first run)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "state.json")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_first_run_generation_1_accepted(self) -> None:
        self.assertTrue(check_generation(self.state, 1))

    def test_first_run_large_generation_accepted(self) -> None:
        self.assertTrue(check_generation(self.state, 2026091901))

    def test_first_run_generation_0_rejected(self) -> None:
        self.assertFalse(check_generation(self.state, 0))

    def test_first_run_negative_generation_rejected(self) -> None:
        self.assertFalse(check_generation(self.state, -1))


class TestCheckGenerationWithExistingState(unittest.TestCase):
    """Replay protection when a state file already exists."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "state.json")
        write_state(self.state, generation=50)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_higher_generation_accepted(self) -> None:
        self.assertTrue(check_generation(self.state, 100))

    def test_same_generation_rejected(self) -> None:
        self.assertFalse(check_generation(self.state, 50))

    def test_lower_generation_rejected(self) -> None:
        self.assertFalse(check_generation(self.state, 30))

    def test_generation_1_when_last_is_50_rejected(self) -> None:
        self.assertFalse(check_generation(self.state, 1))

    def test_generation_51_accepted(self) -> None:
        self.assertTrue(check_generation(self.state, 51))

    def test_large_generation_accepted(self) -> None:
        self.assertTrue(check_generation(self.state, 99999999999))


class TestCheckGenerationEdgeCases(unittest.TestCase):
    """Corrupt / invalid state file handling."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "state.json")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_corrupt_state_file_rejected(self) -> None:
        Path(self.state).write_text("not valid json {{{", encoding="utf-8")
        self.assertFalse(check_generation(self.state, 100))

    def test_empty_state_file_rejected(self) -> None:
        Path(self.state).write_text("", encoding="utf-8")
        self.assertFalse(check_generation(self.state, 100))

    def test_state_missing_generation_key_rejected(self) -> None:
        Path(self.state).write_text(
            json.dumps({"sha256": "abc", "applied_at": "2026-01-01"}),
            encoding="utf-8",
        )
        self.assertFalse(check_generation(self.state, 100))

    def test_state_generation_is_string_rejected(self) -> None:
        Path(self.state).write_text(
            json.dumps({"generation": "fifty"}),
            encoding="utf-8",
        )
        self.assertFalse(check_generation(self.state, 100))

    def test_state_generation_is_null_rejected(self) -> None:
        Path(self.state).write_text(
            json.dumps({"generation": None}),
            encoding="utf-8",
        )
        self.assertFalse(check_generation(self.state, 100))

    def test_no_crash_on_corrupt_file(self) -> None:
        """Must not raise an exception on corrupt data."""
        Path(self.state).write_text("}{corrupt", encoding="utf-8")
        try:
            result = check_generation(self.state, 100)
        except Exception as exc:  # noqa: BLE001
            self.fail(f"check_generation raised an exception on corrupt file: {exc}")
        self.assertFalse(result)

    def test_generation_0_always_rejected(self) -> None:
        """Generation 0 must never be accepted regardless of state."""
        self.assertFalse(check_generation(self.state, 0))

    def test_large_generation_number_accepted(self) -> None:
        """Very large generation numbers are valid."""
        self.assertTrue(check_generation(self.state, 2**31 - 1))


class TestWriteState(unittest.TestCase):
    """State file is written correctly and atomically."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "state.json")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_state_file_written(self) -> None:
        write_state(self.state, generation=42)
        self.assertTrue(Path(self.state).exists())

    def test_state_file_contains_generation(self) -> None:
        write_state(self.state, generation=42)
        data = json.loads(Path(self.state).read_text(encoding="utf-8"))
        self.assertEqual(data["generation"], 42)

    def test_state_generation_readable_by_check(self) -> None:
        write_state(self.state, generation=100)
        self.assertTrue(check_generation(self.state, 101))
        self.assertFalse(check_generation(self.state, 100))

    def test_state_updated_after_second_write(self) -> None:
        write_state(self.state, generation=10)
        write_state(self.state, generation=20)
        data = json.loads(Path(self.state).read_text(encoding="utf-8"))
        self.assertEqual(data["generation"], 20)

    def test_extra_fields_preserved(self) -> None:
        write_state(self.state, generation=1, sha256="abc123", applied_at="2026-01-01")
        data = json.loads(Path(self.state).read_text(encoding="utf-8"))
        self.assertEqual(data["sha256"], "abc123")
        self.assertEqual(data["applied_at"], "2026-01-01")

    def test_no_tmp_file_left_after_write(self) -> None:
        write_state(self.state, generation=5)
        tmp_file = self.state + ".tmp"
        self.assertFalse(Path(tmp_file).exists(), "Temp file left after atomic write")

    def test_parent_dir_created_if_missing(self) -> None:
        nested = os.path.join(self.tmp.name, "a", "b", "state.json")
        write_state(nested, generation=1)
        self.assertTrue(Path(nested).exists())


if __name__ == "__main__":
    unittest.main()
