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
- `opencoord --smoke-frames N` renders N frames and exits 0 (CI / packaging smoke test). In smoke mode
  `auto_connect` is forced off and settings are not saved, so it never touches a device or the user's config.
  `opencoord --simulator` uses the simulated device; `-v` enables debug logging.
- The UI modules are listed in `ui.md`; `tests/ui/test_smoke.py` runs `--smoke-frames` in a subprocess because
  Dear PyGui segfaults on a second viewport in one process.

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
    `packaging/linux/99-opencoord-rfexplorer.rules` → `$out/lib/udev/rules.d/` (all systems). The flake does not install the icon yet
    (`packaging/icons/opencoord-*.png` exist; hicolor install is a TODO).
  - `apps.default`: `nix run` launches the GUI.
  - `devShells.default`: `uv`, `python313`, `ruff`, `nixfmt`; `UV_PYTHON_DOWNLOADS=never`,
    `UV_PYTHON=${python.interpreter}`; on Linux `LD_LIBRARY_PATH` = runtime libs + `libstdc++` (uv's unpatched
    wheel) + the same GL fallback. `nix develop -c uv run opencoord --smoke-frames 5` works. Note: uv in the shell
    recreates `.venv` on the Nix Python.
  - `checks.<system>.pytest`: venv with `workspace.deps.default // { pytest = [ ]; hypothesis = [ ]; }` (not the
    whole dev group, so no pyinstaller/mypy), source = `pyproject.toml` + `tests/` + `profiles/` only (the community-profile test reads `profiles/`), runs
    `pytest -m "not ui and not hardware" -p no:cacheprovider`.
  - `nixosModules.default`: `programs.opencoord.enable` (+ `package`, default the flake package) adds the package to
    `environment.systemPackages` and `services.udev.packages` (the package ships `lib/udev/rules.d/`); the rule
    uses `TAG+="uaccess"`, so no group is created or needed. Checked by `nix flake check` and a NixOS eval.
  - `formatter`: `pkgs.nixfmt` (the RFC-style formatter; `nixfmt-rfc-style` is now a deprecated alias). `nix fmt`.
- Local verification: `nix build && ./result/bin/opencoord --smoke-frames 5`, `nix flake check -L`,
  `nix flake check --all-systems --no-build` (evaluates the darwin/aarch64 outputs without building).
- New files must be `git add`ed before the flake can see them.

## Linux desktop files (`packaging/linux/`, implemented)
- `opencoord.desktop` (passes `desktop-file-validate`; `Exec=opencoord`, `Icon=opencoord`).
- `99-opencoord-rfexplorer.rules`: `SUBSYSTEM=="tty"`, CP210x `10c4:ea60`, `MODE="0660"`, `TAG+="uaccess"`.
- `io.github.waayway.opencoord.metainfo.xml`: AppStream metadata (passes `appstreamcli validate`), used by the
  AppImage. `make-appimage.sh` lives here too (see below).

## Docker (`Dockerfile`, `.dockerignore`, implemented)
Base: `ghcr.io/astral-sh/uv:python3.13-bookworm-slim` (build arg `UV_IMAGE`). Needs BuildKit/buildx (`--mount`,
`--output`); on a host without `docker buildx`, drop the `docker-buildx` binary into `~/.docker/cli-plugins/`.

| Stage | Purpose |
|---|---|
| `test` | apt `libx11-6 libgl1` (UI tests import dearpygui), copies pyproject/uv.lock/README/LICENSE/src/tests/profiles; `uv sync --locked` (venv at `/opt/venv`), `ruff check`, `ruff format --check`, `pytest -m "not ui and not hardware"` |
| `build` | apt `binutils ca-certificates curl file libgl1 libx11-6`; `uv build --out-dir /out/dist` (wheel + sdist); then `COPY packaging`, `uv sync --locked`, `packaging/build.py --appimage` and copy `OpenCoord-*.tar.gz` + `OpenCoord-*.AppImage` to `/out/dist` (needs network for appimagetool + runtime) |
| `artifacts` | `FROM scratch`, copies `/out/dist/` to `/`. `docker build --target artifacts --output dist/ .` |
| `runtime` (last = default) | uv base + mesa/X11 apt libs; venv `/opt/venv` with the built wheel (bind-mounted from `build`), `ENTRYPOINT ["opencoord"]`; `opencoord-cli` is reachable with `--entrypoint` |

GUI run command (README, best-effort): `docker run --rm --device /dev/ttyUSB0 -e DISPLAY -v /tmp/.X11-unix:/tmp/.X11-unix ghcr.io/waayway/opencoord`
(`xhost +local:` may be needed). `.dockerignore` excludes `.git`, `.github`, `.superpowers`, `.claude`, `plans`, `.venv`,
`result`, `dist`, `build`, caches, and the flake files.

