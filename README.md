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
  already holds. The integration can never find a sign by itself — which is why
  it ships a [commissioning panel](#commissioning-from-the-browser) that does
  the serial side from the browser.
- **Nothing can wake the device.** Between contacts the radio and MCU are down,
  so every command is deferred until the sign next connects. A coalescing work
  queue sits behind the services, and `sensor.*_next_contact` tells you when your
  change will actually appear.

## Install

Copy `custom_components/visionect/` into your Home Assistant `config` directory,
or add this repository to HACS as a custom integration. Then add **Visionect**
from *Settings → Devices & Services* and choose a port (11113 by default).

Then point the sign at Home Assistant. There is no discovery, so this step is
unavoidable, once, for every sign — but it can be done from the browser you are
already in. See [Commissioning from the browser](#commissioning-from-the-browser).

By hand over the sign's USB serial console, it is:

```
server_tcp_set <home-assistant-ip> 11113
flash_save
cs 1
cs 3
```

`cs 3` on its own does nothing while a session is already open — `cs 1` first is
what tears the old socket down. The sign will also re-dial by itself roughly an
hour after `flash_save` if you would rather wait.

## Commissioning from the browser

**Settings → Visionect signs** in the sidebar. Plug the sign into the machine
running the browser with its USB cable and the panel does the sequence above for
you: it identifies the sign, shows you what the sign currently thinks, prefills
the Home Assistant address *from the address the listener actually bound to*,
shows you the exact commands it is about to send, and streams the console while
it runs them. It then waits for `conn_state_get` to report `tcp open`, so you
know it worked before you unplug the cable.

It uses **Web Serial**, so the sign's FTDI FT232 bridge stays owned by the
operating system's own driver — there is nothing to install and no driver to
unbind.

> ### The panel needs a secure context
>
> **Web Serial only exists on an HTTPS origin or a loopback one, in Chrome or
> Edge.** On a plain `http://` LAN address — which is a great many Home
> Assistant installs — `navigator.serial` is not there at all, and no setting
> in Home Assistant can change that: it is a property of how the browser
> reached the page. The panel detects this and says so on its first screen
> rather than appearing to work.
>
> `http://homeassistant.local:8123` does **not** count. The exemption is for
> loopback IP addresses and the literal name `localhost`, not for a name that
> happens to resolve to one.
>
> Three ways to get a secure context:
>
> - Home Assistant Cloud (Nabu Casa), which is HTTPS with nothing to configure.
> - A reverse proxy with a TLS certificate in front of Home Assistant.
> - Or, just for commissioning, open `http://localhost:8123` in a browser on the
>   Home Assistant host itself, with the sign plugged into that machine.
>
> Firefox and Safari have both declined to implement Web Serial, so there is no
> flag to turn on there. The panel still shows you the exact command sequence,
> which is the same sequence you would type into a serial terminal.

The panel will not send anything that can cost you a device. `play_music`,
`sf_rdid` and `sf_rdst` take the firmware's console down until a reboot;
`fs_format`, `cc3100_format`, the `*_upgrade` commands, `display_conf_set`,
`cli_password_set`, the `dcm*`, `bsim*` and `feat_*` families and others are all
refused — not by the buttons omitting them, but by an allow list checked inside
the serial transport, so no future change to the UI can get around it. The WiFi
passphrase is masked out of the console stream, including the sign's own echo of
it, and is never written to the plan, the transcript or the log.

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

The commissioning panel is plain ES modules with no build step, so its own
tests are JavaScript, run by Node's built-in test runner against the same
verbatim device captures:

```sh
node --test tests/js/*.test.mjs
```

`tests/test_panel_js.py` runs that suite too, so a plain `pytest` covers it;
it skips rather than fails when `node` is not installed. What it proves is
every decision the panel makes — the line discipline, the interleaved-log
framing, the plan builder, the identify handshake, the forbidden-command guard.
What it cannot prove is Web Serial itself: that API needs a real browser and a
real user gesture, so opening a port is the one thing only a human with a cable
can confirm.

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

## Installing via HACS

Add this repository as a **custom repository** (category: Integration), install,
then restart Home Assistant and add **Visionect** from *Settings → Devices &
Services*.

> **No brand icon yet.** Home Assistant's icons and logos live in a separate
> repository, [`home-assistant/brands`](https://github.com/home-assistant/brands),
> not here — so until a `visionect` entry is accepted there, HACS and the
> integrations page show a generic placeholder. Adding one means a PR to that
> repo with a 256×256 and 512×512 `icon.png` (and optionally `logo.png`),
> which needs artwork rather than code. The HACS validation workflow passes
> `ignore: brands` for this reason; drop that line once the entry lands.

### Version support

`hacs.json` declares a floor of **2025.1.0**, but the only version this has
actually been exercised against is **2026.9.4**. The panel needs
`async_register_static_paths` (`hass.http.register_static_path` was removed in
2026.9), and the service-target helpers moved to `homeassistant.helpers.target`
recently — that import is guarded both ways, but older releases are untested
rather than known-good. Treat anything below 2026.9 as unverified.

## Licence

MIT.
