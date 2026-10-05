/**
 * Commissioning plans: the exact sequence of command lines a flow would send,
 * built without touching the port.
 *
 * A plan is produced first, shown to the user, and only then executed. That is
 * not decoration. Every command in here is in the tier that costs a trip with
 * a cable if it is wrong, and a half-run plan leaves the sign on the new WiFi
 * and the old server, or the other way round. Printing the plan is the only
 * way to review a bootstrap *before* it strands a sign somewhere you cannot
 * reach it.
 *
 * Ported from `pyvisionect.io.usb.provisioning`, with three deliberate
 * differences:
 *
 * - **`wifi_conf_set` is never used.** The Python picks it when the SSID has no
 *   whitespace and falls back to the three single-value setters otherwise.
 *   Here it is always the three setters, because `wifi_conf_set` is positional
 *   -- it splits on whitespace -- and because routing the passphrase through
 *   its own single command is what lets the passphrase be kept out of the plan
 *   object entirely. The cost is that WIFI_BAND (TCLV 68) is never written;
 *   `wifi_conf_set` is its only writer and there is no `wifi_band_set`. The
 *   device keeps whatever band it already has, which for dual-band (0, the
 *   factory default) is what you want anyway.
 * - **`reboot` is replaced by `cs 1` then `cs 3`.** A reboot blanks the glass
 *   for about a minute. Dropping connectivity to state 1 and climbing back to
 *   state 3 re-dials the server in seconds and leaves the picture up.
 * - **The passphrase is not in the plan.** The step that carries it has
 *   `secret: "psk"` and no `command`; {@link renderStep} fills it in at the
 *   moment of writing, from a secrets object the UI holds and then drops.
 *
 * `cs 3` on its own is a no-op while a TCP session is already open -- it
 * answers `Connectivity in state 3` and the existing session keeps
 * heartbeating. That is the single most confusing step in commissioning, and
 * the reason both halves are always emitted together.
 */

/** The port a sign dials out to, and the port the integration listens on. */
export const DEFAULT_SERVER_PORT = 11113;

/** `WIFI_SECURITY` (TCLV 66) values. ASCII strings, not integers. */
export const WIFI_SECURITY = Object.freeze({
  OPEN: "none",
  WPA2: "wpa2",
  WPA2_ENTERPRISE: "wpa2e",
});

export const WIFI_SECURITY_VALUES = Object.freeze(Object.values(WIFI_SECURITY));

/** The key under which the passphrase is passed to {@link renderStep}. */
export const SECRET_PSK = "psk";

/** A plan could not be built. The message is meant to be shown verbatim. */
export class PlanError extends Error {
  constructor(message, field = null) {
    super(message);
    this.name = "PlanError";
    this.field = field;
  }
}

function step({ command = null, name = null, secret = null, why, display = null, expectPrompt = true }) {
  if (command === null && secret === null) {
    throw new Error("a step needs either a command or a secret");
  }
  const word = name ?? String(command).split(/\s+/, 1)[0];
  return Object.freeze({
    name: word,
    command,
    secret,
    expectPrompt,
    why,
    // What the user is shown and what the transcript records. For a secret
    // step this is the only form that exists until the moment of writing.
    display: display ?? command ?? `${word} ••••••••`,
  });
}

/**
 * The line to write for *step*, filling in a secret if it needs one.
 *
 * The only function in the panel that produces a string containing the
 * passphrase, and the string is handed straight to
 * `SerialConsole.command(line, {secrets: [psk]})`, which masks it out of
 * everything it records.
 */
export function renderStep(aStep, secrets = {}) {
  if (aStep.secret === null) {
    return aStep.command;
  }
  const value = secrets[aStep.secret];
  if (typeof value !== "string" || value === "") {
    throw new PlanError(`step ${aStep.name} needs the ${aStep.secret}, and none was given`);
  }
  return `${aStep.name} ${value}`;
}

function plan(name, steps, notes) {
  return Object.freeze({
    name,
    steps: Object.freeze(steps),
    notes: Object.freeze(notes),
    /** Just the display lines -- what the confirmation dialog shows. */
    get display() {
      return this.steps.map((s) => s.display);
    },
    /** Which steps carry a secret, so the UI knows what it must still hold. */
    get secrets() {
      return [...new Set(this.steps.map((s) => s.secret).filter((s) => s !== null))];
    },
  });
}

