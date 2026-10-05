# Visionect e-ink signs — a Home Assistant integration

Home Assistant **is** the sign's server. This integration replaces the Visionect
Software Suite outright: it hosts the device-facing TCP listener, owns the
imaging pipeline, and models each sign as a device.

It is built on [`pyvisionect`](../../deps/lib/python3.14/site-packages/pyvisionect),
a clean-room reimplementation of the protocol.

---

## You are the server — read this first

Three properties of the hardware decide everything about how this works, and
none of them are negotiable.

**1. The sign is a TCP client.** It dials out to port 11113 and speaks first. It
runs no listening service at all. So there is nothing for Home Assistant to
poll and no address to connect to — Home Assistant has to *listen*, and the
sign has to be told where to call.

**2. There is no discovery, in either direction.** No mDNS, no DHCP option, no
broadcast. The server address lives on the device as a parameter that is
**read-only over the network**. The integration can never "find" a sign; you
have to point it here. See [Pointing your sign at Home
Assistant](#pointing-your-sign-at-home-assistant).

**3. Nothing can wake the device.** Between contacts the radio and the MCU are
down. So **every command is deferred** until the sign next calls in. On a
mains-powered sign with a one-minute heartbeat that is invisible; on a battery
sign it is the dominant fact. `sensor.<sign>_next_contact` exists to answer
"when will my change appear?" and `binary_sensor.<sign>_pending_changes` exists
to say "it has not appeared yet".

A corollary worth stating out loud: **any sign that can reach port 11113 gets
in.** The UUID in its first packet is the entire identity claim — there is no
pairing code, no nonce, no shared secret, and registration is auto-create. The
real risk is not device hijack (there are no per-device secrets to steal) but
**content exfiltration**: anything that presents your sign's UUID gets whatever
you push to it, full-screen. Put the port on a network only your signs can
reach, and never expose 11113 to the internet.

---

## Install

### 1. The library

`pyvisionect` is not on PyPI, so the integration cannot declare it in
`manifest.json`. It is installed into Home Assistant's own `deps` directory,
which is inside the config directory and therefore survives a container
restart *and* a container recreate:

```sh
# the path Home Assistant adds to sys.path at boot, when not in a venv
SP=/config/deps/lib/python3.14/site-packages        # match the container's python

podman exec hass pip install --target "$SP" 'xxhash>=3.0'
cp -r /path/to/pyvisionect/src/pyvisionect "$HOST_CONFIG/deps/lib/python3.14/site-packages/"
find "$HOST_CONFIG/deps/.../pyvisionect" -name __pycache__ -prune -exec rm -rf {} +
```

Verify it from inside the container the same way Home Assistant will:

```sh
podman exec hass python -c "
import asyncio
from homeassistant.bootstrap import async_mount_local_lib_path
asyncio.run(async_mount_local_lib_path('/config'))
import pyvisionect, xxhash, numpy, PIL; print('ok', pyvisionect.__file__)"
```

`numpy` and `Pillow` are already in Home Assistant core's dependency set, so
nothing extra is needed for them.

> **The `python3.14` in that path is the container's Python version.** A Home
> Assistant image that moves to 3.15 will stop seeing the directory. Re-run the
> copy against the new path after a major image upgrade. (This is the price of
> using Home Assistant's official mechanism; the alternative — installing into
> the image's own `site-packages` — does not survive a container recreate.)

### 2. `lz4` — optional, but install it if you can

**The `lz4` project publishes no `musllinux` wheels, and the official Home
Assistant container is Alpine.** A plain `pip install lz4` there falls back to a
source build, which fails because the image has no compiler.

The integration handles both cases. It imports `lz4` at setup: if it is there,
outbound frames are LZ4-compressed exactly as the vendor's own server does; if
it is not, it falls back to `ConnectionConfig(compressor=STORED_ONLY)`, which
emits every block uncompressed (`Stored=1`) — a mode the block format explicitly
provides for. Which one is in use is logged at setup and reported in
diagnostics as `connection_config.compressor`.

**Prefer LZ4 where you can, because it is the only path proven against this
firmware's decompressor.** The all-`Stored` path is conforming by specification
and the device has no reason to refuse it, but it has not been observed on the
wire.

To build it into the container's `deps` directory (which is bind-mounted, so the
compiled extension survives both a restart and a recreate even though the build
toolchain does not):

```sh
podman exec hass apk add --no-cache gcc musl-dev python3-dev
podman exec hass pip install --target /config/deps/lib/python3.14/site-packages 'lz4>=4.0'
```

A note on cost, because the design document guessed wrong about this: LZ4 was
expected to buy "essentially nothing", on the grounds that the captured vendor
push was ~1.84 MB on the wire against a 1 843 200-byte payload. That is true of
*that* frame. On ordinary dashboard content — large flat white areas plus some
dithered gradient — a measured push went out in **317 KB against the same
1 843 268-byte payload, a 5.8x reduction**. So LZ4 is worth having for the Wi-Fi
time alone, especially on a battery sign.

`xxhash` *is* needed, and does ship musllinux wheels. There is a pure-Python
fallback but it is about nine times slower per frame.

### 3. The integration

Copy this directory to `<config>/custom_components/visionect/`, restart Home
Assistant, then **Settings → Devices & Services → Add Integration → Visionect**.

The flow asks for a port and a bind address, checks that it can actually bind
them — the only validation the protocol permits — and then shows you the
instructions for pointing your sign here. Keep port **11113**: it is the
firmware default, so a sign that has never been reconfigured needs no port
argument at all, and the DNS route below requires the port to stay put.

### 4. Is the port reachable?

| Install method | Reachable? |
|---|---|
| Home Assistant OS / Supervised | **Yes, no action needed** — the Supervisor runs core with host networking |
| Container, following the official docs | **Yes** — the documented `docker run` and compose file both use host networking |
| Container, bridge network | **No**, until you publish `11113:11113` |
| Core (venv) | Yes — 11113 is unprivileged, so only a host firewall applies |

If you are on a bridge network, **publishing a port needs the container
recreated, not restarted**: `docker compose up -d --force-recreate`. A plain
`docker restart` will appear to change nothing, which is the single most
confusing failure here, because `docker restart` is exactly what Home
Assistant's own docs tell you to do after a config change.

If nothing has connected ten minutes after setup, the integration raises a
repair issue saying so, and distinguishes "nothing reached the port at all"
from "something connected but never identified itself" — those need opposite
troubleshooting. The same counters are on the **Visionect listener** device and
in diagnostics.

---

## Pointing your sign at Home Assistant

Your sign cannot be discovered and cannot be told over the network where to
find Home Assistant. You must set it one of two ways.

### Option A — USB (always works, needs physical access)

The sign's micro-USB port is a serial console at 115200 8N1 (`/dev/ttyUSB0` on
Linux, `/dev/cu.usbserial-*` on macOS):

