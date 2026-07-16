# Contributing

Thanks for improving this kit.

## Development setup

```bash
git clone https://github.com/alexsvdk/netbird-ubuntu-autoinstall.git
cd netbird-ubuntu-autoinstall
cp .env.example .env   # fill secrets only for real builds
python3 -m pip install -r requirements-dev.txt
```

## Checks before opening a pull request

1. Do not commit `.env`, `*.iso`, generated `autoinstall.yaml`, or `__pycache__`.
2. Keep secret fields in `.env.example` empty.
3. Run the unit tests (they exercise the real shipped scripts):

```bash
python3 -m unittest discover -s tests -v
```

4. Optionally syntax-check the builder:

```bash
bash -n build-autoinstall-iso.sh
python3 -m py_compile patch-grub.py render-autoinstall.py validate-autoinstall-iso.py
```

A full multi-GB ISO build is not required for docs-only changes. For logic
changes that touch autoinstall/GRUB generation, prefer the unit tests above
and a real `./build-autoinstall-iso.sh` when you can.

## Scope

Keep changes focused: packaging, docs, and build/render/validate behavior.
Avoid committing binary ISOs or personal credentials.