// --------------------------------------------------------------------- WiFi

const LOOPBACK = new Set(["localhost", "127.0.0.1", "::1", "0.0.0.0", "[::]", "::", "0:0:0:0:0:0:0:1"]);

/**
 * Set the WiFi credentials -- TCLV 67, 66 and 65, in that order.
 *
 * SSID last on purpose: it is the field that decides association, so it is
 * written once everything else is in place.
 *
 * `wifi_ssid_set` takes everything after the command name and one separating
 * space **verbatim** -- interior spaces, runs of spaces, apostrophes, quotes
 * and backslashes all land in TCLV 65 byte for byte, measured on 7.4.4407.
 * There is no quoting or escaping convention, so the SSID is sent raw and
 * `"My WiFi"` would set an SSID whose first and last characters are quotes.
 */
export function planWifi({ ssid, psk, security = WIFI_SECURITY.WPA2 }) {
  if (!WIFI_SECURITY_VALUES.includes(security)) {
    throw new PlanError(
      `security must be one of ${WIFI_SECURITY_VALUES.join(", ")} -- these are ` +
        `ASCII strings, not integers. Got ${JSON.stringify(security)}`,
      "security",
    );
  }
  if (typeof ssid !== "string" || ssid === "") {
    throw new PlanError("the SSID is required", "ssid");
  }
  for (const [char, label] of [
    ["\t", "a tab"],
    ["\r", "a carriage return"],
    ["\n", "a newline"],
  ]) {
    if (ssid.includes(char)) {
      throw new PlanError(
        `the SSID contains ${label}, which this console cannot carry. CR and LF ` +
          "submit the line; TAB is the firmware line editor's usage-lookup key and " +
          "is swallowed, so 'Two<TAB>Words' would set 'TwoWords'. Spaces are fine.",
        "ssid",
      );
    }
  }
  // An SSID is at most 32 octets. The firmware does not appear to check, and a
  // silently truncated SSID is a sign that will not associate.
  const octets = new TextEncoder().encode(ssid).length;
  if (octets > 32) {
    throw new PlanError(
      `the SSID is ${octets} bytes; the 802.11 limit is 32, and a truncated SSID ` +
        "is a sign that will not associate",
      "ssid",
    );
  }

  const steps = [];
  const open = security === WIFI_SECURITY.OPEN;
  if (open) {
    if (psk) {
      throw new PlanError("an open network takes no passphrase", "psk");
    }
  } else {
    if (typeof psk !== "string" || psk === "") {
      throw new PlanError("the passphrase is required for a secured network", "psk");
    }
    if (/\s/.test(psk)) {
      // wifi_psk_set is a single-argument command of the same shape as
      // wifi_ssid_set, so it very probably would carry a space. But TCLV 67
      // has no read path -- wifi_conf_get returns SSID, security and band and
      // never the passphrase -- so there is no way to check what landed. A
      // truncated passphrase does not fail loudly; it stops the sign
      // associating and the firmware then power-cycles itself on
      // "E: Max conn errs. Reboot" with ErrorCode still 0x0. Guessing here is
      // not recoverable without the cable.
      throw new PlanError(
        "a passphrase containing whitespace is refused. There is no read path for " +
          "TCLV 67 -- wifi_conf_get never returns the passphrase -- so nothing can " +
          "confirm what landed, and a truncated one fails silently as a sign that " +
          "will not associate and then power-cycles itself. Rename the network.",
        "psk",
      );
    }
    if (security === WIFI_SECURITY.WPA2 && (psk.length < 8 || psk.length > 63)) {
      throw new PlanError(
        `a WPA2 passphrase is 8 to 63 characters; this one is ${psk.length}. The ` +
          "firmware will accept it and then simply never associate.",
        "psk",
      );
    }
    steps.push(
      step({
        name: "wifi_psk_set",
        secret: SECRET_PSK,
        why:
          "writes TCLV 67. Its own command, not an argument to wifi_conf_set, so the " +
          "passphrase never has to be shown, logged or whitespace-split.",
      }),
    );
  }
  steps.push(
    step({
      command: `wifi_security_set ${security}`,
      why: "writes TCLV 66, an ASCII string -- 'none', 'wpa2' or 'wpa2e'.",
    }),
    step({
      command: `wifi_ssid_set ${ssid}`,
      why:
        "writes TCLV 65. Hidden from this firmware's help but present and working, " +
        "and it takes the rest of the line verbatim -- which is what carries a space " +
        "in the name. Last on purpose: the SSID is what decides association.",
    }),
  );
  return plan("wifi", steps, [
    "RAM only until flash_save.",
    "wifi_conf_get reads back the SSID, security and band -- never the passphrase. " +
      "There is no read path for TCLV 67 at all.",
    "WIFI_BAND (TCLV 68) is not written: wifi_conf_set is its only writer and this " +
      "plan does not use wifi_conf_set. The sign keeps the band it already has.",
  ]);
}

