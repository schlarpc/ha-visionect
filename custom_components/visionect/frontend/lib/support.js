/**
 * Whether this browser, on this origin, can talk to a serial port at all --
 * and if not, what the user is supposed to do about it.
 *
 * This is the first thing the panel runs and the most important thing it says.
 * Web Serial is only exposed in a **secure context**, which means HTTPS or a
 * loopback origin, and only in Chromium browsers. Plenty of Home Assistant
 * installs are plain `http://` on a LAN IP, where `navigator.serial` simply
 * does not exist. A panel that silently does nothing there is worse than no
 * panel, so the check is pure, total, and returns prose fit to put on screen.
 *
 * `http://homeassistant.local:8123` does **not** qualify. The secure-context
 * carve-out is for loopback addresses and the literal names `localhost` and
 * `*.localhost` -- an mDNS name that happens to resolve to the same machine is
 * still an insecure origin as far as the browser is concerned. That one trips
 * people up constantly, so it is called out by name.
 *
 * Pure on purpose: it takes a plain description of the environment rather than
 * reading globals, which is what makes every branch testable without a browser.
 */

/** Build the argument to {@link checkEnvironment} from the real globals. */
export function describeEnvironment(win = globalThis) {
  const nav = win.navigator ?? {};
  return {
    isSecureContext: Boolean(win.isSecureContext),
    hasSerial: Boolean(nav.serial),
    protocol: win.location?.protocol ?? "",
    hostname: win.location?.hostname ?? "",
    embedded: (() => {
      try {
        return win.top !== win.self;
      } catch {
        // A cross-origin parent throws on access, which itself means embedded.
        return true;
      }
    })(),
    brands: (nav.userAgentData?.brands ?? []).map((b) => b.brand),
    userAgent: nav.userAgent ?? "",
  };
}

function looksChromium(env) {
  if (env.brands.length > 0) {
    return env.brands.some((b) => /Chromium|Google Chrome|Microsoft Edge/i.test(b));
  }
  return /Chrome|Chromium|Edg\//.test(env.userAgent) && !/Firefox|FxiOS/.test(env.userAgent);
}

const TLS_REMEDIES = Object.freeze([
  "Open Home Assistant over HTTPS. Home Assistant Cloud (Nabu Casa) gives you that " +
    "with nothing to configure.",
  "Or put a reverse proxy with a TLS certificate in front of Home Assistant -- " +
    "Caddy, Traefik, or the Nginx Proxy Manager add-on -- and open the panel through it.",
  "Or, just for commissioning, open http://localhost:8123 in a browser running on the " +
    "Home Assistant host itself, with the sign plugged into that machine. A loopback " +
    "origin counts as secure even over plain HTTP.",
]);

/**
 * Can this page use Web Serial?
 *
 * @returns {{ok: boolean, code: string, title: string, detail: string,
 *            remedies: string[]}}
 */
export function checkEnvironment(env) {
  if (!env.isSecureContext) {
    const where = env.hostname ? `${env.protocol}//${env.hostname}` : "this address";
    const mdns = /\.local$/i.test(env.hostname);
    return {
      ok: false,
      code: "insecure-origin",
      title: "This page is not a secure context, so serial ports are not available",
      detail:
        `The browser only exposes serial ports to pages served over HTTPS, or from a ` +
        `loopback address such as http://localhost. This page came from ${where}, which ` +
        `is neither, so navigator.serial does not exist here at all -- there is nothing ` +
        `the panel can do about it from inside the page.` +
        (mdns
          ? ` Note that ${env.hostname} does not count as loopback even though it may ` +
            `resolve to this machine: the exemption is for loopback IP addresses and ` +
            `the literal name "localhost", not for any name that points at them.`
          : ""),
      remedies: [...TLS_REMEDIES],
    };
  }
  if (!env.hasSerial) {
    if (!looksChromium(env)) {
      return {
        ok: false,
        code: "no-serial-api",
        title: "This browser does not implement Web Serial",
        detail:
          "Web Serial is available in Chrome, Edge and other Chromium-based browsers. " +
          "Firefox and Safari have both declined to implement it, so there is no flag " +
          "to turn on. The origin is fine -- only the browser is in the way.",
        remedies: [
          "Open this page in Chrome or Edge.",
          "Or commission the sign from a serial terminal: the panel shows the exact " +
            "command sequence it would have run, and it is the same sequence typed by hand.",
        ],
      };
    }
    return {
      ok: false,
      code: "serial-blocked",
      title: "Serial ports are blocked on this page",
      detail:
        "This looks like a Chromium browser on a secure origin, but navigator.serial is " +
        "missing. That usually means an enterprise policy, a browser extension, or a " +
        "Permissions-Policy header from a reverse proxy in front of Home Assistant is " +
        "withholding the Serial API.",
      remedies: [
        "Check whether a reverse proxy is sending a Permissions-Policy header; it needs " +
          "to allow 'serial' for this origin.",
        "Check chrome://policy for a managed policy disabling device APIs.",
      ],
    };
  }
  if (env.embedded) {
    return {
      ok: false,
      code: "embedded",
      title: "This panel is inside a frame that is not allowed to open serial ports",
      detail:
        "A framed page can only use Web Serial if the frame carries " +
        'allow="serial". Home Assistant does not frame this panel itself, so something ' +
        "else is -- a dashboard webpage card, or an app wrapping the whole frontend.",
      remedies: [
        "Open the panel in its own tab rather than through the embedding page.",
      ],
    };
  }
  return {
    ok: true,
    code: "ok",
    title: "Serial ports are available",
    detail:
      "Plug the sign into this machine with its USB cable, then choose its port. It " +
      "presents as an FTDI FT232 (0403:6001).",
    remedies: [],
  };
}
