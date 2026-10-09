# Tooling & packaging

## uv
- `.python-version` = `3.13` (release builds). CI also tests 3.11–3.14. `requires-python = ">=3.11"`.
- `uv.lock` is committed. CI and Docker use `uv sync --locked`.
- Dependencies: `dearpygui`, `pyserial`, `numpy`, `platformdirs`, `tomli-w`. Dev group: `pytest`, `hypothesis`,
  `ruff`, `mypy`, `pyinstaller`.
- Scripts: `opencoord = "opencoord.ui.app:main"`, `opencoord-cli = "opencoord.cli:main"`.
- Commands:
  - `uv sync`
  - `uv run pytest`
  - `uv run ruff check --fix && uv run ruff format`
  - `uv run mypy src`
  - `uv run opencoord --simulator`

## Project skeleton (implemented)
- Build backend: `hatchling` (`[tool.hatch.build.targets.wheel] packages = ["src/opencoord"]`); src layout.
- Tool config lives in `pyproject.toml`: ruff (line 100; rules E,F,W,I,UP,B,SIM,C4,RUF; relative imports banned),
  mypy (strict override for `opencoord.device.*`, `core.*`, `coord.*`), pytest markers `hardware` and `ui`.
- `tests/conftest.py` skips `hardware` tests unless `OPENCOORD_HARDWARE=1`; `tests/ui/test_smoke.py` skips without
  `DISPLAY`/`WAYLAND_DISPLAY`.
- `opencoord --smoke-frames N` renders N frames and exits 0 (CI / packaging smoke test).
- Only `src/opencoord/ui/app.py` exists in `ui/` so far; other planned modules are not written yet.

## Nix flake (`flake.nix`, implemented)
- Built with uv2nix + pyproject-nix + pyproject-build-systems (all `follows` nixpkgs = `nixos-unstable`), so the Nix
  build and devShell resolve from the **same `uv.lock`**. `mkPyprojectOverlay { sourcePreference = "wheel"; }` +
  `pyproject-build-systems.overlays.wheel`, Python `python313`. `flake.lock` is committed (`nix flake lock` to bump).
- Systems: `x86_64-linux`, `aarch64-linux`, `aarch64-darwin`. **`x86_64-darwin` is not offered**: nixpkgs 26.11
  (nixos-unstable) dropped it and evaluating `legacyPackages.x86_64-darwin` throws.
- Wheel fixups (`pyprojectOverrides`, Linux only): `dearpygui` gets `autoPatchelfHook` + `libx11` +
  `stdenv.cc.cc.lib` (its `.so` links `libX11.so.6` and `libstdc++` directly). GLFW dlopens the rest.
- Outputs:
  - `packages.default` = `packages.opencoord`: venv `opencoord-env` (`workspace.deps.default`, no dev tools) wrapped
    with `makeWrapper` into `$out/bin/opencoord` and `$out/bin/opencoord-cli`. On Linux the wrapper **prepends**
    `libGL` (glvnd), `libx11`, `libxrandr`, `libxinerama`, `libxcursor`, `libxi`, `libxext`, `libxkbcommon`,
    `wayland` to `LD_LIBRARY_PATH` and **appends** `/run/opengl-driver/lib` then nixpkgs `mesa/lib`. The mesa
    fallback is what makes `nix run` work on non-NixOS hosts (glvnd otherwise finds no GLX vendor:
    `GLX: No GLXFBConfigs returned` → assertion crash). Cost: mesa adds ~1 GiB to the closure (total ~1.3 GiB).
    Installs `packaging/linux/opencoord.desktop` → `$out/share/applications/` and
    `packaging/linux/99-opencoord-rfexplorer.rules` → `$out/lib/udev/rules.d/` (all systems). No icon yet.
  - `apps.default`: `nix run` launches the GUI.
  - `devShells.default`: `uv`, `python313`, `ruff`, `nixfmt`; `UV_PYTHON_DOWNLOADS=never`,
    `UV_PYTHON=${python.interpreter}`; on Linux `LD_LIBRARY_PATH` = runtime libs + `libstdc++` (uv's unpatched
    wheel) + the same GL fallback. `nix develop -c uv run opencoord --smoke-frames 5` works. Note: uv in the shell
    recreates `.venv` on the Nix Python.
  - `checks.<system>.pytest`: venv with `workspace.deps.default // { pytest = [ ]; hypothesis = [ ]; }` (not the
    whole dev group, so no pyinstaller/mypy), source = `pyproject.toml` + `tests/` only, runs
    `pytest -m "not ui and not hardware" -p no:cacheprovider`.
  - `formatter`: `pkgs.nixfmt` (the RFC-style formatter; `nixfmt-rfc-style` is now a deprecated alias). `nix fmt`.
