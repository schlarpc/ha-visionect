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

27 entities, created from the device's own status packet rather than a static
list, so you do not get a screenful of permanently-unavailable sensors: battery,
voltage, current, signal strength, temperature, uptime, last contact, next
contact, display updates, filesystem free space, charging, connected, and more.
Plus an `image` entity showing what is on the screen, buttons, dither/encoding
selects, and 12 services — `display_image`, `display_text`, `set_content_source`,
TCLV parameter read/write, device file listing and readback.

## Status

Working and verified against real hardware, but young. See
[`OPEN-QUESTIONS.md`](https://github.com/schlarpc/pyvisionect/blob/main/OPEN-QUESTIONS.md)
in the library for what is still unresolved.

## Licence

MIT.