```
server_tcp_set <home-assistant-address> 11113
flash_save
```

and then make the sign open a **new** connection. See the warning below.

### Option B — DNS (no physical access)

Only possible if the sign already holds a *hostname* rather than a literal IP —
read it with `server_tcp_get`. Re-point that name at Home Assistant in your
local DNS and the sign arrives on its next connection.

### Use a hostname, not an IP

If you are making the USB trip anyway, spend it on a **hostname you control**,
not on an IP address. Then you can move Home Assistant, change its address, or
switch back to the Visionect server later with a one-line DNS change instead of
another trip up the ladder. This is the single most valuable sentence on this
page.

### Making the sign actually reconnect — a correction

The vendor documents `cs 3` ("Connect to server") as forcing an immediate
reconnect without a reboot. **On firmware 7.4.4407 it does nothing at all while
a TCP connection is already open**: observed over the serial console, `cs 3`
returns `Connectivity in state 3`, the existing session's packet counter keeps
incrementing, and no new connection is attempted. A mains-powered sign holds
that session more or less permanently.

So after `server_tcp_set` + `flash_save`, the sign moves when it next opens a
connection, which is either:

* at the next spontaneous reconnect — these do happen (two were observed in a
  22-minute window in the reference capture), so this is usually a matter of
  minutes to tens of minutes; or
* at the next `reboot`, which is the deterministic option and is the vendor's
  own documented plan.

`cs 3` is still worth running — it is harmless and it does force a connect when
the sign is *not* currently connected.

Observed in practice: after `server_tcp_set` + `flash_save`, the sign stayed on
its old session for **about an hour** and then moved of its own accord, with no
further prompting. So the honest advice is: do the two commands, then either
reboot the sign for a deterministic switch, or walk away and check back later.

