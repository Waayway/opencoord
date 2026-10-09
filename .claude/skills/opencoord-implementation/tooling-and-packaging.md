# Tooling & packaging (planned)

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

## Nix flake (`flake.nix`)
- Built with uv2nix + pyproject-nix + pyproject-build-systems, so the Nix build and devShell resolve from the **same
  `uv.lock`**.
- Outputs:
  - `packages.default` (wrapped with `makeWrapper`, adding `libGL`, X11/Wayland libs to `LD_LIBRARY_PATH` because the
    Dear PyGui wheel dlopens them)
  - `apps.default`
  - `devShells.default` (uv, python313, ruff, nixfmt, GL libs; `UV_PYTHON_DOWNLOADS=never`,
    `UV_PYTHON=${python}`)
  - `checks` (pytest)
  - `formatter`
- Systems: x86_64/aarch64 linux and darwin.
- The package installs a `.desktop` file, icon and udev rule under `$out/share` and `$out/lib/udev/rules.d`.

## Docker (`Dockerfile`)
Base: `ghcr.io/astral-sh/uv:python3.13-bookworm-slim`.

| Stage | Purpose |
|---|---|
| `test` | `uv sync --locked`, ruff, pytest (simulator) |
| `build` | `uv build` + PyInstaller onedir + AppImage via appimagetool (`--appimage-extract-and-run`, no FUSE in containers) |
| `artifacts` | `FROM scratch`, copies `dist/`. Use `docker build --target artifacts --output dist/ .` |
| `runtime` (default) | slim image + mesa/X11 libs; run with `--device /dev/ttyUSB0`, X11 socket mount |

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
| `ci.yml` | setup-uv → ruff, mypy, pytest matrix (ubuntu/windows/macos × 3.11–3.14); UI smoke test under `xvfb-run` |
| `build.yml` | builds per OS: ubuntu-22.04 (+ `-arm`), windows-latest, macos-14 (+ Intel if available); uploads artifacts |
| `nix.yml` | `nix flake check` + `nix build` on Linux/macOS |
| `docker.yml` | build/test stages on PR; on tag, push the runtime image to `ghcr.io/waayway/opencoord` |
| `release.yml` | on `v*` tag: collect all artifacts + `SHA256SUMS` into a GitHub Release; optional PyPI trusted publishing |

Rule: every shipped artifact must be reproducible from a clean GitHub runner. No manual release steps.
