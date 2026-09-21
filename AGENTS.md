# Project Agent Instructions

## Environment variables must work through every build path

When adding or changing an environment variable used by rendering, provisioning, or ISO generation:

- Keep the default value in the renderer and document the variable in `.env.example` and the user instructions.
- Pass the variable through every build entry point: `build-autoinstall-iso.sh`, `build-autoinstall-iso.ps1`, `build-autoinstall-iso.bat`, and any Docker or subprocess boundary.
- Do not assume that a host `.env` variable is automatically visible inside Docker.
- Add or update a regression test that verifies the value reaches the generated YAML or embedded scripts.
- Test both the default value and at least one non-default value.

Before declaring a build-variable change complete, inspect the generated `autoinstall.yaml` or extracted ISO contents and confirm the configured value is present.
