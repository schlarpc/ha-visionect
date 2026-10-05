/**
 * What this panel is allowed to say to a sign, and what it must never say.
 *
 * This module is the single chokepoint: `SerialConsole.command` calls
 * {@link assertAllowed} before it writes a byte, so "the panel cannot emit
 * `sf_rdid`" is a property of the transport rather than a property of the UI
 * remembering not to offer a button. A future contributor adding a text box
 * that sends arbitrary commands gets a refusal, not a bricked sign.
 *
 * Two independent checks, in this order:
 *
 *   1. A **deny list**, which is the measured list of commands that cost a
 *      device reboot, a flash format, a licence, a password you then have to
 *      know, or a panel. Checked first so the refusal can say *why*.
 *   2. An **allow list**, which is the ~20 commands commissioning actually
 *      needs. Anything else is refused as out of scope even if it is harmless.
 *
 * The allow list alone would be sufficient to keep the sign safe. The deny
 * list exists anyway because it is the part that encodes knowledge: it is a
 * written record of what went wrong on real hardware, and it keeps being
 * correct if somebody widens the allow list.
 */

/** The mask substituted for a secret before anything is displayed or logged. */
export const MASK = "•".repeat(8);

/**
 * Commands that must never be sent from a browser, by exact name.
 *
 * Every entry here is either measured damage or unrecoverable-without-a-cable.
 */
export const DENIED_COMMANDS = Object.freeze({
  // --- kills the console until the device reboots (~25 min of no sign) -----
  play_music: "takes the USB CLI task down; the console does not come back until the device reboots",
  sf_rdid: "asserts in spi_flash_cli.c:139 and takes the USB CLI task with it",
  sf_rdst: "asserts in spi_flash_cli.c:188 and takes the USB CLI task with it",
  // --- destroys stored state ----------------------------------------------
  fs_format: "formats the image filesystem",
  cc3100_format: "formats the radio's own flash, including its calibration",
  // --- changes identity, licensing or access ------------------------------
  cli_password_set: "sets a console password you then have to know to get back in",
  // --- can damage the panel ------------------------------------------------
  display_conf_set: "a wrong VCOM rail can physically damage the e-ink panel",
  // --- leaves the device asleep or lying about its sensors -----------------
  app_sleep: "puts the application into deep sleep",
  "24aa256_test": "writes a test pattern over the EEPROM",
  lms: "makes the LM75 report a simulated temperature, which changes the waveform chosen for every later update",
  // --- SPI flash write protection -----------------------------------------
  sf_unprot: "removes SPI flash write protection",
  sf_wrst: "writes the SPI flash status register",
});

/**
 * Families that must never be sent, by name pattern.
 *
 * Patterns rather than names because these families are open-ended: the point
 * is that *any* `*_upgrade` is refused, including one this panel has never
 * heard of.
 */
export const DENIED_PATTERNS = Object.freeze([
  { pattern: /_upgrade$/, why: "flashes firmware; an interrupted upgrade is a brick" },
  { pattern: /^feat_(enable|disable)$/, why: "changes the licensed feature set" },
  { pattern: /^feat_/, why: "the feature-licence family is out of bounds" },
  { pattern: /^encryption_\w*_set$/, why: "changes the outbound encryption key or mode, which silently breaks the link to Home Assistant" },
  { pattern: /^dcm[a-z]*$/, why: "pokes the display driver directly" },
  { pattern: /^bsim/, why: "makes the device report a simulated battery state" },
  { pattern: /format$/, why: "formats a filesystem" },
]);

/**
 * The commands commissioning needs. Nothing else goes out.
 *
 * Note what is *not* here:
 *
 * - `wifi_conf_set`. It is positional and splits its arguments on whitespace,
 *   so an SSID or passphrase containing a space shifts every later argument
 *   along by one. The three single-value setters are used unconditionally
 *   instead, which also means the passphrase occupies exactly one command and
 *   can be kept out of everything that gets displayed.
 * - `reboot`. The reconnect is done with `cs 1` then `cs 3`, which costs
 *   seconds instead of a minute and does not blank the glass.
 * - `help`. 5 KB of output to learn nothing the panel acts on.
 */
export const ALLOWED_COMMANDS = Object.freeze(
  new Set([
    // identify
    "uuid_get",
    "fw_version_get",
    "cli_version_get",
    "gtin_get",
    // state the panel shows
    "server_tcp_get",
    "server_hb_get",
    "wifi_conf_get",
    "wifi_bssid_get",
    "wifi_mac_conf_get",
    "conn_state_get",
    "conn_type_get",
    "ipv4_conf_get",
    "cc3100_rssi",
    "bq24023_mode_get",
    "battery_conf_get",
    "lmr",
    "uptime",
    // commissioning
    "wifi_ssid_set",
    "wifi_psk_set",
    "wifi_security_set",
    "server_tcp_set",
    "flash_save",
    "flash_load",
    "cs",
  ]),
);

/** Raised instead of writing to the port. */
export class ForbiddenCommandError extends Error {
  constructor(line, reason) {
    super(`refusing to send ${JSON.stringify(line)}: ${reason}`);
    this.name = "ForbiddenCommandError";
    this.line = line;
    this.reason = reason;
  }
}

/**
 * The command word of a line: everything up to the first space, lowercased.
 *
 * The firmware is case-insensitive about command names, so the check has to be
 * too -- `SF_RDID` is the same assertion as `sf_rdid`.
 */
export function commandName(line) {
  return String(line).trim().split(/\s+/, 1)[0].toLowerCase();
}

/**
 * Why *line* may not be sent, or null if it may.
 *
 * @returns {{code: string, reason: string} | null}
 */
export function checkCommand(line) {
  const raw = String(line);
  if (/[\r\n]/.test(raw)) {
    return {
      code: "line-break",
      reason:
        "it contains a line break. CR submits a line on this firmware, so " +
        "this would send two commands and desynchronise the stream",
    };
  }
  if (raw.includes(";")) {
    return {
      code: "separator",
      reason:
        "the firmware has no statement separator, so this would be sent as " +
        "one command name and rejected",
    };
  }
  const name = commandName(raw);
  if (!name) {
    return { code: "empty", reason: "it is empty" };
  }
  if (Object.hasOwn(DENIED_COMMANDS, name)) {
    return { code: "denied", reason: DENIED_COMMANDS[name] };
  }
  for (const { pattern, why } of DENIED_PATTERNS) {
    if (pattern.test(name)) {
      return { code: "denied", reason: why };
    }
  }
  if (!ALLOWED_COMMANDS.has(name)) {
    return {
      code: "not-allowed",
      reason:
        `${name} is not one of the commands this panel commissions with. ` +
        "Use a serial terminal if you need it",
    };
  }
  return null;
}

/** Throw {@link ForbiddenCommandError} unless *line* may be sent. */
export function assertAllowed(line) {
  const problem = checkCommand(line);
  if (problem !== null) {
    throw new ForbiddenCommandError(line, problem.reason);
  }
  return line;
}

/** Replace every occurrence of every secret with {@link MASK}. */
export function redact(text, secrets = []) {
  let out = String(text);
  for (const secret of secrets) {
    if (typeof secret === "string" && secret.length > 0) {
      out = out.split(secret).join(MASK);
    }
  }
  return out;
}