### You cannot run alongside the Visionect server

The sign holds exactly one server address and dials exactly one server. There
is no multi-master mode and no "report to both". Pointing a sign here takes it
away from VSS completely, and VSS will show it offline. Before you repoint,
**write down the UUID** from VSS (`GET /api/device/`) — it is how you will
recognise the sign here, and you will not be able to read it from VSS
afterwards.

---

## The content model

**A sign has a content source; pushing is the front door.** This is not a
stylistic choice. Pushing ten images to a sleeping sign must result in one push
— the last — which makes the queue depth exactly one, and a one-deep
last-write-wins queue *is* a desired-state register.

It also means the source is re-read **at wake time**, not at request time. Set
a sign's source to `camera.front_door` once and it shows a fresh snapshot on
every contact forever, with no automation at all. That is the path to try first.

```yaml
action: visionect.set_content_source
target:
  device_id: <your sign>
data:
  source: entity
  entity_id_source: camera.front_door
```

Every action accepts **any** target Home Assistant understands: `device_id`,
`entity_id` (any entity belonging to a sign — `image.<sign>_screen` is the easy
one), `area_id`, `floor_id` or `label_id`. The sign is the unit of meaning and
the handler resolves whatever you gave it back to one, so these are the same
call:

```yaml
target:
  device_id: <your sign>
# or
target:
  entity_id: image.kitchen_sign_screen
# or
target:
  area_id: kitchen          # a sign and a lamp in it: only the sign is acted on
```

A target that names nothing belonging to this integration is a validation
error, not a silent no-op.

The imperative path is one call and no configuration:

```yaml
action: visionect.display_image
target:
  device_id: <your sign>
data:
  image: /config/www/chart.png     # path | http(s) URL | image.* | camera.*
```

`image` is resolved by shape. The one subtlety worth knowing: passing an
**entity id** sets a *live* source, while passing a path or URL **snapshots it
now**. That is the right default for both — someone who types
`camera.doorbell` wants the latest frame every hour, and someone who types
`/config/www/chart.png` wants the file read now. Pass `snapshot: true` to force
the entity case to snapshot.

### Pushing a dashboard — the honest answer

**Home Assistant cannot screenshot itself.** There is no rasteriser in core, and
rendering a Lovelace kiosk URL to an image needs a browser. This integration
ships no browser, no add-on dependency and spawns no subprocesses, so it cannot
do this for you.

What it does instead is make delegating it one text field. Point a content
source at a renderer you run, and the integration fetches it at the moment the
sign is awake:

```yaml
action: visionect.set_content_source
target:
  device_id: <your sign>
data:
  source: url
  url: >-
    http://renderer.lan:3000/screenshot?url=http%3A%2F%2Fhomeassistant.local%3A8123
    %2Flovelace%2Fsign%3Fkiosk&width=1440&height=2560&wait=3000
  headers:
    Authorization: "Bearer <a Home Assistant long-lived token>"
```

Pull-at-wake genuinely beats push-whenever here: a pulled frame is seconds old
whenever the sign happens to wake, and the renderer runs once per contact
rather than on a timer. The `url` is rendered as a Jinja template at push time,
so `?t={{ now().timestamp() }}` works for cache-busting.

This is the weakest part of the experience, it is not solvable inside a custom
integration, and anyone promising otherwise is about to ship a browser.

For a short message with no renderer at all there is `visionect.display_text`:
one font, one size, word-wrapped, centred. It will not grow. Anything more
elaborate is a layout engine, and the right layout engine is the browser you
already have.

### Every push is 1.84 MB

This hardware cannot do partial updates — the rectangle support flag is false
unconditionally for it — so every push is a single full-screen frame. Please do
not point a sign at a source that changes every minute. The display-update
count is exposed as a sensor because e-ink panels have a finite update budget.

---

## Entities

Entities are created from **the first status packet**, not from a static list.
This sign reports 61 of roughly 80 possible fields, so a static list would
create about twenty permanently-unavailable entities on day one. A field that
first appears on a later heartbeat gets its entity then; entities are never
removed.

**Primary**