- Local verification: `nix build && ./result/bin/opencoord --smoke-frames 5`, `nix flake check -L`,
  `nix flake check --all-systems --no-build` (evaluates the darwin/aarch64 outputs without building).
- New files must be `git add`ed before the flake can see them.

## Linux desktop files (`packaging/linux/`, implemented)
- `opencoord.desktop` (passes `desktop-file-validate`; `Exec=opencoord`, `Icon=opencoord`).
- `99-opencoord-rfexplorer.rules`: `SUBSYSTEM=="tty"`, CP210x `10c4:ea60`, `MODE="0660"`, `TAG+="uaccess"`.

## Docker (`Dockerfile`, `.dockerignore`, implemented)
Base: `ghcr.io/astral-sh/uv:python3.13-bookworm-slim` (build arg `UV_IMAGE`). Needs BuildKit/buildx (`--mount`,
`--output`); on a host without `docker buildx`, drop the `docker-buildx` binary into `~/.docker/cli-plugins/`.

| Stage | Purpose |
|---|---|
| `test` | copies pyproject/uv.lock/README/LICENSE/src/tests; `uv sync --locked` (venv at `/opt/venv`), `ruff check`, `ruff format --check`, `pytest -m "not ui and not hardware"` |
| `build` | `uv build --out-dir /out/dist` (wheel + sdist). `TODO(Task 5)` marker: add PyInstaller onedir + AppImage (appimagetool with `--appimage-extract-and-run`, no FUSE) writing to `/out/dist` |
| `artifacts` | `FROM scratch`, copies `/out/dist/` to `/`. `docker build --target artifacts --output dist/ .` |
| `runtime` (last = default) | uv base + mesa/X11 apt libs; venv `/opt/venv` with the built wheel (bind-mounted from `build`), `ENTRYPOINT ["opencoord"]`; `opencoord-cli` is reachable with `--entrypoint` |

GUI run command (README, best-effort): `docker run --rm --device /dev/ttyUSB0 -e DISPLAY -v /tmp/.X11-unix:/tmp/.X11-unix ghcr.io/waayway/opencoord`
(`xhost +local:` may be needed). `.dockerignore` excludes `.git`, `.github`, `.superpowers`, `.claude`, `plans`, `.venv`,
`result`, `dist`, `build`, caches, and the flake files.

## PyInstaller & OS packaging
- One spec: `packaging/opencoord.spec` (onedir; datas: channel plans, profile templates, icons). On macOS it adds a
  `BUNDLE` → `OpenCoord.app`.
- Windows: `packaging/windows/opencoord.iss` (Inno Setup) → `OpenCoord-<ver>-win64-setup.exe` plus a portable zip.
  Adds a `.opencoord` file association.
- macOS: `hdiutil create` → DMG; ad-hoc `codesign -s -`; notarisation only if secrets are present.
- Linux: AppDir (`.desktop`, icon, AppStream XML) → `appimagetool`. Built on ubuntu-22.04 for an old glibc baseline.
  Ships `packaging/linux/99-opencoord-rfexplorer.rules`.
- Icons: `packaging/icons/opencoord.svg` → script generates `.ico`, `.icns`, PNGs.
- Version: single source in `pyproject.toml`, read at runtime via `importlib.metadata`. Release tags are `vX.Y.Z`.

## GitHub Actions (`.github/workflows/`)
| Workflow | What it runs |
|---|---|
| `ci.yml` | on push/PR, cancel-in-progress per ref. Jobs: `lint` (ruff check, ruff format --check, mypy); `test` (ubuntu/windows/macos x py 3.11-3.14, `uv sync --locked --python X` + `uv run --python X pytest -m "not ui and not hardware"`, overrides `.python-version`); `ui-smoke` (apt xvfb + mesa/X11 libs, `xvfb-run -a uv run pytest -m ui`). Actions: checkout@v4, setup-uv@v5 (cache on) |
| `build.yml` | builds per OS: ubuntu-22.04 (+ `-arm`), windows-latest, macos-14 (+ Intel if available); uploads artifacts |
| `nix.yml` | on push/PR, cancel-in-progress per ref; ubuntu-latest + macos-latest: DeterminateSystems `nix-installer-action` + `magic-nix-cache-action`, `nix flake check -L`, `nix build -L`, `./result/bin/opencoord --version` |
| `docker.yml` | push/PR/tags `v*`: buildx + build-push-action (GHA cache, per-target scopes): `test` target, `runtime` target (loaded, `--version` check), `artifacts` target (`outputs: type=local,dest=dist`, uploaded as `docker-dist`); on `v*` tags also login + metadata + push runtime to `ghcr.io/waayway/opencoord` (`packages: write`) |
| `release.yml` | on `v*` tag: collect all artifacts + `SHA256SUMS` into a GitHub Release; optional PyPI trusted publishing |

Rule: every shipped artifact must be reproducible from a clean GitHub runner. No manual release steps.
