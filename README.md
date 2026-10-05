# Visionect for Home Assistant

Drive Visionect e-ink signs from Home Assistant **without the Visionect Software
Suite**. No vendor server, no licence server, no Postgres, no Redis, no WebKit —
Home Assistant hosts the device-facing listener itself, in-process. No add-on and
no sidecar: the integration ships no binaries and spawns no subprocesses.

Built on [`pyvisionect`](https://github.com/schlarpc/pyvisionect), a sans-io
reimplementation of the device wire protocol.

## How it works, and why that shapes everything

The sign is a **TCP client**. It dials out to port 11113 and speaks first, with
no handshake and no authentication — its UUID is the whole identity claim. So:

- Home Assistant **hosts a listener**; there is nothing to poll and no coordinator fetch loop.
- There is **no discovery**, in either direction. The sign must be *pointed at*
  Home Assistant, over its USB serial console or by re-pointing a DNS name it
  already holds. The integration can never find a sign by itself.
- **Nothing can wake the device.** Between contacts the radio and MCU are down,
  so every command is deferred until the sign next connects. A coalescing work
  queue sits behind the services, and `sensor.*_next_contact` tells you when your
  change will actually appear.

## Install

Copy `custom_components/visionect/` into your Home Assistant `config` directory,
or add this repository to HACS as a custom integration. Then add **Visionect**
from *Settings → Devices & Services* and choose a port (11113 by default).

Point the sign at Home Assistant over its USB serial console:

```
server_tcp_set <home-assistant-ip> 11113
flash_save
cs 1
cs 3
```

`cs 3` on its own does nothing while a session is already open — `cs 1` first is
what tears the old socket down. The sign will also re-dial by itself roughly an
hour after `flash_save` if you would rather wait.

> **Keep the listener up.** If nothing answers on the configured address this
> firmware eventually power-cycles itself (`E: Max conn errs. Reboot`), and
> `ErrorCode` stays `0` throughout, so device status will not tell you it is
> happening.

## What you get

Entities created from the device's own status packet rather than a static list,
so you do not get a screenful of permanently-unavailable sensors: battery,
voltage, current, signal strength, temperature, uptime, last contact, next
contact, display updates, filesystem free space, charging, connected, and more.
Plus an `image` entity showing what is on the screen, buttons, dither/encoding
selects, and 12 services — `display_image`, `display_text`, `set_content_source`,
TCLV parameter read/write, device file listing and readback.

## Partial screen updates

**Off by default. Opt in per sign, under *Configure → Partial screen updates*.**

Normally every push is the whole screen: 1.84 MB of pixels, ~78 KB on the wire,
and 2.9–5.3 s on the glass. With partial updates on, a change to one corner of
a dashboard goes out as one small rectangle instead — measured on the real sign
at **1.5 KB against 58 KB**, drawing in **1.7 s against 2.9 s**.

That is bandwidth and CPU. It is **not** continuous or animated updates, and
nothing here promises that: the panel's waveform has a floor of well over a
second whatever the area.

Two things to know before you turn it on.

**The ghosting policy is load-bearing.** Measured on the hardware, this firmware
clears *nothing* by itself — across eight partial pushes it never once ran a
clearing refresh of its own accord. Only a full-screen push asks for one. So the
integration forces a full push every N partials (default 10, the vendor's own
limit), and that forced push is the only thing cleaning the panel. Watch
`sensor.<sign>_partials_since_refresh`; its attributes carry the whole policy.

**Your sign may not be offered it.** There is no way to ask a sign whether it
takes a rectangle. The list comes from hardware that was actually measured doing
so, one physical sign at a time, so an unverified sign is simply not on it.

## Development

### Running the tests

The suite runs against a **fake sign that replays verbatim captured traffic**
from a real 31.2" panel, through the real listener on a real socket — not
against mocks of the protocol.

```sh
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python -r requirements_test.txt
uv pip install --python .venv/bin/python -e ../pyvisionect  # not on PyPI yet

.venv/bin/python -m pytest tests/ -q
.venv/bin/python -m pytest tests/ --cov=custom_components.visionect --cov-report=term-missing
```

`pytest-homeassistant-custom-component` installs an exact Home Assistant
version, so `requirements_test.txt` pins it: changing that pin re-tests a
different core, which is the point of it being a pin.

Run `pytest` from the repository root. Home Assistant finds a custom
integration through the importable `custom_components` package, so the working
directory is load-bearing.

### Deploying, and keeping the copies honest

There is more than one copy of this integration on the author's machine — the
repository, the running Home Assistant, an archive — and they had already
drifted. So copying is a script, and the script can also just look:

```sh
scripts/sync.sh --check                      # report drift, change nothing
scripts/sync.sh ~/hass-test/config/custom_components/visionect
scripts/sync.sh --library ../pyvisionect <dest>   # and the library beside it
```

Destinations can be listed one per line in `.sync-targets` (gitignored, since
they are local paths) instead of being passed each time. `--library` also
refreshes the hand-copied `pyvisionect` under the target's `deps/`, which is
how that copy came to be missing a whole module.

The script regenerates `translations/en.json` from `strings.json` before
copying, because for a custom integration the one is a copy of the other and it
had silently fallen six keys behind.

Home Assistant caches integrations, so restart it after a sync.

## Status

Working and verified against real hardware, but young. See
[`OPEN-QUESTIONS.md`](https://github.com/schlarpc/pyvisionect/blob/main/OPEN-QUESTIONS.md)
in the library for what is still unresolved.

## Licence

MIT.
