/**
 * The Visionect commissioning panel.
 *
 * A sign has to be *pointed at* Home Assistant. There is no discovery in
 * either direction -- the sign is a TCP client that dials out and runs no
 * listening service, and the eight TCLV fields a bootstrap needs are
 * `canWrite: false` over the network, so there is no over-the-air escape
 * either. Telling a sign about a server it cannot yet reach can only happen
 * over the cable. Once per sign, unavoidably.
 *
 * What this panel changes is *which machine* needs the cable. Before it, that
 * was a machine with Python, pyserial and a checkout on it. Now it is the
 * laptop the user already has Home Assistant open on.
 *
 * Plain ES modules, no build step, no framework, no dependencies. The custom
 * element is hand-rolled rather than Lit, because the panel has about nine
 * states and a framework would be the largest thing in the directory.
 *
 * The logic worth trusting lives in `lib/`, which is free of DOM and free of
 * Web Serial and therefore testable without a browser:
 *
 *   lib/guard.js      what may be sent, and what may never be
 *   lib/console.js    line discipline, echo/prompt framing, async log splitting
 *   lib/plan.js       the command sequences, and the address guess
 *   lib/sign.js       the identify handshake and the reply parsers
 *   lib/execute.js    running a plan and stopping at the first failure
 *   lib/support.js    whether this origin can use Web Serial at all
 *   lib/transport.js  Web Serial, as four methods
 */

import { SerialConsole } from "./lib/console.js";
import { describeOutcome, executePlan } from "./lib/execute.js";
import { ALLOWED_COMMANDS, DENIED_COMMANDS } from "./lib/guard.js";
import {
  DEFAULT_SERVER_PORT,
  PlanError,
  WIFI_SECURITY,
  planCommission,
  suggestServer,
} from "./lib/plan.js";
import { NotASignError, identify, readSnapshot, waitForConnection } from "./lib/sign.js";
import { checkEnvironment, describeEnvironment } from "./lib/support.js";
import { WebSerialTransport, requestPort } from "./lib/transport.js";

const STYLESHEET = new URL("./panel.css", import.meta.url).href;

const el = (tag, attrs = {}, children = []) => {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "class") {
      node.className = value;
    } else if (key === "text") {
      node.textContent = value;
    } else if (key === "html") {
      node.innerHTML = value;
    } else if (key.startsWith("on")) {
      node.addEventListener(key.slice(2), value);
    } else if (value !== null && value !== undefined && value !== false) {
      node.setAttribute(key, value === true ? "" : String(value));
    }
  }
  for (const child of [].concat(children)) {
    if (child !== null && child !== undefined) {
      node.append(child);
    }
  }
  return node;
};

const card = (title, children, extraClass = "") =>
  el("section", { class: `card ${extraClass}`.trim() }, [
    title ? el("h2", { text: title }) : null,
    ...[].concat(children),
  ]);

class VisionectCommissioningPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._env = checkEnvironment(describeEnvironment(globalThis));
    this._state = this._env.ok ? "idle" : "blocked";
    this._message = null;
    this._sign = null;
    this._snapshot = null;
    this._plan = null;
    this._stream = [];
    this._console = null;
    this._transport = null;
    this._form = {
      changeWifi: true,
      ssid: "",
      security: WIFI_SECURITY.WPA2,
      host: "",
      port: DEFAULT_SERVER_PORT,
      reconnect: true,
    };
    this._suggestion = null;
    // The passphrase lives only in the <input>. It is read once, at the moment
    // of execution, into a local that is dropped as soon as the run ends, and
    // the input is cleared then too. JS strings cannot be wiped, so this is
    // "held as briefly as possible", not "scrubbed".
    this._pskInput = null;
  }

  connectedCallback() {
    this._render();
  }

  disconnectedCallback() {
    // Leaving the page must give the port back; nothing else on the machine
    // can open it while this holds it.
    this._disconnect().catch(() => {});
  }

  set panel(value) {
    this._panel = value;
    const config = value?.config ?? {};
    this._suggestion = suggestServer({
      listener: { host: config.listener_host ?? "", port: config.listener_port ?? null },
      location: globalThis.location,
      defaultPort: config.listener_port ?? DEFAULT_SERVER_PORT,
    });
    this._form.host = this._suggestion.host;
    this._form.port = this._suggestion.port;
    this._render();
  }

  get panel() {
    return this._panel;
  }

  set hass(value) {
    this._hass = value;
  }

  // -------------------------------------------------------------- transitions

  _set(state, message = null) {
    this._state = state;
    this._message = message;
    this._render();
  }

  _note(direction, text) {
    this._stream.push({ direction, text });
    if (this._stream.length > 2000) {
      this._stream.splice(0, this._stream.length - 2000);
    }
    this._paintStream();
  }

  async _connect() {
    this._set("connecting", "Waiting for a port to be chosen…");
    let port;
    try {
      port = await requestPort();
    } catch (err) {
      // A cancelled picker is a NotFoundError, and is not an error worth
      // shouting about.
      this._set("idle", /NotFound/.test(err.name) ? null : `Could not open the port: ${err.message}`);
      return;
    }
    const transport = new WebSerialTransport(port);
    try {
      await transport.open();
    } catch (err) {
      this._set(
        "idle",
        `The port would not open: ${err.message}. Something else may have it -- a ` +
          "serial terminal, or a script still running on this machine.",
      );
      return;
    }
    this._transport = transport;
    this._console = new SerialConsole(transport, {
      onLog: (entry) => this._note("log", entry.text),
      onTraffic: ({ direction, text }) => this._note(direction, text),
    });
    this._set("identifying", "Asking what is on the other end…");
    try {
      this._sign = await identify(this._console);
    } catch (err) {
      await this._disconnect();
      if (err instanceof NotASignError) {
        this._set(
          "idle",
          `That port is not a Visionect sign, so the panel will not send anything to ` +
            `it. ${err.message}`,
        );
      } else {
        this._set("idle", `The port opened but did not answer as expected: ${err.message}`);
      }
      return;
    }
    await this._refresh();
  }

  async _refresh() {
    if (this.shadowRoot?.getElementById("host")) {
      // Keep anything half-typed; a re-read rebuilds the form from _form.
      this._readForm();
    }
    this._set("reading", "Reading the sign's configuration…");
    try {
      this._snapshot = await readSnapshot(this._console, {
        onProgress: (command) => this._set("reading", `Reading ${command}…`),
      });
    } catch (err) {
      this._set("ready", `Read failed: ${err.message}`);
      return;
    }
    // Prefill the WiFi name from what the sign already has: the overwhelmingly
    // common case is "same network, new server", and retyping an SSID is how
    // typos get in.
    if (!this._form.ssid && this._snapshot.wifi?.ssid) {
      this._form.ssid = this._snapshot.wifi.ssid;
    }
    if (this._snapshot.wifi?.security) {
      this._form.security = this._snapshot.wifi.security;
    }
    this._set("ready", null);
  }

  async _disconnect() {
    const console_ = this._console;
    this._console = null;
    this._transport = null;
    try {
      await console_?.close();
    } catch {
      /* going away anyway */
    }
  }

  _review() {
    this._readForm();
    try {
      const secured = this._form.security !== WIFI_SECURITY.OPEN;
      this._plan = planCommission({
        ssid: this._form.changeWifi ? this._form.ssid : null,
        psk: this._form.changeWifi && secured ? this._pskInput?.value ?? "" : null,
        security: this._form.security,
        host: this._form.host,
        port: this._form.port,
        reconnect: this._form.reconnect,
      });
    } catch (err) {
      if (err instanceof PlanError) {
        this._plan = null;
        this._set("ready", err.message);
        return;
      }
      throw err;
    }
    this._set("reviewing", null);
  }

  async _run() {
    const plan = this._plan;
    // Read the passphrase at the last possible moment, keep it in one local,
    // and drop it before this function returns.
    let secrets = { psk: this._pskInput?.value ?? "" };
    this._set("running", "Running the plan…");
    try {
      const outcome = await executePlan(this._console, plan, {
        secrets,
        onStep: ({ phase, index, total, step, error }) => {
          if (phase === "start") {
            this._set("running", `${index + 1}/${total}  ${step.display}`);
          } else if (phase === "error") {
            this._note("error", `${step.display}: ${error.message}`);
          }
        },
      });
      this._outcome = { plan, ...outcome, summary: describeOutcome(plan, outcome) };
    } finally {
      secrets = null;
      if (this._pskInput) {
        this._pskInput.value = "";
      }
    }
    if (this._outcome.error !== null) {
      this._set("done", null);
      return;
    }
    if (!this._form.reconnect) {
      this._set("done", null);
      return;
    }
    this._set("waiting", "Waiting for the sign to dial Home Assistant…");
    const state = await waitForConnection(this._console, {
      onPoll: (poll) => this._set("waiting", `Connection state: ${poll.state || "unknown"}`),
    });
    this._outcome.connection = state;
    await this._refreshQuietly();
    this._set("done", null);
  }

  async _refreshQuietly() {
    try {
      this._snapshot = await readSnapshot(this._console);
    } catch {
      /* the outcome card is what matters now */
    }
  }

  _readForm() {
    const root = this.shadowRoot;
    const value = (id) => root.getElementById(id)?.value ?? "";
    const checked = (id) => Boolean(root.getElementById(id)?.checked);
    this._form.changeWifi = checked("change-wifi");
    // Not trimmed: wifi_ssid_set takes the rest of the line verbatim, so a
    // leading or trailing space in the name is a real SSID and trimming it
    // here would quietly set a different network.
    if (root.getElementById("ssid")) {
      this._form.ssid = value("ssid");
    }
    this._form.security = value("security") || this._form.security;
    this._form.host = value("host");
    this._form.port = value("port");
    this._form.reconnect = checked("reconnect");
  }

  // ------------------------------------------------------------------ painting

  _render() {
    if (!this.shadowRoot) {
      return;
    }
    const root = this.shadowRoot;
    root.replaceChildren(
      el("link", { rel: "stylesheet", href: STYLESHEET }),
      el("div", { class: "page" }, [
        el("header", {}, [
          el("h1", { text: "Commission a Visionect sign" }),
          el("p", {
            class: "lede",
            text:
              "A sign has to be told where Home Assistant is, over its USB cable, " +
              "once. Nothing discovers anything here: the sign dials out and never " +
              "listens, and its server address cannot be written over the network. " +
              "Plug it into this machine and this page can do the rest.",
          }),
        ]),
        this._environmentCard(),
        ...(this._env.ok ? this._mainCards() : []),
        this._safetyCard(),
      ]),
    );
    this._paintStream();
  }

  _environmentCard() {
    const ok = this._env.ok;
    return card(
      null,
      [
        el("div", { class: `banner ${ok ? "good" : "bad"}` }, [
          el("strong", { text: this._env.title }),
          el("p", { text: this._env.detail }),
          this._env.remedies.length
            ? el(
                "ul",
                {},
                this._env.remedies.map((r) => el("li", { text: r })),
              )
            : null,
        ]),
      ],
      "env",
    );
  }

  _mainCards() {
    if (this._state === "idle" || this._state === "connecting" || this._state === "identifying") {
      return [this._connectCard()];
    }
    return [
      this._signCard(),
      this._state === "reviewing" ? this._reviewCard() : null,
      this._state === "done" ? this._outcomeCard() : null,
      this._state === "reviewing" || this._state === "done" ? null : this._formCard(),
      this._streamCard(),
    ].filter(Boolean);
  }

  _connectCard() {
    const busy = this._state !== "idle";
    return card("1. Choose the sign's serial port", [
      el("p", {
        text:
          "The sign appears as an FTDI FT232 serial port (0403:6001). The browser " +
          "will ask you which port to use -- it will not let this page look at ports " +
          "you have not picked.",
      }),
      el("button", {
        class: "primary",
        disabled: busy,
        onclick: () => this._connect(),
        text: busy ? "Working…" : "Choose serial port",
      }),
      this._message ? el("p", { class: "problem", text: this._message }) : null,
    ]);
  }

  _signCard() {
    const s = this._snapshot ?? {};
    const rows = [
      ["UUID", this._sign?.uuid],
      ["Firmware", this._sign?.firmware?.version],
      ["Hardware", this._sign?.firmware?.hardware],
      ["Panel", this._sign?.firmware?.panel],
      ["Server", s.server ? `${s.server.host ?? "?"}:${s.server.port ?? "?"}` : null],
      ["WiFi network", s.wifi ? `${s.wifi.ssid || "(none)"} (${s.wifi.security || "?"})` : null],
      ["Connection", s.connection?.state],
      ["Signal", s.rssi === null || s.rssi === undefined ? null : `${s.rssi} dBm`],
      [
        "Battery",
        s.charger
          ? `${s.charger.mode ?? "?"}, ${s.charger.voltageMv ?? "?"} mV, ${s.charger.currentMa ?? "?"} mA`
          : null,
      ],
      ["Temperature", s.temperature === null || s.temperature === undefined ? null : `${s.temperature} °C`],
      ["Uptime", s.uptime === null || s.uptime === undefined ? null : `${s.uptime} min`],
    ];
    const errors = Object.entries(s.errors ?? {});
    return card("2. What the sign currently thinks", [
      el(
        "dl",
        { class: "readout" },
        rows.flatMap(([label, value]) => [
          el("dt", { text: label }),
          el("dd", { class: value ? "" : "unknown", text: value ?? "not reported" }),
        ]),
      ),
      s.connection && !s.connection.connected
        ? el("p", {
            class: "problem",
            text:
              `The sign is not holding a session (${s.connection.state || "unknown"}). ` +
              "That is expected before commissioning, and is what the last two steps fix.",
          })
        : null,
      errors.length
        ? el("details", {}, [
            el("summary", { text: `${errors.length} command(s) did not answer` }),
            el(
              "ul",
              {},
              errors.map(([command, why]) => el("li", { text: `${command}: ${why}` })),
            ),
          ])
        : null,
      el("div", { class: "actions" }, [
        el("button", {
          onclick: () => this._refresh(),
          disabled: this._state === "running" || this._state === "waiting",
          text: "Re-read",
        }),
        el("button", {
          onclick: () => this._disconnect().then(() => this._set("idle", null)),
          disabled: this._state === "running" || this._state === "waiting",
          text: "Release port",
        }),
      ]),
      this._message && this._state !== "reviewing" ? el("p", { class: "problem", text: this._message }) : null,
    ]);
  }

  _formCard() {
    const f = this._form;
    const suggestion = this._suggestion;
    return card("3. Point it at this Home Assistant", [
      el("div", { class: "field row" }, [
        el("input", {
          type: "checkbox",
          id: "change-wifi",
          checked: f.changeWifi,
          onchange: () => {
            this._readForm();
            this._render();
          },
        }),
        el("label", { for: "change-wifi", text: "Also set the WiFi network" }),
      ]),
      f.changeWifi
        ? el("div", { class: "subform" }, [
            el("div", { class: "field" }, [
              el("label", { for: "ssid", text: "Network name (SSID)" }),
              el("input", { type: "text", id: "ssid", value: f.ssid, autocomplete: "off", spellcheck: "false" }),
              el("small", {
                text:
                  "Sent exactly as typed. There is no quoting: quotes and backslashes " +
                  "would become part of the name. Spaces are fine; a tab is not.",
              }),
            ]),
            el("div", { class: "field" }, [
              el("label", { for: "security", text: "Security" }),
              el(
                "select",
                {
                  id: "security",
                  onchange: () => {
                    this._readForm();
                    this._render();
                  },
                },
                Object.values(WIFI_SECURITY).map((value) =>
                  el("option", { value, selected: value === f.security, text: value }),
                ),
              ),
            ]),
            f.security === WIFI_SECURITY.OPEN
              ? null
              : el("div", { class: "field" }, [
                  el("label", { for: "psk", text: "Passphrase" }),
                  this._makePskInput(),
                  el("small", {
                    text:
                      "Never shown, never logged, and masked out of the console stream " +
                      "below -- including the sign's own echo of it. There is no way to " +
                      "read it back off a sign, so a wrong one shows up only as a sign " +
                      "that will not associate.",
                  }),
                ]),
          ])
        : null,
      el("div", { class: "field" }, [
        el("label", { for: "host", text: "Home Assistant address, as the sign will see it" }),
        el("input", { type: "text", id: "host", value: f.host, autocomplete: "off", spellcheck: "false", placeholder: "10.0.0.5" }),
        suggestion?.warning
          ? el("small", { class: "warn", text: suggestion.warning })
          : el("small", {
              text:
                suggestion?.source === "listener"
                  ? "Taken from the address this integration's listener is actually bound to."
                  : "Check this is reachable from the sign's network.",
            }),
      ]),
      el("div", { class: "field" }, [
        el("label", { for: "port", text: "Port" }),
        el("input", { type: "number", id: "port", value: f.port, min: "1", max: "65535" }),
      ]),
      el("div", { class: "field row" }, [
        el("input", { type: "checkbox", id: "reconnect", checked: f.reconnect }),
        el("label", {
          for: "reconnect",
          text: "Re-dial immediately (cs 1 then cs 3) instead of waiting for the sign to try again",
        }),
      ]),
      el("div", { class: "actions" }, [
        el("button", { class: "primary", onclick: () => this._review(), text: "Show me the commands" }),
      ]),
    ]);
  }

  _makePskInput() {
    // Kept as one long-lived node rather than rebuilt on every render, so a
    // re-render cannot drop a half-typed passphrase -- and so there is exactly
    // one place in the DOM that has ever held it.
    if (!this._pskInput) {
      this._pskInput = el("input", { type: "password", id: "psk", autocomplete: "off", spellcheck: "false" });
    }
    return this._pskInput;
  }

  _reviewCard() {
    const plan = this._plan;
    return card(
      "4. Confirm. These are the exact commands, in this order",
      [
        el(
          "ol",
          { class: "plan" },
          plan.steps.map((step) =>
            el("li", {}, [el("code", { text: step.display }), el("span", { class: "why", text: step.why })]),
          ),
        ),
        el("div", { class: "banner warn" }, [
          el("strong", { text: "Before you confirm" }),
          el(
            "ul",
            {},
            plan.notes.map((note) => el("li", { text: note })),
          ),
        ]),
        el("div", { class: "actions" }, [
          el("button", { class: "primary", onclick: () => this._run(), text: "Run these commands" }),
          el("button", { onclick: () => this._set("ready", null), text: "Back" }),
        ]),
      ],
      "review",
    );
  }

  _outcomeCard() {
    const outcome = this._outcome ?? {};
    const good = outcome.error === null;
    const connected = outcome.connection?.connected;
    return card(
      "Result",
      [
        el("div", { class: `banner ${good ? "good" : "bad"}` }, [
          el("strong", { text: good ? "The plan ran" : "The plan stopped early" }),
          el("p", { text: outcome.summary ?? "" }),
          outcome.error ? el("p", { class: "problem", text: outcome.error.message }) : null,
        ]),
        outcome.connection
          ? el("div", { class: `banner ${connected ? "good" : "warn"}` }, [
              el("strong", {
                text: connected
                  ? "The sign is holding a session with Home Assistant"
                  : "The sign has not come back yet",
              }),
              el("p", {
                text: connected
                  ? `conn_state_get reports "${outcome.connection.state}". The sign should ` +
                    "appear in Home Assistant now, and the cable can come out."
                  : `conn_state_get still reports "${outcome.connection.state}". Leave the ` +
                    "cable in: the address is committed to flash, so the cable is the only " +
                    "way to change it. Re-read below, or run cs 1 and cs 3 again.",
              }),
            ])
          : null,
        el("div", { class: "actions" }, [
          el("button", { class: "primary", onclick: () => this._refresh(), text: "Re-read the sign" }),
          el("button", { onclick: () => this._set("ready", null), text: "Change something else" }),
        ]),
      ],
      "outcome",
    );
  }

  _streamCard() {
    const pane = el("pre", { class: "stream", id: "stream" });
    this._streamPane = pane;
    return card("Console", [
      el("p", {
        class: "hint",
        text:
          "Everything on the wire, as it happens. The sign narrates to this same " +
          "port unprompted -- a heartbeat every minute is eight lines of it -- so " +
          "log lines are marked and kept out of command replies.",
      }),
      pane,
      el("div", { class: "actions" }, [
        el("button", {
          onclick: () => {
            this._stream = [];
            this._paintStream();
          },
          text: "Clear",
        }),
        el("button", {
          onclick: () =>
            navigator.clipboard?.writeText(
              this._stream.map((l) => `${prefixFor(l.direction)}${l.text}`).join("\n"),
            ),
          text: "Copy",
        }),
      ]),
      this._state === "running" || this._state === "waiting" || this._state === "reading"
        ? el("p", { class: "busy", text: this._message ?? "Working…" })
        : null,
    ]);
  }

  _paintStream() {
    const pane = this._streamPane;
    if (!pane || !pane.isConnected) {
      return;
    }
    pane.replaceChildren(
      ...this._stream.map((line) =>
        el("span", { class: `line ${line.direction}`, text: `${prefixFor(line.direction)}${line.text}\n` }),
      ),
    );
    pane.scrollTop = pane.scrollHeight;
  }

  _safetyCard() {
    return card(
      "What this panel will not do",
      [
        el("p", {
          text:
            "The command allow list lives in lib/guard.js and is checked inside the " +
            "transport, not in the buttons, so these cannot be sent from this page " +
            "even by accident.",
        }),
        el(
          "ul",
          { class: "denied" },
          Object.entries(DENIED_COMMANDS).map(([name, why]) =>
            el("li", {}, [el("code", { text: name }), el("span", { class: "why", text: why })]),
          ),
        ),
        el("p", {
          class: "hint",
          text:
            `Families refused by pattern too: anything ending _upgrade, the feat_*, ` +
            `dcm*, bsim* and encryption_*_set families, and anything ending format. ` +
            `Only ${ALLOWED_COMMANDS.size} commands are permitted at all, and ` +
            `wifi_conf_set and reboot are deliberately not among them.`,
        }),
      ],
      "safety",
    );
  }
}

function prefixFor(direction) {
  return { out: "> ", in: "  ", log: "~ ", error: "! " }[direction] ?? "  ";
}

if (!customElements.get("visionect-commissioning-panel")) {
  customElements.define("visionect-commissioning-panel", VisionectCommissioningPanel);
}