// ------------------------------------------------------------------- server

/**
 * Reject a server address that would not work, with the reason.
 *
 * The loopback check is the one that matters. Web Serial needs a secure
 * context, and `http://localhost:8123` is the one plain-HTTP origin that
 * qualifies -- so the panel is most likely to be open on exactly the URL that
 * must never be written into a sign. A sign told to dial `127.0.0.1` dials
 * itself, finds nothing, and after `flash_save` the only way back is the cable.
 */
export function checkServerHost(host) {
  if (typeof host !== "string" || host.trim() === "") {
    throw new PlanError("the Home Assistant address is required", "host");
  }
  const value = host.trim();
  if (/\s/.test(value)) {
    throw new PlanError("the address cannot contain a space", "host");
  }
  if (/^[a-z][a-z0-9+.-]*:\/\//i.test(value)) {
    throw new PlanError(
      `use a bare host, not a URL: ${JSON.stringify(value)} should be just the ` +
        "hostname or IP. The sign speaks its own TCP protocol, not HTTP.",
      "host",
    );
  }
  if (value.includes("/")) {
    throw new PlanError("use a bare host with no path", "host");
  }
  if (LOOPBACK.has(value.toLowerCase())) {
    throw new PlanError(
      `${value} is this browser's own machine, not an address the sign can reach. ` +
        "The sign dials out over WiFi, so it needs the LAN address or DNS name of " +
        "the Home Assistant host. Writing a loopback address and committing it with " +
        "flash_save leaves the serial cable as the only way back.",
      "host",
    );
  }
  if (value.length > 253) {
    throw new PlanError("that address is longer than a DNS name can be", "host");
  }
  if (!/^[A-Za-z0-9._:\-[\]]+$/.test(value)) {
    throw new PlanError(
      `${JSON.stringify(value)} is not a hostname or an IP address`,
      "host",
    );
  }
  return value;
}

export function checkServerPort(port) {
  const value = Number(port);
  if (!Number.isInteger(value) || value < 1 || value > 65535) {
    throw new PlanError(`${JSON.stringify(port)} is not a TCP port`, "port");
  }
  return value;
}

/**
 * Point a sign at a server, leaving WiFi alone.
 *
 * `server_tcp_set` -> `flash_save` -> `cs 1` -> `cs 3`. The common case: the
 * sign is already on a network you control and you only want it talking to
 * your Home Assistant instead of the vendor's suite.
 */
export function planRepoint({ host, port = DEFAULT_SERVER_PORT, reconnect = true }) {
  const server = checkServerHost(host);
  const tcpPort = checkServerPort(port);
  const steps = [
    step({
      command: `server_tcp_set ${server} ${tcpPort}`,
      why:
        "writes TCLV 18 (server IP or DNS name) and 19 (port) to RAM. Both are " +
        "canWrite:false over the network, which is the whole reason this needs a cable.",
    }),
    step({
      command: "flash_save",
      why:
        "TCLV 53. Commits everything above to flash. Mandatory -- every setter so far " +
        "was RAM-only, and a power-cycle would undo the lot. After this, the cable is " +
        "the only way back.",
    }),
  ];
  if (reconnect) {
    steps.push(
      step({
        command: "cs 1",
        why:
          "drops connectivity to radio-on/unassociated. This is the step that actually " +
          "tears the existing TCP session down.",
      }),
      step({
        command: "cs 3",
        why:
          "climbs back: associate, DHCP, dial the new server. On its own, while a " +
          "session is still open, this is a no-op that just answers 'Connectivity in " +
          "state 3' -- which is why it is always paired with cs 1.",
      }),
    );
  }
  return plan("repoint", steps, serverNotes(server, tcpPort, reconnect));
}

