# OpenCoord

Open-source spectrum scanner for RF Explorer devices and frequency coordinator for wireless
microphones and IEMs (intermod-free frequency plans). Built with Python and Dear PyGui.

**Status: pre-alpha.** Nothing is usable yet.

## Development

Requires [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run pytest
uv run ruff check && uv run ruff format --check && uv run mypy src
uv run opencoord --version
```

### Linux serial permissions

The RF Explorer shows up as `/dev/ttyUSB0` (CP2102N, `10c4:ea60`). Add your user to the group that
owns the port (`dialout` on Debian/Ubuntu, `uucp` on Arch) and log in again.

### Linux udev rule

Instead of joining a group, install the shipped udev rule, which grants the logged-in user access
to the RF Explorer (it is in `packaging/linux/`, in the `.tar.gz`, and inside the AppImage under
`usr/lib/udev/rules.d/`; extract it with `./OpenCoord-*.AppImage --appimage-extract`):

```sh
sudo cp 99-opencoord-rfexplorer.rules /etc/udev/rules.d/
sudo udevadm control --reload && sudo udevadm trigger
```

## Native builds

PyInstaller bundle plus the OS package, written to `dist/` (run on the matching OS):

```sh
uv run python packaging/build.py --appimage    # Linux:   AppImage + .tar.gz
uv run python packaging/build.py --installer   # Windows: Inno Setup installer + portable .zip
uv run python packaging/build.py --dmg         # macOS:   .dmg with OpenCoord.app
```

The `Build` GitHub workflow produces all of them on pull requests, tags and manual runs.

## Docker

```sh
docker build -t opencoord:dev .                          # runtime image (default stage)
docker build --target test .                             # lint + tests
docker build --target artifacts --output dist/ .         # wheel, sdist, Linux tar.gz + AppImage
```

Run the GUI from the image (best-effort; Wayland works through XWayland, and you may need
`xhost +local:` first):

```sh
docker run --rm --device /dev/ttyUSB0 -e DISPLAY \
  -v /tmp/.X11-unix:/tmp/.X11-unix ghcr.io/waayway/opencoord
```

## License

GPL-3.0-or-later. See `LICENSE`.
