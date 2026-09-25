"""Regression checks for the Windows Docker preflight."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = (ROOT / "build-autoinstall-iso.ps1").read_text(encoding="utf-8")
BUNDLE = (ROOT / "offline" / "build-apt-bundle.sh").read_text(encoding="utf-8")
NVIDIA_KEY = (ROOT / "offline" / "keys" / "nvidia-container-toolkit.asc").read_text(encoding="ascii")
NETBIRD_KEY = (ROOT / "offline" / "keys" / "netbird.asc").read_text(encoding="ascii")
BATCH = (ROOT / "build-autoinstall-iso.bat").read_text(encoding="utf-8")


class TestWindowsDockerPreflight(unittest.TestCase):
    def test_powershell_checks_and_starts_docker_desktop(self) -> None:
        self.assertIn("function Test-DockerDaemon", POWERSHELL)
        self.assertIn("function Test-DockerImage", POWERSHELL)
        self.assertIn("& docker info --format", POWERSHELL)
        self.assertIn("$ErrorActionPreference = \"Continue\"", POWERSHELL)
        self.assertIn("$val = $val -replace", POWERSHELL)
        self.assertIn("ALLOWED_SIGNERS_B64", POWERSHELL)
        self.assertIn("base64 -d", POWERSHELL)
        self.assertIn("2>&1 | Out-Null", POWERSHELL)
        self.assertNotIn("docker info --format '{{.ServerVersion}}' >$null 2>&1", POWERSHELL)
        self.assertIn("& docker desktop start", POWERSHELL)
        self.assertIn('Docker\\Docker\\Docker Desktop.exe', POWERSHELL)
        self.assertIn("Start-Sleep -Seconds 2", POWERSHELL)
        self.assertIn("within 180 seconds", POWERSHELL)
        self.assertIn("DockerCredentialHelperPath", POWERSHELL)
        self.assertIn("retrying with anonymous credentials", POWERSHELL)
        self.assertIn("docker-credential-desktop.cmd", POWERSHELL)
        self.assertIn("$pullExitCode", POWERSHELL)
        self.assertIn("function Get-DockerImageId", POWERSHELL)
        self.assertIn("function Remove-SupersededDockerImage", POWERSHELL)
        self.assertIn("Removing superseded Docker image", POWERSHELL)

    def test_preflight_runs_before_the_build(self) -> None:
        self.assertLess(
            POWERSHELL.index("Ensure-DockerDaemon"),
            POWERSHELL.index("This creates a FULLY UNATTENDED installer."),
        )

    def test_bundle_script_is_mounted_as_a_file(self) -> None:
        self.assertIn(".autoinstall-bundle.tmp.sh", POWERSHELL)
        self.assertIn("bash /work/.autoinstall-bundle.tmp.sh", POWERSHELL)
        self.assertIn("sed 's/\\r$//' /work/offline/build-apt-bundle.sh", POWERSHELL)
        self.assertNotIn("$BuilderImage bash -euc $bundleScript", POWERSHELL)
        self.assertIn("Remove-Item $bundleScriptPath", POWERSHELL)

    def test_external_apt_rows_preserve_empty_components(self) -> None:
        self.assertIn("IFS=$'\\x1f' read -r name repository suite component key_url", BUNDLE)
        self.assertIn('print("\\x1f".join(', BUNDLE)
        self.assertIn("--http1.1 -fsSL", BUNDLE)
        self.assertIn("--retry 5 --retry-all-errors", BUNDLE)
        self.assertIn('"--showformat=${Package}\\n${Version}\\n${Architecture}\\n"', BUNDLE)
        self.assertIn('nvidia_prefix = "linux-modules-nvidia-595-open-"', BUNDLE)
        self.assertIn('linux-image-"):]', BUNDLE)
        self.assertIn('fallback_key="/work/offline/keys/${name}.asc"', BUNDLE)
        self.assertIn("BEGIN PGP PUBLIC KEY BLOCK", NVIDIA_KEY)
        self.assertIn("BEGIN PGP PUBLIC KEY BLOCK", NETBIRD_KEY)

    def test_batch_entrypoint_uses_powershell_script(self) -> None:
        self.assertIn("build-autoinstall-iso.ps1", BATCH)


if __name__ == "__main__":
    unittest.main()
