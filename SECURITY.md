# Security policy

## What this project embeds

This kit builds a custom Ubuntu Server ISO. Values from your local `.env` —
including `PASSWORD` (hashed at build time), `SSH_PUBLIC_KEY`, and especially
`NETBIRD_SETUP_KEY` — are written into `autoinstall.yaml` and the generated
`.iso`.

Treat every generated ISO and every generated `autoinstall.yaml` as **secrets**.
Do not commit them, upload them to public releases, or share them on untrusted
media.

## Reporting a vulnerability

If you discover a security issue in this repository’s scripts or documentation
(for example a path that would leak setup keys into a public tree), please open
a [private security advisory](https://github.com/alexsvdk/netbird-ubuntu-autoinstall/security/advisories/new)
on GitHub, or contact the maintainer via GitHub if advisories are unavailable.

Please do **not** open a public issue that includes real setup keys, passwords,
private keys, or full generated autoinstall YAML.

## Operational guidance

1. Prefer **one-off** NetBird setup keys and revoke them after the peer enrolls.
2. Keep `.env` out of git (it is listed in `.gitignore`).
3. If an ISO may have been copied or exposed, rebuild with a new setup key and
   revoke the old key.
4. The installer **erases the largest non-installation disk** on the target
   machine — only boot media produced by this kit on hardware you intend to wipe.
