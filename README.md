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

## Docker

```sh
docker build -t opencoord:dev .                          # runtime image (default stage)
docker build --target test .                             # lint + tests
docker build --target artifacts --output dist/ .         # wheel + sdist into ./dist
```

Run the GUI from the image (best-effort; Wayland works through XWayland, and you may need
`xhost +local:` first):

```sh
docker run --rm --device /dev/ttyUSB0 -e DISPLAY \
  -v /tmp/.X11-unix:/tmp/.X11-unix ghcr.io/waayway/opencoord
```

## License

GPL-3.0-or-later. See `LICENSE`.