| Entity | What it is for |
|---|---|
| `image.<sign>_screen` | What the sign is showing. Rendered **post-dither**, so it looks like the panel rather than better than it. |
| `sensor.<sign>_next_contact` | "Your change will appear at 14:32." The most useful entity here. |
| `binary_sensor.<sign>_pending_changes` | The deferred model made visible. Its attributes list the queue, the content source, the revision counters and the last error. |
| `button.<sign>_update_now` / `_refresh` / `_clear_screen` | |
| `select.<sign>_dither_mode` / `_encoding` / `_fit_mode` | Server-side, so instant in Home Assistant. |

**Diagnostic** — battery, battery voltage and current, signal strength,
temperature, panel temperature, last boot, display updates, last status reason,
connect reason, last contact, last push, filesystem free, MCU awake count,
network errors, Wi-Fi DTIM, access point, connections, failed pushes, charging,
connected, display out of sync, image push blocked.

Three of those deserve a note:

* **Signal strength is negated.** The wire value is a dBm *magnitude* (44 means
  −44 dBm). The library's RSSI codec does the negation; getting it wrong is the
  most likely bug in an integration like this.
* **"Last status reason" is not a fault.** It is `ErrorCode`, which is the
  *reason the packet was sent*. `deep sleep request` is the device announcing
  something normal.
* **Last boot is a timestamp, not a duration.** The device reports uptime in
  minutes; `SensorDeviceClass.UPTIME` converts it to a boot moment and
  suppresses the minute-granularity jitter for you.

### Availability: asleep is not unavailable

`available` tracks the **transport**; staleness goes in **`assumed_state`**.
A sign with an hourly heartbeat would otherwise spend 59 minutes of every hour
showing `unavailable` on every entity, and the recorder would be full of gaps.
An asleep sign is doing exactly what it was told to do.

This follows four independent core precedents — `zwave_js` (where an *asleep*
node is available and so is a *dead* one), `matter`, `oralb`/`xiaomi_ble` and
`shelly` — all of which surface lifecycle state as a separate diagnostic rather
than as an availability input. So: `binary_sensor.<sign>_connected` is the
honest flappy "is it on the line", `sensor.<sign>_last_contact` says how old
the data is, and `sensor.<sign>_next_contact` says when it will be fresh.

Entities whose value *is* a property of the connection — `connected`,
`connections`, the listener counters, the selects — are always available and
never assumed, because "no socket" is a valid value for them rather than
missing data.

### Restarting Home Assistant is free

E-ink is persistent: after a restart the sign is still showing the last frame.
The integration persists the content source, the revision counters, the pushed
checksum and the encoder's `FrameState`, so a restart with a settled sign
produces **zero** pushes. `DisplayStateCRC` — the device echoing our own
checksum back — is also checked once per run, and if the sign is showing
something we did not push, the content is re-asserted. Once per run, so a
device that never confirms cannot put the integration in a push loop.

---

## What this integration deliberately does not do

