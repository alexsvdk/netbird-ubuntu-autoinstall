# NetBird Ubuntu Autoinstall

Public kit that builds an **Ubuntu Server 24.04.4** ISO with **NetBird** enrollment and unattended install.

```bash
git clone https://github.com/alexsvdk/netbird-ubuntu-autoinstall.git
cd netbird-ubuntu-autoinstall
cp .env.example .env   # fill secrets — never commit .env
```

This kit builds an Ubuntu Server 24.04.4 ISO that:

- erases the largest non-installation disk;
- installs Ubuntu Server without questions;
- creates a Linux user;
- enables OpenSSH;
- disables SSH password login;
- adds your SSH public key;
- grants that user passwordless sudo;
- installs Docker Engine (`docker.io`) and Compose v2 after first boot, retrying while package mirrors become available;
- adds the user to the `docker` group (run `docker` without sudo);
- provides `compose` as a shorthand for `docker compose`;
- disables sleep/hibernate;
- powers off after installation instead of rebooting into the USB installer again;
- installs and enrolls NetBird on the first boot of the installed system, with the chosen hostname as the peer name;
- retries NetBird enrollment until the network is available and records its progress in `/var/log/netbird-enroll.log`;
- sends best-effort installation status notifications to the configured `ntfy.sh` topic;
- limits each Docker container's logs to 10 rotated files × 10 MiB (about 100 MiB);
- stores the system journal persistently and caps it at about 100 MiB with automatic rotation and cleanup;
- enables UFW with OpenSSH allowed and full trust for NetBird traffic on `wt0` (in and out).

## Requirements

- macOS, Linux, or Windows 10/11
- Docker Desktop / Docker Engine
- about 8 GB free disk space
- Ethernet on the target server
- a NetBird **one-off** setup key
- your SSH public key

## Configuration (`.env`)

Copy the example and fill in secrets/settings:

```bash
cp .env.example .env
```

Supported variables (see `.env.example`):

| Variable | Description |
| --- | --- |
| `UBUNTU_VERSION` | Ubuntu live-server release (default `24.04.4`) |
| `ARCH` | Target CPU architecture: `amd64` (default, x86_64) or `arm64` (aarch64). Aliases: `x86_64`/`x64`, `aarch64`/`arm` |
| `ISO_MIRROR` | Source ISO download: `auto` (default, speed-test CD mirrors), `default` (Canonical only), base mirror URL, or full `.iso` URL |
| `ISO_URL` | Full source ISO URL (overrides `ISO_MIRROR`) |
| `OUTPUT_ISO` | Output ISO filename or absolute host path (default `ubuntu-${UBUNTU_VERSION}-autoinstall-${ARCH}.iso`) |
| `HOSTNAME` / `TARGET_HOSTNAME` | Target hostname (from `.env` or `TARGET_HOSTNAME`; shell `HOSTNAME` alone is ignored) |
| `USERNAME` | Linux username |
| `PASSWORD` | Console password |
| `SSH_PUBLIC_KEY` | SSH public key line |
| `NETBIRD_SETUP_KEY` | NetBird one-off setup key. It is embedded in the generated ISO; keep that ISO private and revoke the key after enrollment. |
| `APT_REGION` | Mirror strategy: `auto` (default, geoip), `default` (global archive), 2-letter country code (`ru`, `de`, `nl`, …), or `custom` |
| `APT_MIRROR` | Optional preferred primary mirror URI (prepended to candidates) |
| `APT_SECURITY_MIRROR` | Optional security-pocket mirror URI |
| `APT_FALLBACK` | If no mirror works: `offline-install` (default), `abort`, or `continue-anyway` |
| `OFFLINE_BUNDLE_REFRESH` | Local APT bundle refresh mode: `auto` (default) updates metadata and downloads only missing/newer `.deb` files; `never` requires a complete existing cache |
| `OFFLINE_BUNDLE_CACHE` | Project-relative persistent cache path (default `offline/packages/${UBUNTU_VERSION}-${ARCH}`) |
| `NOTIFY_TOPIC` | ntfy.sh topic for installation status notifications (default `samovar_test`) |

Non-empty values from `.env` (or the shell, except bare `HOSTNAME`) skip the matching interactive prompt. Empty values still prompt at build time.

To receive progress updates, install the **ntfy** app and subscribe to the same `NOTIFY_TOPIC`. The default topic is `samovar_test`; use a unique topic for a real installation.

### APT mirrors by region

During install and on the installed system, Subiquity picks the first working primary mirror from a candidate list.

Examples in `.env`:

```bash
# Fast path for Russia (official country mirror + global fallback)
APT_REGION=ru

# Custom mirror first (e.g. Yandex), then archive.ubuntu.com
APT_REGION=custom
APT_MIRROR=http://mirror.yandex.ru/ubuntu

# Germany + optional explicit security mirror
APT_REGION=de
# APT_SECURITY_MIRROR=http://security.ubuntu.com/ubuntu

# Leave defaults: geoip country-mirror, then archive.ubuntu.com
APT_REGION=auto
```

