"""Regression checks for the Windows Docker preflight."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = (ROOT / "build-autoinstall-iso.ps1").read_text(encoding="utf-8")
BATCH = (ROOT / "build-autoinstall-iso.bat").read_text(encoding="utf-8")


class TestWindowsDockerPreflight(unittest.TestCase):
    def test_powershell_checks_and_starts_docker_desktop(self) -> None:
        self.assertIn("function Test-DockerDaemon", POWERSHELL)
        self.assertIn("& docker info --format", POWERSHELL)
        self.assertIn("2>$null", POWERSHELL)
        self.assertNotIn("docker info --format '{{.ServerVersion}}' >$null 2>&1", POWERSHELL)
        self.assertIn("& docker desktop start", POWERSHELL)
        self.assertIn('Docker\\Docker\\Docker Desktop.exe', POWERSHELL)
        self.assertIn("Start-Sleep -Seconds 2", POWERSHELL)
        self.assertIn("within 180 seconds", POWERSHELL)

    def test_preflight_runs_before_the_build(self) -> None:
        self.assertLess(
            POWERSHELL.index("Ensure-DockerDaemon"),
            POWERSHELL.index("This creates a FULLY UNATTENDED installer."),
        )

    def test_batch_entrypoint_uses_powershell_script(self) -> None:
        self.assertIn("build-autoinstall-iso.ps1", BATCH)


if __name__ == "__main__":
    unittest.main()