function serverNotes(server, port, reconnect) {
  const notes = [
    "Nothing persists until flash_save, so the setters can be run, read back, and " +
      "undone with a power-cycle right up until that step.",
    "If the sign already holds a DNS *name* for its server, re-pointing that name at " +
      "this machine moves it with no commands at all. Check what server_tcp_get says " +
      "before writing anything.",
    `After this the sign dials ${server}:${port}. If it cannot get there, TCLV 18 is ` +
      "read-only over the network, so the cable is the only way back. Keep it plugged " +
      "in until the connection state reads 'tcp open'.",
  ];
  if (reconnect) {
    notes.push(
      "cs 1 then cs 3 re-dials in seconds and leaves the picture on the glass. A " +
        "reboot would also work and costs about a minute of blank screen.",
    );
  } else {
    notes.push(
      "No reconnect step: the sign keeps its current session until it re-dials on its " +
        "own, which can be the best part of an hour after a flash_save.",
    );
  }
  return notes;
}

// ---------------------------------------------------------------- bootstrap

/**
 * The full first-time sequence: WiFi, then server, then commit, then re-dial.
 *
 * Pass `ssid: null` to leave WiFi untouched, which makes this
 * {@link planRepoint}.
 */
export function planCommission({
  ssid = null,
  psk = null,
  security = WIFI_SECURITY.WPA2,
  host,
  port = DEFAULT_SERVER_PORT,
  reconnect = true,
}) {
  const steps = [];
  const notes = [];
  if (ssid !== null && ssid !== "") {
    const wifi = planWifi({ ssid, psk, security });
    steps.push(...wifi.steps);
    notes.push(...wifi.notes);
  }
  const repoint = planRepoint({ host, port, reconnect });
  steps.push(...repoint.steps);
  notes.push(...repoint.notes);
  notes.push(
    "Read the whole sequence before confirming. A half-run plan leaves the sign on the " +
      "new WiFi and the old server, or the other way round, which is the worst of both.",
  );
  return plan("commission", steps, notes);
}

// ------------------------------------------------------------- address guess

/**
 * Where to point the sign, guessed from what the page knows -- and when not to
 * guess.
 *
 * Getting this right automatically is most of the value of the panel, and
 * getting it wrong is the one mistake that costs a cable. So the guess is
 * explicit about its own confidence.
 *
 * `listener` is what the integration itself reported through the panel config:
 * the address and port its TCP listener is actually bound to. That beats the
 * page URL in every case, because the page URL is where the *browser* reached
 * Home Assistant, which may be a cloud relay, a reverse proxy, or loopback --
 * none of which a sign can dial.
 *
 * @returns {{host: string, port: number, source: string, warning: string|null}}
 */
export function suggestServer({ listener = null, location = null, defaultPort = DEFAULT_SERVER_PORT } = {}) {
  const port = listener?.port ?? defaultPort;

  const listenerHost = listener?.host ?? "";
  if (listenerHost && !LOOPBACK.has(listenerHost.toLowerCase())) {
    return {
      host: listenerHost,
      port,
      source: "listener",
      warning: null,
    };
  }

  const hostname = (location?.hostname ?? "").replace(/^\[|\]$/g, "");
  if (!hostname || LOOPBACK.has(hostname.toLowerCase())) {
    return {
      host: "",
      port,
      source: "none",
      warning:
        "This page is open on localhost, which is the one plain-HTTP address Web " +
        "Serial accepts -- and an address no sign can dial. Type the LAN address or " +
        "DNS name of this Home Assistant host instead.",
    };
  }
  if (/\.ui\.nabu\.casa$/i.test(hostname)) {
    return {
      host: "",
      port,
      source: "none",
      warning:
        "This page came in over Nabu Casa Cloud, which relays HTTPS only. The sign " +
        "speaks its own TCP protocol on port " +
        `${port} and cannot use the relay, so it needs the LAN address of this ` +
        "Home Assistant host. Type it in.",
    };
  }
  if (/\.local$/i.test(hostname)) {
    return {
      host: hostname,
      port,
      source: "location",
      warning:
        `${hostname} is an mDNS name. The sign resolves names through the DNS server ` +
        "its DHCP lease gives it and has no mDNS resolver, so this will probably not " +
        "resolve. An IP address is safer.",
    };
  }
  return {
    host: hostname,
    port,
    source: "location",
    warning:
      "Taken from the address of this page. Check it is reachable from the sign's " +
      "network, not just from this browser -- a reverse proxy hostname often is not.",
  };
}
