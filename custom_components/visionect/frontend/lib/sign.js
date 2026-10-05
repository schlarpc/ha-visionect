/**
 * Proving it is a sign, and reading back what the sign thinks.
 *
 * Reply shapes are ported from `pyvisionect.io.usb.parsers`, which took them
 * off a live device. They are matched loosely on purpose: a field that moved
 * should degrade to "unknown" in one row of a table, not abort the whole read.
 * The one exception is the identify handshake, which is strict -- claiming to
 * have found a sign when it has found a 3D printer is the one place being
 * generous does damage.
 */

import { UnknownCommand } from "./console.js";

/** The port answered, but not like a Visionect sign. */
export class NotASignError extends Error {
  constructor(message, evidence = "") {
    super(message);
    this.name = "NotASignError";
    this.evidence = evidence;
  }
}

/**
 * `uuid_get` prints a blank line, then 16 space-separated `0x` bytes.
 *
 * Not a dashed UUID. The trailing zero bytes are real -- a 12-byte factory
 * identifier zero-padded to 16 -- so the rendered form ending in `00000000` is
 * correct and not truncation.
 */
export function parseUuid(lines) {
  const tokens = [];
  for (const line of lines) {
    const tail = line.includes("UUID:") ? line.slice(line.indexOf("UUID:") + 5) : line;
    for (const match of tail.matchAll(/0x([0-9a-fA-F]{1,2})/g)) {
      tokens.push(match[1]);
    }
  }
  if (tokens.length !== 16) {
    throw new NotASignError(
      `uuid_get should answer with 16 bytes; this port gave ${tokens.length}`,
      lines.join("\n"),
    );
  }
  const hex = tokens.map((t) => t.padStart(2, "0").toLowerCase()).join("");
  return [
    hex.slice(0, 8),
    hex.slice(8, 12),
    hex.slice(12, 16),
    hex.slice(16, 20),
    hex.slice(20, 32),
  ].join("-");
}

/** `fw_version_get` -- eight lines covering app, bootloader, board and panel. */
export function parseFirmware(lines) {
  const fields = {};
  for (const line of lines) {
    const at = line.indexOf(":");
    if (at > 0) {
      fields[line.slice(0, at).trim()] = line.slice(at + 1).trim();
    }
  }
  const version = fields["FW Version"] ?? "";
  // "PP32 v1.1, BOM: 0, APP: Joan" -- nested "k: v" pairs, so it cannot go
  // through the field split above.
  const hw = fields.HW ?? "";
  return {
    version,
    buildDate: fields["FW Build date"] ?? null,
    bootloader: fields["BL Version"] ?? null,
    build: fields["Build Version"] ?? null,
    hardware: hw ? hw.split(",")[0].trim() : null,
    app: /APP:\s*(\S+)/.exec(hw)?.[1] ?? null,
    panel: fields.EPD ?? null,
  };
}

/** `server_tcp_get` -- where the sign dials out. */
export function parseServerTcp(lines) {
  const fields = fieldsOf(lines);
  const port = Number.parseInt(fields["Server port"] ?? "", 10);
  return {
    host: fields["Server IP/DNS"] ?? null,
    port: Number.isInteger(port) ? port : null,
  };
}

/** `wifi_conf_get` -- SSID, security and band. Never the passphrase. */
export function parseWifiConf(lines) {
  const fields = fieldsOf(lines);
  const band = Number.parseInt(fields.Band ?? "", 10);
  return {
    ssid: fields.SSID ?? null,
    security: fields.Security ?? null,
    band: Number.isInteger(band) ? band : null,
  };
}

/**
 * `conn_state_get` -- the socket's state in words, e.g. `"tcp open"`.
 *
 * `connected` is a substring test on "open" because the firmware's state
 * vocabulary is not documented anywhere, so an unknown state reads as
 * not-connected rather than as an error.
 */
export function parseConnState(lines) {
  const state = fieldsOf(lines).Conn ?? "";
  return { state: state.trim(), connected: state.toLowerCase().includes("open") };
}

/** `cc3100_rssi` -- `"RSSI:-31 dBm"`, no space after the colon. */
export function parseRssi(lines) {
  for (const line of lines) {
    const match = /RSSI:\s*(-?\d+)/.exec(line);
    if (match) {
      return Number.parseInt(match[1], 10);
    }
  }
  return null;
}

