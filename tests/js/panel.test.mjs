/**
 * Unit tests for the parts of the commissioning panel that do not need a
 * browser: the guard, the console framing, the plan builder, the identify
 * handshake, the reply parsers and the executor.
 *
 * Web Serial itself cannot be driven from here -- the API needs a real browser
 * and a user gesture -- so the division of labour is deliberate: every
 * decision the panel makes lives in a DOM-free module under `frontend/lib/`
 * and is tested here against captures taken off real hardware, while the only
 * thing left for a human with a cable to confirm is that the port opens.
 *
 * Run with `node --test tests/js/`, which `tests/test_panel_js.py` also does
 * so a plain `pytest` covers it.
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  ALLOWED_COMMANDS,
  DENIED_COMMANDS,
  ForbiddenCommandError,
  MASK,
  assertAllowed,
  checkCommand,
  commandName,
  redact,
} from "../../custom_components/visionect/frontend/lib/guard.js";
import {
  CommandResult,
  IncorrectParameters,
  PROMPT,
  SerialConsole,
  TERMINATOR,
  UnknownCommand,
  classify,
} from "../../custom_components/visionect/frontend/lib/console.js";
import {
  DEFAULT_SERVER_PORT,
  PlanError,
  WIFI_SECURITY,
  checkServerHost,
  planCommission,
  planRepoint,
  planWifi,
  renderStep,
  suggestServer,
} from "../../custom_components/visionect/frontend/lib/plan.js";
import {
  NotASignError,
  identify,
  parseConnState,
  parseFirmware,
  parseRssi,
  parseServerTcp,
  parseUuid,
  parseWifiConf,
  readSnapshot,
} from "../../custom_components/visionect/frontend/lib/sign.js";
import {
  describeOutcome,
  executePlan,
} from "../../custom_components/visionect/frontend/lib/execute.js";
import { checkEnvironment } from "../../custom_components/visionect/frontend/lib/support.js";
import { FRAMES, FakeTransport, HEARTBEAT, echoUnknown } from "./fake.mjs";

const consoleOn = (transport, options = {}) =>
  new SerialConsole(transport, { commandTimeout: 2, idleTimeout: 0.1, ...options });

// ---------------------------------------------------------------------------
// The guard: what can never be emitted.
// ---------------------------------------------------------------------------

describe("guard", () => {
  // The list from the brief, verbatim, including the families. If any of these
  // can be sent, the panel can cost somebody a device.
  const mustRefuse = [
    "play_music",
    "sf_rdid",
    "sf_rdst",
    "fs_format",
    "cc3100_format",
    "cc3100_fw_upgrade",
    "feat_enable touch",
    "feat_disable touch",
    "feat_get",
    "cli_password_set hunter2 hunter2",
    "sf_unprot",
    "sf_wrst",
    "encryption_key_set deadbeef",
    "encryption_mode_set 1",
    "display_conf_set 2400 2400 2400 2400 0 0 0 0 1",
    "dcmb 1",
    "dcmc 0",
    "dcmd",
    "dcmh 1",
    "dcms",
    "dcmt 0",
    "dcmu",
    "dcmw",
    "app_sleep 60",
    "24aa256_test",
    "bsim 1 1",
    "bsimi 1 100",
    "bsimv 4200",
    "lms 1 40",
  ];

  for (const line of mustRefuse) {
    it(`refuses ${line}`, () => {
      assert.throws(() => assertAllowed(line), ForbiddenCommandError);
      assert.notEqual(checkCommand(line), null);
    });
  }

  it("refuses them case-insensitively, because the firmware is", () => {
    // UPTIME works on the device, so SF_RDID would too.
    assert.throws(() => assertAllowed("SF_RDID"), ForbiddenCommandError);
    assert.throws(() => assertAllowed("Play_Music"), ForbiddenCommandError);
  });

  it("refuses anything ending in _upgrade, including ones it has not heard of", () => {
    assert.throws(() => assertAllowed("something_new_upgrade"), ForbiddenCommandError);
  });

  it("refuses reboot and wifi_conf_set, which are safe but deliberately unused", () => {
    // reboot blanks the glass for a minute; cs 1 + cs 3 does the same job in
    // seconds. wifi_conf_set splits its arguments on whitespace.
    assert.equal(checkCommand("reboot")?.code, "not-allowed");
    assert.equal(checkCommand("wifi_conf_set a b wpa2 0")?.code, "not-allowed");
  });

  it("refuses a line break, because CR submits", () => {
    assert.equal(checkCommand("uptime\rsf_rdid")?.code, "line-break");
    assert.equal(checkCommand("uptime\nsf_rdid")?.code, "line-break");
  });

  it("refuses a semicolon, because the firmware has no statement separator", () => {
    assert.equal(checkCommand("uptime; sf_rdid")?.code, "separator");
  });

  it("allows exactly the commissioning set", () => {
    for (const name of ALLOWED_COMMANDS) {
      assert.equal(checkCommand(name), null, `${name} should be allowed`);
    }
  });

  it("names a reason for every entry in the deny list", () => {
    for (const [name, why] of Object.entries(DENIED_COMMANDS)) {
      assert.ok(why.length > 20, `${name} needs a real explanation, got ${why}`);
      assert.equal(checkCommand(name)?.code, "denied");
    }
  });

  it("does not let an allowed name smuggle a denied one in its arguments", () => {
    // Arguments are the firmware's problem, not the guard's -- but the command
    // word is all that decides, so this must still be allowed rather than
    // accidentally matched against the deny list.
    assert.equal(checkCommand("wifi_ssid_set play_music"), null);
    assert.equal(commandName("wifi_ssid_set play_music"), "wifi_ssid_set");
  });

  it("masks every occurrence of a secret", () => {
    assert.equal(redact("psk=s3cret and s3cret", ["s3cret"]), `psk=${MASK} and ${MASK}`);
    assert.equal(redact("nothing", [""]), "nothing");
    assert.equal(redact("nothing", []), "nothing");
  });
});

describe("the console refuses forbidden commands before it writes", () => {
  it("writes nothing at all", async () => {
    const transport = new FakeTransport();
    const console_ = consoleOn(transport);
    await assert.rejects(() => console_.command("sf_rdid"), ForbiddenCommandError);
    assert.deepEqual(transport.written, [], "nothing may reach the port");
    await console_.close();
  });
});

// ---------------------------------------------------------------------------
// Line discipline and framing.
// ---------------------------------------------------------------------------

describe("console framing", () => {
  it("terminates with a bare CR, never an LF", async () => {
    const transport = new FakeTransport();
    const console_ = consoleOn(transport);
    await console_.command("uptime");
    assert.deepEqual(transport.written, ["uptime\r"]);
    assert.equal(TERMINATOR, "\r");
    assert.ok(!transport.written[0].includes("\n"));
    await console_.close();
  });

  it("strips the echo, the prompt and the rv line", async () => {
    const transport = new FakeTransport();
    const console_ = consoleOn(transport);
    const result = await console_.command("flash_save");
    assert.deepEqual(result.lines, []);
    assert.equal(result.rv, 0);
    assert.ok(result.ok);
    assert.ok(result.echoed);
    assert.equal(result.terminatedBy, "prompt");
    await console_.close();
  });

  it("reads rv in both spellings", async () => {
    const transport = new FakeTransport({
      reply: (line) => (line === "flash_save" ? "flash_save\r\nrv: 0x0\r\n> " : null),
    });
    const console_ = consoleOn(transport);
    assert.equal((await console_.command("flash_save")).rv, 0);
    await console_.close();
  });

  it("parses a real multi-line frame into fields", async () => {
    const transport = new FakeTransport();
    const console_ = consoleOn(transport);
    const result = await console_.command("server_tcp_get");
    assert.deepEqual(result.fields(), {
      "Server IP/DNS": "visionect.internal.example.com",
      "Server port": "11113",
    });
    await console_.close();
  });

  it("survives the reply arriving one byte at a time", async () => {
    const transport = new FakeTransport({ chunkSize: 1 });
    const console_ = consoleOn(transport);
    const result = await console_.command("wifi_conf_get");
    assert.deepEqual(parseWifiConf(result.lines), {
      ssid: "ExampleAP",
      security: "wpa2",
      band: 0,
    });
    await console_.close();
  });

  // The whole reason this module exists.
  it("keeps a heartbeat burst that lands mid-reply out of the reply", async () => {
    const frame = FRAMES.fw_version_get;
    const at = frame.indexOf("BL Version");
    const transport = new FakeTransport({
      reply: (line) =>
        line === "fw_version_get" ? frame.slice(0, at) + HEARTBEAT + frame.slice(at) : null,
    });
    const console_ = consoleOn(transport);
    const result = await console_.command("fw_version_get");
    assert.equal(parseFirmware(result.lines).version, "7.4.4407");
    assert.equal(parseFirmware(result.lines).bootloader, "7.4.4407");
    for (const line of result.lines) {
      assert.ok(!line.startsWith("Heart-beat"), `log line leaked into the reply: ${line}`);
      assert.ok(!line.startsWith("sys evt"), `log line leaked into the reply: ${line}`);
    }
    assert.equal(result.logs.length, 8, "all eight heartbeat lines are logs");
    await console_.close();
  });

  it("classifies output already buffered before the command as log, not reply", async () => {
    const transport = new FakeTransport({ prelude: HEARTBEAT });
    const console_ = consoleOn(transport);
    // Give the pump a tick to take the prelude in, as a real port would.
    await new Promise((resolve) => setTimeout(resolve, 5));
    const result = await console_.command("uptime");
    assert.deepEqual(result.lines, ["Uptime in min: 5"]);
    assert.equal(result.logs.length, 8);
    await console_.close();
  });

  it("does not absorb a log line that shares a chunk with the prompt", async () => {
    // The Python reads whatever in_waiting reports and cannot push back; this
    // port can, so an async line arriving right behind the prompt stays
    // available to the next command instead of being eaten.
    const transport = new FakeTransport({
      reply: (line) => (line === "uptime" ? FRAMES.uptime + "Heart-beat event\r\n" : null),
    });
    const console_ = consoleOn(transport);
    const first = await console_.command("uptime");
    assert.deepEqual(first.lines, ["Uptime in min: 5"]);
    const second = await console_.command("uptime");
    assert.ok(
      second.logs.some((l) => l.text === "Heart-beat event"),
      "the trailing log line should be drained before the next command",
    );
    await console_.close();
  });

  it("notices when the echo is missing and says so instead of guessing", async () => {
    const transport = new FakeTransport({
      reply: () => "Uptime in min: 5\r\n> ",
    });
    const console_ = consoleOn(transport);
    const result = await console_.command("uptime");
    assert.equal(result.echoed, false);
    // The answer is kept rather than thrown away, which is what the caller
    // needs; `echoed` is how it knows to treat it with suspicion.
    assert.deepEqual(result.lines, ["Uptime in min: 5"]);
    await console_.close();
  });

  it("raises UnknownCommand on an unrecognised command", async () => {
    const transport = new FakeTransport({ reply: (line) => echoUnknown(line) });
    const console_ = consoleOn(transport);
    await assert.rejects(() => console_.command("gtin_get"), UnknownCommand);
    await console_.close();
  });

  it("raises IncorrectParameters for both spellings the firmware uses", async () => {
    for (const body of [
      'Incorrect command parameter(s).  Enter "help" to view a list of available commands.',
      "E: Invalid argument(s)",
    ]) {
      const transport = new FakeTransport({
        reply: (line) => `${line}\r\n${body}\r\n\r\n> `,
      });
      const console_ = consoleOn(transport);
      await assert.rejects(() => console_.command("cs 9"), IncorrectParameters);
      await console_.close();
    }
  });

  it("does not mistake E: Invalid argument(s) for an async log line", () => {
    // "^[DIWEF] \S" needs a space after the letter; "E:" has a colon. If this
    // ever changes, a rejected command would look like a silent success.
    assert.equal(classify("E: Invalid argument(s)"), false);
    assert.equal(classify("E: TCP connection Error: -111"), true);
    assert.equal(classify("W WD task timeout: cli_USB"), true);
    assert.equal(classify("Uptime in min: 5"), false);
  });

  it("times out rather than hanging when no prompt comes", async () => {
    const transport = new FakeTransport({ reply: (line) => `${line}\r\nhalf an answer\r\n` });
    const console_ = consoleOn(transport, { commandTimeout: 0.3 });
    await assert.rejects(() => console_.command("uptime"), /no prompt/);
    await console_.close();
  });

  it("sync() gets to a prompt from an unknown position", async () => {
    const transport = new FakeTransport({ prelude: "garbage with no prompt\r\n" });
    const console_ = consoleOn(transport);
    await console_.sync();
    assert.deepEqual(transport.written, ["\r"]);
    await console_.close();
  });

  it("CommandResult.one() refuses to guess when the shape changed", () => {
    const result = new CommandResult({ command: "x", raw: "", lines: ["a", "b"] });
    assert.throws(() => result.one(), /expected 1/);
    assert.throws(() => result.field("nope"), /not in reply/);
  });

  it("the prompt constant is two characters with no newline", () => {
    assert.equal(PROMPT, "> ");
  });
});

// ---------------------------------------------------------------------------
// The passphrase never leaves the one command that carries it.
// ---------------------------------------------------------------------------

describe("passphrase handling", () => {
  it("goes to the device verbatim but is masked everywhere else", async () => {
    const psk = "correct-horse";
    const transport = new FakeTransport({
      // The device echoes what it was sent, passphrase included. That echo is
      // the leak this has to catch.
      reply: (line) => `${line}\r\nrv: 0\r\n> `,
    });
    const seen = [];
    const console_ = consoleOn(transport, {
      onTraffic: ({ text }) => seen.push(text),
    });
    const result = await console_.command(`wifi_psk_set ${psk}`, { secrets: [psk] });

    assert.deepEqual(transport.written, [`wifi_psk_set ${psk}\r`], "the device gets the real one");
    assert.ok(!result.raw.includes(psk), "raw frame must be masked");
    assert.ok(!result.command.includes(psk), "command must be masked");
    assert.equal(result.command, `wifi_psk_set ${MASK}`);
    assert.ok(result.echoed, "masking both sides must not break echo anchoring");
    for (const text of seen) {
      assert.ok(!text.includes(psk), `secret leaked into the stream: ${text}`);
    }
    for (const entry of console_.transcript) {
      assert.ok(!JSON.stringify(entry).includes(psk), "secret leaked into the transcript");
    }
    await console_.close();
  });

  it("is absent from the plan object entirely", () => {
    const plan = planWifi({ ssid: "Kitchen", psk: "correct-horse", security: "wpa2" });
    assert.ok(!JSON.stringify(plan.steps).includes("correct-horse"));
    assert.deepEqual(plan.display, [
      `wifi_psk_set ${MASK}`,
      "wifi_security_set wpa2",
      "wifi_ssid_set Kitchen",
    ]);
    assert.deepEqual(plan.secrets, ["psk"]);
    // Only renderStep ever produces the real line.
    assert.equal(
      renderStep(plan.steps[0], { psk: "correct-horse" }),
      "wifi_psk_set correct-horse",
    );
    assert.throws(() => renderStep(plan.steps[0], {}), PlanError);
  });
});

// ---------------------------------------------------------------------------
// Plans.
// ---------------------------------------------------------------------------

describe("planWifi", () => {
  it("uses the three single-value setters, SSID last", () => {
    const plan = planWifi({ ssid: "Kitchen WiFi", psk: "correct-horse" });
    assert.deepEqual(
      plan.steps.map((s) => s.name),
      ["wifi_psk_set", "wifi_security_set", "wifi_ssid_set"],
    );
  });

  it("never emits wifi_conf_set, even for an SSID with no space in it", () => {
    const plan = planWifi({ ssid: "Kitchen", psk: "correct-horse" });
    assert.ok(!plan.steps.some((s) => s.name === "wifi_conf_set"));
  });

  it("sends a spaced SSID raw, with no quoting", () => {
    const plan = planWifi({ ssid: "My Home WiFi", psk: "correct-horse" });
    assert.equal(plan.steps.at(-1).command, "wifi_ssid_set My Home WiFi");
  });

  it("passes quotes and backslashes through, because they land in the SSID", () => {
    const plan = planWifi({ ssid: 'odd"name\\here', psk: "correct-horse" });
    assert.equal(plan.steps.at(-1).command, 'wifi_ssid_set odd"name\\here');
  });

  it("refuses a tab in the SSID: the line editor swallows it", () => {
    assert.throws(() => planWifi({ ssid: "Two\tWords", psk: "correct-horse" }), PlanError);
  });

  it("refuses whitespace in the passphrase: there is no read path to check it", () => {
    assert.throws(() => planWifi({ ssid: "Kitchen", psk: "two words" }), PlanError);
  });

  it("refuses a passphrase that is not a legal WPA2 length", () => {
    assert.throws(() => planWifi({ ssid: "Kitchen", psk: "short" }), PlanError);
    assert.throws(() => planWifi({ ssid: "Kitchen", psk: "x".repeat(64) }), PlanError);
    assert.ok(planWifi({ ssid: "Kitchen", psk: "x".repeat(63) }));
  });

  it("refuses an SSID over 32 octets", () => {
    assert.throws(() => planWifi({ ssid: "x".repeat(33), psk: "correct-horse" }), PlanError);
    // Measured in bytes, not characters.
    assert.throws(() => planWifi({ ssid: "é".repeat(17), psk: "correct-horse" }), PlanError);
  });

  it("refuses a security value that is not one of the three ASCII strings", () => {
    assert.throws(() => planWifi({ ssid: "Kitchen", psk: "x".repeat(10), security: "2" }), PlanError);
    assert.throws(() => planWifi({ ssid: "Kitchen", psk: "x".repeat(10), security: "wpa3" }), PlanError);
  });

  it("takes no passphrase for an open network", () => {
    const plan = planWifi({ ssid: "Guest", psk: "", security: WIFI_SECURITY.OPEN });
    assert.deepEqual(
      plan.steps.map((s) => s.name),
      ["wifi_security_set", "wifi_ssid_set"],
    );
    assert.deepEqual(plan.secrets, []);
    assert.throws(
      () => planWifi({ ssid: "Guest", psk: "something", security: WIFI_SECURITY.OPEN }),
      PlanError,
    );
  });

  it("says in its notes that the band is not written", () => {
    const plan = planWifi({ ssid: "Kitchen", psk: "correct-horse" });
    assert.ok(plan.notes.some((n) => /TCLV 68/.test(n)));
  });
});

describe("planRepoint", () => {
  it("is set, commit, then cs 1 and cs 3 in that order", () => {
    const plan = planRepoint({ host: "10.0.0.5" });
    assert.deepEqual(plan.display, [
      "server_tcp_set 10.0.0.5 11113",
      "flash_save",
      "cs 1",
      "cs 3",
    ]);
  });

  it("never emits cs 3 without cs 1 before it", () => {
    // cs 3 alone is a no-op while a session is open -- it answers
    // "Connectivity in state 3" and nothing reconnects.
    for (const plan of [
      planRepoint({ host: "10.0.0.5" }),
      planRepoint({ host: "10.0.0.5", reconnect: false }),
      planCommission({ host: "10.0.0.5", ssid: "Kitchen", psk: "correct-horse" }),
    ]) {
      const names = plan.display;
      const three = names.indexOf("cs 3");
      if (three !== -1) {
        assert.ok(names.indexOf("cs 1") !== -1 && names.indexOf("cs 1") < three);
      }
    }
  });

  it("always includes flash_save, because every setter before it is RAM-only", () => {
    assert.ok(planRepoint({ host: "10.0.0.5" }).display.includes("flash_save"));
    assert.ok(
      planRepoint({ host: "10.0.0.5", reconnect: false }).display.includes("flash_save"),
    );
  });

  it("puts flash_save after every setter and before every cs", () => {
    const names = planCommission({
      host: "10.0.0.5",
      ssid: "Kitchen",
      psk: "correct-horse",
    }).display.map((d) => d.split(" ")[0]);
    const commit = names.indexOf("flash_save");
    assert.ok(names.slice(0, commit).every((n) => n.endsWith("_set")));
    assert.ok(names.slice(commit + 1).every((n) => n === "cs"));
  });

  it("every step in every plan is a command the guard allows", () => {
    const plans = [
      planRepoint({ host: "10.0.0.5" }),
      planWifi({ ssid: "Kitchen", psk: "correct-horse" }),
      planWifi({ ssid: "Guest", psk: "", security: WIFI_SECURITY.OPEN }),
      planCommission({ host: "10.0.0.5", ssid: "Kitchen WiFi", psk: "correct-horse" }),
      planCommission({ host: "10.0.0.5" }),
    ];
    for (const plan of plans) {
      for (const step of plan.steps) {
        const line = renderStep(step, { psk: "correct-horse" });
        assert.equal(checkCommand(line), null, `${line} must be allowed`);
      }
    }
  });
});

describe("checkServerHost", () => {
  // The important one. Web Serial needs a secure context, and localhost is the
  // one plain-HTTP origin that qualifies -- so the panel is most likely to be
  // open on exactly the address that must never be written into a sign.
  for (const host of ["localhost", "127.0.0.1", "::1", "0.0.0.0", "LOCALHOST"]) {
    it(`refuses ${host}`, () => {
      assert.throws(() => checkServerHost(host), PlanError);
      assert.throws(() => planRepoint({ host }), PlanError);
    });
  }

  it("refuses a URL, because the sign does not speak HTTP", () => {
    assert.throws(() => checkServerHost("http://10.0.0.5:8123"), /bare host/);
    assert.throws(() => checkServerHost("10.0.0.5/path"), PlanError);
  });

  it("refuses an empty or whitespace-bearing address", () => {
    assert.throws(() => checkServerHost(""), PlanError);
    assert.throws(() => checkServerHost("   "), PlanError);
    assert.throws(() => checkServerHost("10.0.0.5 10.0.0.6"), PlanError);
    // Surrounding whitespace is trimmed rather than refused: it is almost
    // always a paste artefact, and an address cannot legally contain a space.
    assert.equal(checkServerHost("10.0.0.5 "), "10.0.0.5");
  });

  it("accepts an IP and a DNS name", () => {
    assert.equal(checkServerHost("10.0.0.5"), "10.0.0.5");
    assert.equal(checkServerHost(" ha.example.com "), "ha.example.com");
  });

  it("refuses a port that is not a port", () => {
    assert.throws(() => planRepoint({ host: "10.0.0.5", port: 0 }), PlanError);
    assert.throws(() => planRepoint({ host: "10.0.0.5", port: 70000 }), PlanError);
    assert.throws(() => planRepoint({ host: "10.0.0.5", port: "nope" }), PlanError);
    assert.equal(planRepoint({ host: "10.0.0.5", port: "11113" }).steps[0].command,
      "server_tcp_set 10.0.0.5 11113");
  });
});

describe("suggestServer", () => {
  it("prefers what the listener actually bound to", () => {
    const got = suggestServer({
      listener: { host: "192.0.2.50", port: 11113 },
      location: { hostname: "ha.example.com" },
    });
    assert.deepEqual(got, { host: "192.0.2.50", port: 11113, source: "listener", warning: null });
  });

  it("suggests nothing and explains when the page is on localhost", () => {
    const got = suggestServer({
      listener: { host: "127.0.0.1", port: 11113 },
      location: { hostname: "localhost" },
    });
    assert.equal(got.host, "");
    assert.match(got.warning, /no sign can dial/);
    assert.equal(got.port, 11113);
  });

  it("suggests nothing for a Nabu Casa relay, which cannot carry port 11113", () => {
    const got = suggestServer({
      listener: { host: "", port: null },
      location: { hostname: "abc123.ui.nabu.casa" },
    });
    assert.equal(got.host, "");
    assert.match(got.warning, /LAN address/);
    assert.equal(got.port, DEFAULT_SERVER_PORT);
  });

  it("suggests an mDNS name but warns the sign has no mDNS resolver", () => {
    const got = suggestServer({ location: { hostname: "homeassistant.local" } });
    assert.equal(got.host, "homeassistant.local");
    assert.match(got.warning, /mDNS/);
  });

  it("falls back to the page hostname with a caveat", () => {
    const got = suggestServer({ location: { hostname: "ha.example.com" } });
    assert.equal(got.host, "ha.example.com");
    assert.equal(got.source, "location");
    assert.ok(got.warning);
  });

  it("never suggests an address checkServerHost would refuse", () => {
    for (const hostname of ["localhost", "127.0.0.1", "::1", "0.0.0.0", ""]) {
      const got = suggestServer({ location: { hostname } });
      if (got.host !== "") {
        assert.doesNotThrow(() => checkServerHost(got.host));
      }
    }
  });
});

// ---------------------------------------------------------------------------
// Identify, and the reply parsers.
// ---------------------------------------------------------------------------

describe("identify", () => {
  it("accepts a real sign", async () => {
    const transport = new FakeTransport();
    const console_ = consoleOn(transport);
    const info = await identify(console_);
    assert.equal(info.uuid, "00112233-4455-6677-8899-aabb00000000");
    assert.equal(info.firmware.version, "7.4.4407");
    assert.equal(info.firmware.hardware, "PP32 v1.1");
    assert.equal(info.firmware.app, "Joan");
    await console_.close();
  });

  it("refuses something that answers but is not a sign", async () => {
    // A 3D printer, say: it has a prompt and a command line, and it would be
    // perfectly happy to be sent flash_save.
    const transport = new FakeTransport({
      reply: (line) => (line === "" ? "\r\n> " : echoUnknown(line)),
    });
    const console_ = consoleOn(transport);
    await assert.rejects(() => identify(console_), NotASignError);
    await console_.close();
  });

  it("refuses a uuid_get that answers with the wrong number of bytes", async () => {
    const transport = new FakeTransport({
      reply: (line) => (line === "uuid_get" ? "uuid_get\r\nUUID: 0x01 0x02\r\n> " : null),
    });
    const console_ = consoleOn(transport);
    await assert.rejects(() => identify(console_), NotASignError);
    await console_.close();
  });

  it("refuses a sign-shaped uuid with no firmware version", async () => {
    const transport = new FakeTransport({
      reply: (line) =>
        line === "fw_version_get" ? "fw_version_get\r\nsomething else\r\n> " : FRAMES[line],
    });
    const console_ = consoleOn(transport);
    await assert.rejects(() => identify(console_), NotASignError);
    await console_.close();
  });

  it("writes nothing but reads during the handshake", async () => {
    const transport = new FakeTransport();
    const console_ = consoleOn(transport);
    await identify(console_);
    assert.deepEqual(transport.written, ["\r", "uuid_get\r", "fw_version_get\r"]);
    await console_.close();
  });
});

describe("reply parsers", () => {
  it("renders the UUID with its real trailing zeros", () => {
    // A 12-byte factory identifier zero-padded to 16: the trailing zeros are
    // the device's, not truncation.
    const uuid = parseUuid([
      "UUID: 0x00 0x11 0x22 0x33 0x44 0x55 0x66 0x77 0x88 0x99 0xaa 0xbb 0x00 0x00 0x00 0x00",
    ]);
    assert.equal(uuid, "00112233-4455-6677-8899-aabb00000000");
  });

  it("reads server, wifi, connection and rssi off real frames", () => {
    const linesOf = (frame) =>
      frame
        .replaceAll("\r\n", "\n")
        .split("\n")
        .slice(1, -1)
        .map((l) => l.trim())
        .filter((l) => l !== "" && l !== ">");
    assert.deepEqual(parseServerTcp(linesOf(FRAMES.server_tcp_get)), {
      host: "visionect.internal.example.com",
      port: 11113,
    });
    assert.deepEqual(parseWifiConf(linesOf(FRAMES.wifi_conf_get)), {
      ssid: "ExampleAP",
      security: "wpa2",
      band: 0,
    });
    assert.deepEqual(parseConnState(linesOf(FRAMES.conn_state_get)), {
      state: "tcp open",
      connected: true,
    });
    assert.equal(parseRssi(linesOf(FRAMES.cc3100_rssi)), -31);
  });

  it("treats an unknown connectivity state as not connected", () => {
    // The firmware's state vocabulary is not documented anywhere.
    assert.equal(parseConnState(["Conn: something new"]).connected, false);
    assert.equal(parseConnState([]).connected, false);
  });

  it("gives null rather than throwing when a field has moved", () => {
    assert.equal(parseRssi(["nothing here"]), null);
    assert.deepEqual(parseServerTcp([]), { host: null, port: null });
  });
});

describe("readSnapshot", () => {
  it("fills in what it can and records what failed", async () => {
    const transport = new FakeTransport({
      reply: (line) => (line === "cc3100_rssi" ? echoUnknown(line) : FRAMES[line]),
    });
    const console_ = consoleOn(transport);
    const snapshot = await readSnapshot(console_);
    assert.equal(snapshot.server.port, 11113);
    assert.equal(snapshot.connection.connected, true);
    assert.equal(snapshot.rssi, null);
    assert.ok(snapshot.errors.cc3100_rssi, "the one failure is recorded, the rest still read");
    await console_.close();
  });
});

// ---------------------------------------------------------------------------
// Executing a plan.
// ---------------------------------------------------------------------------

describe("executePlan", () => {
  it("runs every step in order", async () => {
    const transport = new FakeTransport({ reply: (line) => `${line}\r\nrv: 0\r\n> ` });
    const console_ = consoleOn(transport);
    const plan = planCommission({ host: "10.0.0.5", ssid: "Kitchen", psk: "correct-horse" });
    const outcome = await executePlan(console_, plan, { secrets: { psk: "correct-horse" } });
    assert.equal(outcome.error, null);
    assert.equal(outcome.ran, plan.steps.length);
    assert.deepEqual(transport.written, [
      "wifi_psk_set correct-horse\r",
      "wifi_security_set wpa2\r",
      "wifi_ssid_set Kitchen\r",
      "server_tcp_set 10.0.0.5 11113\r",
      "flash_save\r",
      "cs 1\r",
      "cs 3\r",
    ]);
    assert.match(describeOutcome(plan, outcome), /written to flash/);
    await console_.close();
  });

  it("stops at the first failure and never reaches flash_save", async () => {
    const transport = new FakeTransport({
      reply: (line) =>
        line.startsWith("wifi_security_set")
          ? `${line}\r\nE: Invalid argument(s)\r\n\r\n> `
          : `${line}\r\nrv: 0\r\n> `,
    });
    const console_ = consoleOn(transport);
    const plan = planCommission({ host: "10.0.0.5", ssid: "Kitchen", psk: "correct-horse" });
    const outcome = await executePlan(console_, plan, { secrets: { psk: "correct-horse" } });
    assert.notEqual(outcome.error, null);
    assert.equal(outcome.failedAt.name, "wifi_security_set");
    assert.ok(!transport.written.some((w) => w.startsWith("flash_save")));
    assert.match(describeOutcome(plan, outcome), /nothing is committed/);
    await console_.close();
  });

  it("stops on a non-zero rv, which is not an exception", async () => {
    const transport = new FakeTransport({
      reply: (line) =>
        line === "flash_save" ? `${line}\r\nrv: -1\r\n> ` : `${line}\r\nrv: 0\r\n> `,
    });
    const console_ = consoleOn(transport);
    const plan = planRepoint({ host: "10.0.0.5" });
    const outcome = await executePlan(console_, plan, {});
    assert.match(outcome.error.message, /rv: -1/);
    assert.ok(!transport.written.some((w) => w.startsWith("cs ")));
    await console_.close();
  });

  it("says the change is permanent once flash_save has run", async () => {
    const transport = new FakeTransport({
      reply: (line) =>
        line === "cs 1" ? `${line}\r\nE: Invalid argument(s)\r\n\r\n> ` : `${line}\r\nrv: 0\r\n> `,
    });
    const console_ = consoleOn(transport);
    const plan = planRepoint({ host: "10.0.0.5" });
    const outcome = await executePlan(console_, plan, {});
    assert.match(describeOutcome(plan, outcome), /permanent/);
    await console_.close();
  });

  it("masks the passphrase in the stream while running the real plan", async () => {
    const seen = [];
    const transport = new FakeTransport({ reply: (line) => `${line}\r\nrv: 0\r\n> ` });
    const console_ = consoleOn(transport, { onTraffic: ({ text }) => seen.push(text) });
    const plan = planCommission({ host: "10.0.0.5", ssid: "Kitchen", psk: "correct-horse" });
    await executePlan(console_, plan, { secrets: { psk: "correct-horse" } });
    assert.ok(seen.length > 0);
    for (const text of seen) {
      assert.ok(!text.includes("correct-horse"), `leaked: ${text}`);
    }
    await console_.close();
  });
});

// ---------------------------------------------------------------------------
// The secure-context gate.
// ---------------------------------------------------------------------------

describe("checkEnvironment", () => {
  const base = {
    isSecureContext: true,
    hasSerial: true,
    protocol: "https:",
    hostname: "ha.example.com",
    embedded: false,
    brands: ["Chromium"],
    userAgent: "",
  };

  it("passes on a secure origin in a Chromium browser", () => {
    const got = checkEnvironment(base);
    assert.equal(got.ok, true);
    assert.equal(got.code, "ok");
  });

  it("explains an insecure origin and what to do about it", () => {
    const got = checkEnvironment({
      ...base,
      isSecureContext: false,
      hasSerial: false,
      protocol: "http:",
      hostname: "10.0.0.5",
    });
    assert.equal(got.ok, false);
    assert.equal(got.code, "insecure-origin");
    assert.match(got.detail, /10\.0\.0\.5/);
    assert.equal(got.remedies.length, 3);
    assert.ok(got.remedies.some((r) => /Nabu Casa/.test(r)));
    assert.ok(got.remedies.some((r) => /reverse proxy/.test(r)));
    assert.ok(got.remedies.some((r) => /localhost:8123/.test(r)));
  });

  it("says why homeassistant.local does not count as localhost", () => {
    // The carve-out is for loopback IPs and the literal name "localhost", not
    // for any name that resolves to one. This trips people up constantly.
    const got = checkEnvironment({
      ...base,
      isSecureContext: false,
      hasSerial: false,
      protocol: "http:",
      hostname: "homeassistant.local",
    });
    assert.equal(got.code, "insecure-origin");
    assert.match(got.detail, /does not count as loopback/);
  });

  it("blames the browser, not the origin, on Firefox", () => {
    const got = checkEnvironment({
      ...base,
      hasSerial: false,
      brands: [],
      userAgent: "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0",
    });
    assert.equal(got.code, "no-serial-api");
    assert.match(got.detail, /Chrome, Edge/);
  });

  it("distinguishes a Chromium browser whose Serial API has been withheld", () => {
    const got = checkEnvironment({ ...base, hasSerial: false });
    assert.equal(got.code, "serial-blocked");
    assert.match(got.detail, /Permissions-Policy/);
  });

  it("catches being inside a frame", () => {
    assert.equal(checkEnvironment({ ...base, embedded: true }).code, "embedded");
  });

  it("always returns prose fit to put on screen", () => {
    for (const env of [
      base,
      { ...base, isSecureContext: false },
      { ...base, hasSerial: false },
      { ...base, hasSerial: false, brands: [], userAgent: "Firefox" },
      { ...base, embedded: true },
    ]) {
      const got = checkEnvironment(env);
      assert.ok(got.title.length > 10);
      assert.ok(got.detail.length > 40);
      assert.ok(Array.isArray(got.remedies));
    }
  });
});