Official country mirrors use `http://XX.archive.ubuntu.com/ubuntu` (Launchpad mirror list: https://launchpad.net/ubuntu/+archivemirrors).

`.env` is gitignored — do not commit secrets.

### Offline APT bundle

Each build embeds a local flat APT repository at `/samovar-offline-apt` in the
generated ISO. During installation it is copied to
`/var/lib/samovar-offline-apt` on the target; first-boot provisioning installs
its Ubuntu packages from that repository before attempting a network mirror.

The persistent cache is ignored by Git. With the default
`OFFLINE_BUNDLE_REFRESH=auto`, the builder checks APT metadata but reuses cached
`.deb` files and only downloads packages that are missing or have a newer
candidate. Use `OFFLINE_BUNDLE_REFRESH=never` to build without contacting APT;
it fails if the cache is missing or its root package lock has changed.

## Build

**macOS / Linux / Windows (Git Bash or WSL):**
```bash
chmod +x build-autoinstall-iso.sh
./build-autoinstall-iso.sh
```

**Windows (PowerShell):**
```powershell
.\build-autoinstall-iso.ps1
```

**Windows (CMD / Command Prompt / Double-click):**
```cmd
build-autoinstall-iso.bat
```

The script loads `.env` if present, then downloads the official Ubuntu Server ISO for the chosen `ARCH` if it is not already in the project directory.

With `ISO_MIRROR=auto` (default), the builder probes popular CD mirrors in parallel (~2 MiB range request each), prints measured speeds, and downloads from the fastest host that has the file. Fallbacks:

- `ISO_MIRROR=default` — Canonical only (`releases.ubuntu.com` / `cdimage.ubuntu.com`)
- fixed base, e.g. `ISO_MIRROR=https://mirror.yandex.ru/ubuntu-releases`
- full URL via `ISO_URL` or `ISO_MIRROR=https://…/ubuntu-….iso`

APT package mirrors on the *installed* system still follow `ARCH` and `APT_*` (`archive.ubuntu.com` for amd64, `ports.ubuntu.com` for arm64).

## Flash

Write the generated ISO to USB using Balena Etcher, Rufus, or Raspberry Pi Imager.

## Target installation

1. Connect Ethernet and power.
2. Boot from USB and wait for Ubuntu to install. At the end it powers off deliberately; it does not reboot.
3. Remove the USB drive, then turn the server on again. This is the first boot of the installed system.
4. Wait a few minutes, then check the NetBird dashboard. The one-off setup key is consumed only at this point.
5. If the peer is absent, log in locally or through the LAN and run `sudo tail -n 100 /var/log/netbird-enroll.log` and `sudo journalctl -u netbird-enroll --no-pager`.
6. Revoke/delete the setup key after enrollment.

### Log retention

Docker uses the `local` logging driver with a 10 MiB file size and 10 files per container. The persistent system journal is limited to roughly 100 MiB. On a fresh installation this applies before Docker is started.

Check the configured driver and journal usage after boot:

```bash
docker info --format '{{.LoggingDriver}}'
journalctl --disk-usage
```

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m pytest tests/ -v
```

## Samovar Autoinstall (`samovar` server profile)

This repository includes a specialized, hardened autoinstall target for the **`samovar`** home lab server.
Features include:
- **Disk mapping by exact serial numbers** (Kingston SSD, SBSSD, WD HDD) — no destructive size-based matching.
- **Fail-closed preflight hardware verification** before any disk partitioning.
- **Detached SSH-signed configuration** (`samovar-config.json` + `samovar-config.json.sig`).
- **Emergency USB recovery agent** (`SAMOVARCFG` FAT32 drive) with transactional rollbacks and replay protection.
- **Isolated host management plane** (SSH, NetBird, recovery operate directly; Mihomo proxy/TUN for workloads).
- **Docker + NVIDIA RTX 3060 provisioning** with Compose templates for Direct, Proxy, and Full TUN egress.
- **Per-interface firewall rules** (SSH strictly on `wt0`, `wifi0`, and `lan0`).

For detailed end-to-end instructions, see **[INSTRUCTIONS.md](INSTRUCTIONS.md)** and the technical specification in [samovar-autoinstall-spec.md](samovar-autoinstall-spec.md).

## License

MIT — see [LICENSE](LICENSE). Security notes: [SECURITY.md](SECURITY.md).

## Important

The installer intentionally erases the **largest internal disk**. The generated ISO includes the NetBird setup key, so treat the ISO and any generated `autoinstall.yaml` as **secrets** and rebuild with a new key if they have been copied or exposed.

Automatic power-on after a power outage is a BIOS/UEFI setting, commonly named:

- Restore on AC Power Loss
- AC Back
- After Power Failure

Set it to `Power On`.