## PyInstaller & OS packaging (implemented)
Single entrypoint: `uv run python packaging/build.py [--appimage|--installer|--dmg]` (after `uv sync`; PyInstaller
is in the dev group). It reads the version from `pyproject.toml` (tomllib), runs PyInstaller with the spec
(`--distpath dist/pyinstaller --workpath build/pyinstaller --clean`), then the OS step, and prints artifact paths.
The flag must match the host OS. Artifacts land directly in `dist/`:

| OS | Flag | Artifacts |
|---|---|---|
| Linux | `--appimage` | `OpenCoord-<ver>-<arch>.AppImage`, `OpenCoord-<ver>-linux-<arch>.tar.gz` (top dir `OpenCoord/` = onedir + udev rule + `.desktop` + png) |
| Windows | `--installer` | `OpenCoord-<ver>-win64-setup.exe`, `OpenCoord-<ver>-win64-portable.zip` |
| macOS | `--dmg` | `OpenCoord-<ver>-macos-arm64.dmg` (`OpenCoord.app` + `/Applications` symlink) |

`<arch>` is `platform.machine()` normalised (`AMD64`→`x86_64`, `arm64`→`aarch64`).

- **Spec** `packaging/opencoord.spec`: onedir, name `OpenCoord`, entry `src/opencoord/__main__.py`
  (`pathex=src`). `copy_metadata("opencoord")` so `importlib.metadata.version` (hence `__version__` /
  `--version`) works frozen; datas `collect_data_files("opencoord", includes=["**/*.toml"])`; hidden imports
  `collect_submodules("dearpygui")` + `("opencoord")`; binaries `collect_dynamic_libs("dearpygui")` (empty on
  Linux, `_dearpygui` is an extension module found by import analysis). `console=False` on Windows/macOS (windowed:
  `--version` prints nothing on Windows), `True` on Linux. Icon `.ico` (Windows) / `.icns` (macOS).
  macOS `BUNDLE` → `OpenCoord.app`, bundle id `io.github.waayway.opencoord`, numeric plist version (`0.1.0.dev0` →
  `0.1.0`), `CFBundleDocumentTypes` for `.opencoord`.
- **Linux host libs are not bundled** (spec filters `a.binaries`): `libstdc++`, `libgcc_s`, `libX11*`, `libXau`,
  `libXdmcp`, `libxcb*`, `libGL*`. A bundled libstdc++ from the older build distro (bookworm / ubuntu-22.04) breaks
  the host's Mesa drivers: `GLX: No GLXFBConfigs returned` then a glfw assertion crash. The bundle keeps only
  `libpython`, dearpygui and stdlib extension deps.
- **AppImage** `packaging/linux/make-appimage.sh <onedir> <version> <outdir>`: AppDir in `build/appimage/` with
  onedir at `usr/lib/opencoord/`, `AppRun` (sh, execs `usr/lib/opencoord/OpenCoord "$@"`), desktop file copied as
  `io.github.waayway.opencoord.desktop` (top level + `usr/share/applications/`; id must match the AppStream
  `<launchable>`), `packaging/linux/io.github.waayway.opencoord.metainfo.xml` installed as
  `usr/share/metainfo/io.github.waayway.opencoord.appdata.xml` (appimagetool only looks for the legacy `.appdata.xml`
  name and validates it with `appstreamcli` when present), 256px icon (`opencoord.png`, `.DirIcon`, hicolor), udev
  rule under `usr/lib/udev/rules.d/` (informational; README says how to install it). Downloads
  `appimagetool-<arch>.AppImage` from the `continuous` release (cached in `build/appimage/`; needs `curl`, `file`) and
  runs it with `--appimage-extract-and-run`, `ARCH=<arch>`. appimagetool downloads the type2 runtime too, so the build
  needs network. Run the result without FUSE via `APPIMAGE_EXTRACT_AND_RUN=1` or `--appimage-extract-and-run`.
- **Windows** `packaging/windows/opencoord.iss` (Inno Setup 6, `ArchitecturesAllowed=x64compatible`): defines
  `AppVersion` (required), `BundleDir`, `OutputDir` via `/D…`; installs to `{autopf}\OpenCoord` (admin), Start-menu
  shortcut + CP210x driver `.url` shortcut, optional desktop shortcut (unchecked task), uninstaller, `.opencoord` →
  `OpenCoord.Session` ProgID (`HKA`, `"…\OpenCoord.exe" "%1"`, `ChangesAssociations=yes`). Fixed `AppId` GUID —
  never change it. `build.py` finds `ISCC.exe` on PATH or in `Program Files (x86)\Inno Setup 6|7`.
- **macOS** `packaging/macos/make-dmg.sh <app> <dmg>`: `codesign --force --deep -s -` + `--verify`, staging dir with
  the app and an `/Applications` symlink, `hdiutil create -format UDZO -fs HFS+`. Not notarised (Gatekeeper asks on
  first launch).
- `opencoord <session>`: `ui/app.py` accepts an optional positional `session` path (`Path`) so file-association
  launches parse; opening it is not implemented yet.