/** `bq24023_mode_get` -- live charge mode, current and pack voltage. */
export function parseCharger(lines) {
  const fields = fieldsOf(lines);
  return {
    mode: fields.BQ ?? null,
    currentMa: intWithUnit(fields.Ibatt),
    voltageMv: intWithUnit(fields.Vbatt),
  };
}

/** `lmr` -- the LM75 board sensor, whole degrees Celsius. */
export function parseTemperature(lines) {
  for (const line of lines) {
    const match = /LM75:\s*(-?\d+)/.exec(line);
    if (match) {
      return Number.parseInt(match[1], 10);
    }
  }
  return null;
}

/** `uptime` -- **minutes**, not seconds. */
export function parseUptime(lines) {
  const value = Number.parseInt(fieldsOf(lines)["Uptime in min"] ?? "", 10);
  return Number.isInteger(value) ? value : null;
}

function fieldsOf(lines) {
  const out = {};
  for (const line of lines) {
    const at = line.indexOf(":");
    if (at > 0) {
      out[line.slice(0, at).trim()] = line.slice(at + 1).trim();
    }
  }
  return out;
}

function intWithUnit(text) {
  if (!text) {
    return null;
  }
  const value = Number.parseInt(text, 10);
  return Number.isInteger(value) ? value : null;
}

/**
 * Prove the far end is a Visionect sign before offering to reconfigure it.
 *
 * `uuid_get` then `fw_version_get`. Both are read-only, both exist on every
 * firmware this protocol has ever shipped on, and the pair is hard to produce
 * by accident: a serial device that answers `uuid_get` with sixteen `0x` bytes
 * *and* `fw_version_get` with an `FW Version` line is a sign.
 *
 * Refusing politely matters because the port picker shows every serial device
 * on the machine. A plotter, a 3D printer, a UPS and a radio all look the same
 * in that list, and all of them would be happy to receive `flash_save`.
 */
export async function identify(console_) {
  await console_.sync();

  let uuid;
  try {
    uuid = parseUuid((await console_.command("uuid_get")).lines);
  } catch (err) {
    if (err instanceof NotASignError) {
      throw err;
    }
    if (err instanceof UnknownCommand) {
      throw new NotASignError(
        "this port has a command line, but it has no uuid_get, so it is not a " +
          "Visionect sign.",
        err.message,
      );
    }
    throw err;
  }

  const firmware = parseFirmware((await console_.command("fw_version_get")).lines);
  if (!firmware.version) {
    throw new NotASignError(
      "fw_version_get did not report an FW Version line, so this is not a Visionect sign.",
      JSON.stringify(firmware),
    );
  }
  return { uuid, firmware };
}

/**
 * Every read the panel displays, each one allowed to fail on its own.
 *
 * A dump that aborted on the first missing command would be useless on exactly
 * the firmware you most want to look at, so failures are collected per command
 * and the rest of the table still fills in.
 */
export async function readSnapshot(console_, { onProgress = null } = {}) {
  const reads = [
    ["uuid", "uuid_get", parseUuid],
    ["firmware", "fw_version_get", parseFirmware],
    ["server", "server_tcp_get", parseServerTcp],
    ["wifi", "wifi_conf_get", parseWifiConf],
    ["connection", "conn_state_get", parseConnState],
    ["rssi", "cc3100_rssi", parseRssi],
    ["charger", "bq24023_mode_get", parseCharger],
    ["temperature", "lmr", parseTemperature],
    ["uptime", "uptime", parseUptime],
  ];
  const out = { errors: {} };
  for (const [key, command, parse] of reads) {
    if (onProgress) {
      onProgress(command);
    }
    try {
      out[key] = parse((await console_.command(command)).lines);
    } catch (err) {
      out[key] = null;
      out.errors[command] = err.message;
    }
  }
  return out;
}

/**
 * Poll `conn_state_get` until the sign holds a TCP session, or time runs out.
 *
 * Called after `cs 3`, which is the whole point of the commissioning flow: the
 * user needs to see "tcp open" before they unplug the cable, because the cable
 * is the only way to fix a wrong address once `flash_save` has run.
 */
export async function waitForConnection(console_, { timeoutMs = 120000, intervalMs = 3000, onPoll = null } = {}) {
  const deadline = Date.now() + timeoutMs;
  let last = null;
  for (;;) {
    try {
      last = parseConnState((await console_.command("conn_state_get")).lines);
    } catch (err) {
      last = { state: `read failed: ${err.message}`, connected: false };
    }
    if (onPoll) {
      onPoll(last);
    }
    if (last.connected) {
      return last;
    }
    if (Date.now() >= deadline) {
      return last;
    }
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }
}