**It sends no `packet.Type 2` (command) packets. Ever.** Type 2 has never been
observed on the wire, in either direction, in any capture — every command
layout is read out of a Go binary rather than byte-verified — and there is
direct precedent in this protocol for the struct and the wire format
disagreeing (the param packet's struct is 12 bytes and its wire header is 8).
So the connection is constructed with `allow_command_packets=False`, which
makes any attempt raise instead of putting a possibly-malformed frame on the
wire, and with `watchdog_enabled=False`, which is mandatory alongside it because
the library's inactivity watchdog answers an expiry with a type-2 status
request.

The consequences, and what replaces each:

| Not shipped | Why | Instead |
|---|---|---|
| `reboot` | unverified framing, and the recovery path is a USB cable | use the serial console |
| `sleep` | type 2 | use the heartbeat interval number |
| `send_command` | type 2 | — |
| `refresh` / `clear_screen` as commands | the command ids exist only as enum entries; there is no emission site for them anywhere in the vendor server | **image pushes**, which were reproduced byte-exactly from a capture. Every push here is full-screen anyway, so re-pushing *is* a full-screen refresh — not a workaround, the better mechanism |

Also not shipped: firmware push (the payloads are AES-encrypted under a key
that only exists on Visionect's servers, so we could relay a blob but never
build, verify or inspect one); link encryption (the key is absent from the
vendor image, so self-hosted deployments are plaintext by construction);
discovery (there is none to build); and partial updates (unreachable on this
hardware).

**TLS is shipped, and almost certainly useless to you.** The two entry options
**TLS certificate file** and **TLS private key file** turn on opportunistic
TLS 1.3: the listener reads the first six bytes of each connection and wraps
only the ones that open with a ClientHello, so plaintext signs and TLS signs
share the one port and nothing has to be migrated. Both default to empty, and
should stay empty unless you know the sign's end works, because the device half
depends on TCLV parameter 145 (*TLS mode*) and **the firmware this was
developed against (7.4.4407) does not implement 145 at all** — it refuses both
a read and a write of it with the same code it gives for a parameter id that
does not exist. Until a sign turns up that answers a read of 145, network
isolation is the only transport control that works, which is why the advice
above matters.

One asymmetry worth knowing: a sign whose 145 *is* set to 1, pointed at a
listener with no certificate, cannot be reached over the network at all and can
only be recovered over its USB serial console. So set the certificate here
first and the parameter second. The listener sniffs for a ClientHello even with
TLS switched off purely to make that mistake visible — it logs it and counts it
in `tls_unsupported` in the diagnostics, rather than letting it look like a
port scan.

Device file readback **is** shipped, as `visionect.read_device_file`, with the
caveats in its own section below.

### Reading a file off the sign — and what is actually on it

```yaml
action: visionect.list_device_files      # one round trip, under a second
target:
  entity_id: image.kitchen_sign_screen

action: visionect.read_device_file       # minutes. See below.
target:
  entity_id: image.kitchen_sign_screen
data:
  filename: /image0.pv2
```

The decoded picture lands in `image.<sign>_device_file`, a diagnostic entity
that is **disabled by default** because filling it is not free. The action's
response carries the headers and the measured rate:

```json
{"filename": "/image0.pv2", "size": 134409, "bytes_read": 134409,
 "duration": 56.4, "rate_kib_s": 2.37, "decoded": true, "blocks": 385,
 "image_checksum": 0, "written_by_us": false, "matches_last_push": false}
```

**It is slow, and the protocol gives no way to make it faster.** One reply
carries at most 1024 bytes however much you ask for, there is no seek opcode,
and the steady rate is ~2.3 KiB/s. So this sign's stored frames take one to
nine minutes each, and a lost reply restarts the transfer from the beginning —
`attempts` is how many times it may do that.

**These files are not a live framebuffer, and this firmware has none.** On
7.4.4407 the six `/imageN.pv2` files are Visionect's shipped demo screens: a
wayfinding board, a museum label and four more. Pushing a new frame changes not
one byte of them, and they carry a zero `ImageHeader.Checksum` and a zero UUID,
which no frame we send ever does — that is what `written_by_us` reports. What
the panel is actually showing is answered for free by `DisplayStateCRC` in every
status packet, which is what `binary_sensor.<sign>_display_out_of_sync` reads.

The action is shipped anyway because the decode is real and verified, and
because a firmware that did cache the live frame would need exactly this and
nothing more.

`button.<sign>_ghost_clear` *is* shipped, disabled by default and labelled
experimental. The server-side signalling is verified; the firmware's reaction to
it is inferred, and on a stock configuration the bit it clears is already clear,
so it may do nothing beyond a second push. It promises nothing.

---

## Device settings

`number.<sign>_heartbeat_interval` writes TCLV 29 and is **optimistic**: Home
Assistant's state changes immediately, the write is coalesced into the device's
pending map (last-write-wins per id), and on the next contact it goes out
followed by a flash save and then a read of the same id — so the *device* gets
the last word, not our optimism.

The flash save matters: without it the write is RAM-only and is lost at the next
reboot, which is a baffling failure. With it, every settings change costs a
flash write — hence the **Persist device settings to flash** entry option and
the `persist:` override on `visionect.write_parameters`.

Honest caveat: nothing in the vendor server was ever observed issuing the flash
save after a parameter write, and no payload for it is documented. The
integration issues it with a value of 1 and reads the parameter back, which is
the only way to find out whether it worked.

Eight parameters — the hardware id, the server address and port, and the Wi-Fi
credential family — are **read-only over the network** and can only be set over
USB. `visionect.write_parameters` refuses them with an error that names the USB
command instead.

---

## Actions

| Action | |
|---|---|
| `visionect.display_image` | path, URL, or `image.*`/`camera.*` entity |
| `visionect.display_text` | one font, one size, word-wrapped |
| `visionect.set_content_source` | the declarative front door |
| `visionect.clear_content` | forget the source; the screen keeps its frame |
| `visionect.update_now` | re-resolve and push on next contact |
| `visionect.refresh` | re-push even if the sign says it is in sync |
| `visionect.clear_screen` | push an all-white frame |
| `visionect.ghost_clear` | experimental inverse pass |
| `visionect.read_parameters` / `write_parameters` | TCLV ids |
| `visionect.list_device_files` | a read of `"."` — there is no list opcode |
| `visionect.read_device_file` | pull one file off the sign's flash and decode it |

Every one of them returns a response, because "queued, expected by 14:32" is
the whole point:

```json
{"results": [{"uuid": "…", "queued": true, "applied_immediately": true,
              "expected_at": "2026-10-05T01:42:00+00:00"}]}
```

`applied_immediately` is `true` when the sign happens to be connected — which,
on a mains-powered sign, is always. The deferred machinery is invisible in the
common case and correct in the hard one.

### Events

`visionect_device_connected`, `visionect_push_completed`,
`visionect_push_failed`, `visionect_command_nacked`, `visionect_files_listed`,
`visionect_touch`, `visionect_button`. These make `wait_for_trigger` work, which
is how you write "push the dashboard, then tell me when it landed".

---

## Troubleshooting

**Nothing has connected.** Check the **Visionect listener** device: "Accepted
connections" counts TCP connections, "Identified signs" counts ones that sent a
valid first status packet. Zero accepted means nothing is reaching the port —
check the sign's `server_tcp_get`, your container's network mode, and the host
firewall. Accepted but zero identified means something is connecting that is not
a sign, or is not speaking this protocol version.

Note that a connection which **opens and closes carrying no protocol bytes at
all** is normal — it was observed from the real sign in ordinary operation and
is counted separately as `silent_connections` in diagnostics. It is not a fault.

**It connected but the screen never changed.** Look at
`binary_sensor.<sign>_pending_changes` attributes for `last_error`, and at
`sensor.<sign>_failed_pushes`. A content source that cannot be resolved does
**not** consume the revision and does **not** clear the screen — the old frame
is better than a blank one, and the next contact retries automatically.

**`display_out_of_sync` is on.** It should now mean something. The sensor is a
`problem` device class, so it only turns on once the sign has genuinely failed
to converge — not during the ordinary window after a push, where the frame is
acked in ~7 s but the confirming `DisplayStateCRC` does not arrive until a
later status packet (10–48 s on the reference sign).

Its attributes say which case you are in:

| attribute | meaning |
|---|---|
| `sync_status` | `in_sync` / `converging` / `diverged` / `unknown` |
| `checksums_match` | the raw comparison — `false` is normal while converging |
| `pushed_checksum` / `device_checksum` | the two values being compared |
| `contacts_since_push` / `contacts_needed` | status packets since the push |
| `settle_seconds` | before this, no number of contacts counts |
| `grace_seconds` | after this, a silent sign is called diverged anyway |

Both windows are derived from the device's own announced `NextStatus`, so a
sign on an hourly heartbeat is not called broken for the 59 minutes it is away.
`unknown` means nothing has ever been pushed — a fresh install, not a fault.

**Debug logging:**

```yaml
logger:
  logs:
    custom_components.visionect: debug
    pyvisionect: debug
```

**Diagnostics** (the ⋮ menu on the config entry or the device) dumps the
listener counters, every decoded status field, the raw tag map including any
unknown residue, the panel geometry and whether it came from a default row, the
pushed checksum against the device's reported one, the TCLV cache and the
pending queue. It is the first thing to attach to a bug report. The UUID,
GTIN, BSSID, content URL and headers are redacted — a `url` content source can
carry a long-lived access token in a header.

---

## Known limitations

* The sign's panel (`DisplayType 0xC2050128`) is **not in the vendor's own
  table**, so the geometry comes from a default row: 1440×2560 canvas, 4 ×
  1440×640, `eink-flip` driver. Diagnostics says so explicitly rather than
  hiding the guess. It is correct for this hardware.
* Hardware revision 1.0.0 signs use interlacing mode 1, which the library
  raises `NotImplementedError` for. This integration will fail to encode for
  one.
* There is a faint dark band at the panel's internal boundaries, most visible
  with blue-noise dithering on smooth gradients. It is device-side and is
  present under the vendor stack too.
* One config entry only. Two would mean two listeners and only one can hold the
  port.

---

## What was actually verified, and what was not

Honesty matters more than a tidy checklist here, so this is the real status.

### Verified on the real panel

A 31.2" Place & Play 32, UUID `00112233-…`, firmware 7.4.4407, re-pointed from
the Visionect Software Suite to this integration.

| Test | Result |
|---|---|
| Sign dials in, is auto-registered, 27 entities created from its status packet | pass |
| Readings match the serial console exactly: RSSI **−31 dBm** (wire 31), 4210 mV, 46 mA, 22 °C, uptime | pass |
| First contact pushes a placeholder; **acked in 3.7 s** | pass |
| It reported the old server's frame checksum, so the integration re-asserted its own content — **once** | pass |
| **`visionect.display_image` put a chosen picture on the glass**: encoded 4 bpp blue-noise, LZ4, acked in 3.9 s, and the device echoed `DisplayStateCRC = 3557037784`, **identical to our `pushed_checksum`** | pass |
| That checksum is byte-identical to the one the offline replay produced from the same file — the encoder is deterministic | pass |
| Restart Home Assistant with the sign in sync **and holding its socket**: teardown completes in 8 s, port rebinds, **zero pushes**, `DisplayUpdateCount` unchanged at 6 | pass |
| `ErrorCode` stays `no error` throughout | pass |

### Also verified against hardware-faithful traffic

Every test below ran against a synthetic sign that replays this exact sign's own
captured status frames — verbatim device-to-server bytes from a server-side
pcap of the 31.2" Place & Play, firmware 7.4.4407, with only identifiers
redacted. Same `DisplayType`, so the same panel row, the same 1440x2560 canvas,
the same two 2880x640 interlaced rectangles and the same 1 843 268-byte image
payload as the real device.

| Test | Result |
|---|---|
| Config flow binds the port, detects the host address, creates the entry | pass |
| 27 entities created **from the status packet**, not a static list | pass |
| `SignalStrength` reported as **−44 dBm** from a wire value of 44 | pass |
| `DeviceUptime` (minutes) rendered as a boot timestamp | pass |
| `NextStatus` drives `sensor.next_contact` | pass |
| **Service call puts pixels on the panel**, device echoes our `DisplayStateCRC` | pass |
| Queue while disconnected, apply on next connection | pass |
| Three queued pushes coalesce to **one**, showing the third | pass |
| Restart with an in-sync sign produces **zero** pushes | pass |
| Reload the entry while a client holds the socket — completes, rebinds, no hang | pass |
| All four content sources resolve: local path, templated URL, `image.*` entity, placeholder | pass |
| Content source survives a restart; a failed push retries on next contact | pass |
| Failed source: old frame kept, revision not consumed, one warning, backoff | pass |
| Dither change forces a re-encode (different checksum) | pass |
| `refresh` forces a redraw by perturbing the checksum | pass |
| TCLV write ordering: reads, then writes, then flash save, all acked | pass |
| Read-only TCLV id refused with the USB command named | pass |
| Repair issue raised when nothing connects for ten minutes | pass |
| Two signs on one listener: 23 entities each, independent sources and checksums | pass |
| Delete a sign, then let it dial in again: entities are rebuilt | pass |
| `assumed_state` flips to true once a sign misses its announced window, while the sign stays **available** with its last readings | pass |
| ...and `binary_sensor.connected` / the selects never assume, because "no socket" is a value for them | pass |
| ...and it logs **once**, on the transition, not once per missed heartbeat | pass |
| Clean boot with no warnings or errors from the integration | pass |

### Not verified

* **A sign on battery.** This one is mains-powered with a one-minute heartbeat,
  so the deferred paths were exercised by disconnecting it rather than by a real
  multi-hour sleep.
* **A firmware that caches the live frame.** `visionect.read_device_file`
  decodes any stored frame correctly, but this firmware keeps none — see below.
* **The flash-save round trip** (write a parameter, save, reboot, read it back).
  It cannot be completed without a reboot.

### Deliberately not built

Beyond everything in "What this integration deliberately does not do": there is
no webhook or upload endpoint for push-only renderers (use a `url` content
source), no `media-source://` support in `visionect.display_image`, and no
`select.power_saving_mode` — its option list has never been recovered, and a
select whose options are guesses is worse than no select.