- **Icons** `packaging/icons/`: source `opencoord.svg` (spectrum with three peaks). `uv run packaging/icons/generate.py`
  (PEP 723 inline deps `pillow`, `resvg-py`; not project deps) writes `opencoord-{16..512}.png`, `opencoord.png`
  (256), `opencoord.ico` (16–256), `opencoord.icns` (from 1024). Outputs are committed; rerun after editing the SVG.
- Built on ubuntu-22.04 runners (glibc 2.35) for an old baseline; the Docker build stage uses bookworm (glibc 2.36).
- Version: single source in `pyproject.toml`, read at runtime via `importlib.metadata`. Release tags are `vX.Y.Z`.

## GitHub Actions (`.github/workflows/`)
| Workflow | What it runs |
|---|---|
| `ci.yml` | on push/PR, cancel-in-progress per ref. Jobs: `lint` (ruff check, ruff format --check, mypy); `test` (ubuntu/windows/macos x py 3.11-3.14, `uv sync --locked --python X` + `uv run --python X pytest -m "not ui and not hardware"`, overrides `.python-version`); `ui-smoke` (apt xvfb + mesa/X11 libs, `xvfb-run -a uv run pytest -m ui`). Actions: checkout@v4, setup-uv@v5 (cache on) |
| `build.yml` | PR, `workflow_dispatch`, tags `v*`, `workflow_call` (used by `release.yml`); cancel-in-progress per ref; matrix `ubuntu-22.04` / `ubuntu-22.04-arm` / `windows-latest` / `macos-14`: `uv sync --locked`, `packaging/build.py --appimage|--installer|--dmg`, smoke `--smoke-frames 5` (Linux: apt xvfb + mesa, onedir and AppImage under `xvfb-run -a` with `APPIMAGE_EXTRACT_AND_RUN=1`; Windows: `choco install innosetup` if missing, `Start-Process -Wait -PassThru` on the onedir exe, then silent install `/VERYSILENT` and smoke the installed exe; macOS: `OpenCoord.app/Contents/MacOS/OpenCoord`). Uploads `opencoord-linux-x86_64`, `opencoord-linux-aarch64`, `opencoord-windows-x86_64`, `opencoord-macos-arm64`. No Intel macOS job |
| `nix.yml` | on push/PR, cancel-in-progress per ref; ubuntu-latest + macos-latest: DeterminateSystems `nix-installer-action` + `magic-nix-cache-action`, `nix flake check -L`, `nix build -L`, `./result/bin/opencoord --version` |
| `docker.yml` | push/PR/tags `v*`: buildx + build-push-action (GHA cache, per-target scopes): `test` target, `runtime` target (loaded, `--version` check), `artifacts` target (`outputs: type=local,dest=dist`: wheel, sdist, Linux tar.gz + AppImage; uploaded as `docker-dist`); on `v*` tags also login + metadata + push runtime to `ghcr.io/waayway/opencoord` (`packages: write`) |
| `release.yml` | on `v*` tag: `build` (calls `build.yml` via `workflow_call`), `dist` (checks the tag equals the `pyproject.toml` version, `uv build`, uploads `opencoord-python`), `release` (downloads every `opencoord-*` artifact with `merge-multiple`, writes `SHA256SUMS`, takes the notes from the `## [<version>]` section of `CHANGELOG.md` with awk, `gh release create --draft`, `--prerelease` for tags containing `-`), `pypi` (trusted publishing via `pypa/gh-action-pypi-publish`, **disabled with `if: false`**, setup steps in a comment; plan Q11). Nothing is published automatically: the draft is reviewed by hand. The `if: false` lint note is silenced in `.github/actionlint.yaml`. Validate workflows with `uvx --from actionlint-py actionlint` |

Rule: every shipped artifact must be reproducible from a clean GitHub runner. No manual release steps.

## Release readiness files (Task 23)
- `README.md` (features, install per OS, supported models, quick start, legal notice), `CONTRIBUTING.md` (incl. how to
  add a channel plan / profile / model, the skill-update rule, release steps), `CHANGELOG.md` (`## [0.1.0] - unreleased`;
  `release.yml` extracts the section for the tag), `.github/ISSUE_TEMPLATE/{bug,device_profile,channel_plan,config}.yml`.
- `profiles/`: community device profiles (`README.md` + `example-70cm-amateur-handheld.toml`);
  `tests/coord/test_community_profiles.py` parses every `profiles/*.toml` with `parse_profile` and the built-in presets.
- Screenshots: `docs/screenshots/{spectrum,analysis,coordination}.png` (< 500 KB) come from the simulator via
  `uv run python docs/make_screenshots.py` (run under `xvfb-run -a` on a headless box; it drives `App` and uses
  `App.capture_window`). Regenerate when the UI changes visibly.
- The version stays `0.1.0.dev0` until the maintainer bumps it and tags; `release.yml` refuses a tag that differs
  from the `pyproject.toml` version.
