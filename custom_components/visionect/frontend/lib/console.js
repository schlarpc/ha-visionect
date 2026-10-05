/**
 * The sign's ASCII serial console, in the browser.
 *
 * A port of `pyvisionect.io.usb.console`, which is where the measurements
 * behind every decision in here are written down. The short version, all of it
 * observed on firmware 7.4.4407 over an FTDI FT232 at 115200 8N1:
 *
 * - **The submit character is a bare CR.** LF does not submit: the device
 *   echoes the line and *holds* it, so the next command is appended to it and
 *   the pair is rejected as one unrecognised word. `\r\n` submits but leaves a
 *   stray LF after the prompt, which then pollutes the next read.
 * - **The device echoes the command line**, CRLF-terminated. That echo is the
 *   anchor this module synchronises on.
 * - **The prompt is `"> "`** with no trailing newline, after every reply. So a
 *   reply has a real terminator; there is no need to read until the port goes
 *   quiet.
 * - **Only some commands emit `rv: <int>`**, and not always last --
 *   `certs_config_get` prints it first. Two spellings, decimal and hex.
 *
 * The actual problem is that the firmware logs to this same UART unprompted. A
 * heartbeat fires every minute and dumps eight lines. Any of them can land in
 * the middle of a reply, so "every line between my command and the prompt is
 * my answer" corrupts intermittently -- the worst failure mode, because it
 * passes every test you write by hand.
 *
 * Four mechanisms separate the two streams, in descending order of how much
 * they are relied on:
 *
 *   1. **Drain before send.** Anything buffered when {@link SerialConsole#command}
 *      is called arrived unsolicited by definition, so it is taken out and
 *      classified as log *before* the command is written.
 *   2. **Echo anchor.** The reply frame starts at the echoed command line.
 *      Anything between the write and that echo is log.
 *   3. **Prompt terminator.** The frame ends at a `"> "` at the start of a
 *      line. Between those anchors the extent of the reply is exact.
 *   4. **Pattern classification.** Lines inside the frame matching
 *      {@link LOG_PATTERNS} are moved to the log stream. The only heuristic in
 *      the chain, and so the only one that can be wrong.
 *
 * One deliberate improvement over the Python: when the prompt is found with
 * bytes after it -- an async log line that arrived in the same chunk -- those
 * bytes are pushed back into the buffer rather than absorbed into the frame.
 * The Python reads whatever `in_waiting` reports and cannot do this; a string
 * buffer can.
 */

import { MASK, assertAllowed, redact } from "./guard.js";

/** 115200 8N1 -- from the vendor reference, confirmed on the wire. */
export const BAUD_RATE = 115200;

/** Bare CR. LF alone does not submit a line. */
export const TERMINATOR = "\r";

/** The device prompt. No trailing newline, which is why it is matched by hand. */
export const PROMPT = "> ";

const UNKNOWN_RE = /^Command '(?<cmd>.*)' not recognised\./;
const BAD_PARAMS_RE = /^(Incorrect command parameter\(s\)\.|E: Invalid argument\(s\))/;
const ASSERT_RE = /^assert: (?<where>\S+:\d+)$/;
const RV_RE = /^rv:\s*(?<value>-?(?:0[xX][0-9a-fA-F]+|\d+))$/;

/**
 * Regexes that identify an *unsolicited* line.
 *
 * Every entry came from a real capture off `/dev/ttyUSB0`; none is
 * speculative. A false positive here silently eats a reply line, so these are
 * anchored and specific rather than loose.
 */
export const LOG_PATTERNS = Object.freeze([
  // vlog severity prefix: "W WD task timeout: cli_USB". Note the space: it is
  // what keeps "E: Invalid argument(s)", which is a real reply, out of here.
  /^[DIWEF] \S/,
  // "sys evt vplatform_heartbeat.c:19, Heartbeat (7)"
  /^sys evt \S+\.c:\d+,/,
  /^Received event: /,
  /^Heart-beat event$/,
  /^sending pv2 status/,
  /^make packet type=\d+/,
  /^Frame send \d+ bytes$/,
  /^Frame recv \d+ bytes$/,
  /^Setting heartbeat after /,
  /^Got response \(id=\d+\)$/,
  /^Profiling:/,
  /^WD task timeout:/,
  // Asynchronous link faults. Narrow on purpose: "E: Invalid argument(s)" is a
  // reply to the command that was just sent and must not be filtered.
  /^E: TCP connection Error/,
  /^E: Max conn errs/,
  // Display narration during an image push.
  /^UPD_FULL/,
  /^border \(\d+ \d+\)$/,
  /^display update id: 0x[0-9a-f]+$/,
  /^Display updated!$/,
  // Connectivity state narration, which `cs` provokes a lot of.
  /^From state \d+ going to state \d+/,
]);

/** True if *line* looks like an asynchronous log line rather than a reply. */
export function classify(line, patterns = LOG_PATTERNS) {
  return patterns.some((p) => p.test(line));
}

/** One asynchronous line, with the moment it was read. */
export class LogLine {
  constructor(text, during = null) {
    this.text = text;
    this.monotonic = nowSeconds();
    this.during = during;
    Object.freeze(this);
  }
}

/** The structured outcome of one command. */
export class CommandResult {
  constructor({ command, raw, lines, rv = null, logs = [], echoed = true, terminatedBy = "prompt" }) {
    this.command = command;
    this.raw = raw;
    this.lines = Object.freeze([...lines]);
    this.rv = rv;
    this.logs = Object.freeze([...logs]);
    this.echoed = echoed;
    this.terminatedBy = terminatedBy;
    Object.freeze(this);
  }

  /** `lines` joined with newlines -- for printing, not for parsing. */
  get text() {
    return this.lines.join("\n");
  }

  /** True when no `rv:` was emitted, or the one emitted was zero. */
  get ok() {
    return this.rv === null || this.rv === 0;
  }

  /** The single reply line, for commands that emit exactly one. */
  one() {
    if (this.lines.length !== 1) {
      throw new Error(
        `${this.command} returned ${this.lines.length} reply lines, expected 1: ` +
          JSON.stringify(this.lines),
      );
    }
    return this.lines[0];
  }

  /** The value of the `"<label>: <value>"` line with this label. */
  field(label) {
    const found = this.fields()[label];
    if (found === undefined) {
      throw new Error(
        `${JSON.stringify(label)} not in reply to ${this.command}: ` + JSON.stringify(this.lines),
      );
    }
    return found;
  }

  /** Every `"<label>: <value>"` line as an object. Later duplicates win. */
  fields() {
    const out = {};
    for (const line of this.lines) {
      const at = line.indexOf(":");
      if (at > 0) {
        out[line.slice(0, at).trim()] = line.slice(at + 1).trim();
      }
    }
    return out;
  }
}

export class ConsoleError extends Error {}
export class ConsoleClosed extends ConsoleError {}
export class PromptNotFound extends ConsoleError {}
export class ConsoleTimeout extends ConsoleError {
  constructor(command, raw, seconds) {
    super(`no prompt for ${JSON.stringify(command)} within ${seconds}s`);
    this.name = "ConsoleTimeout";
    this.command = command;
    this.raw = raw;
  }
}
export class ConsoleAssertionFailed extends ConsoleError {
  constructor(command, where) {
    super(
      `${JSON.stringify(command)} tripped a firmware assertion at ${where}. The ` +
        "USB CLI task is dead and only a device reboot brings the console back",
    );
    this.name = "ConsoleAssertionFailed";
    this.command = command;
    this.where = where;
  }
}
export class UnknownCommand extends ConsoleError {
  constructor(command) {
    super(`the firmware has no command ${JSON.stringify(command)}`);
    this.name = "UnknownCommand";
    this.command = command;
  }
}
export class IncorrectParameters extends ConsoleError {
  constructor(command) {
    super(`the firmware rejected the arguments to ${JSON.stringify(command)}`);
    this.name = "IncorrectParameters";
    this.command = command;
  }
}

function nowSeconds() {
  return (typeof performance !== "undefined" ? performance.now() : Date.now()) / 1000;
}

/**
 * Find the end index of the first prompt that starts a line, at or after
 * *from*, or -1.
 *
 * "Starts a line" is the whole point: `"> "` can appear inside reply text, and
 * a reply that happened to contain it would otherwise be cut in half.
 */
function findPrompt(text, from = 0) {
  let at = text.indexOf(PROMPT, from);
  while (at !== -1) {
    if (at === 0 || text[at - 1] === "\n" || text[at - 1] === "\r") {
      return at + PROMPT.length;
    }
    at = text.indexOf(PROMPT, at + 1);
  }
  return -1;
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * A line-oriented client for the sign's serial console.
 *
 * @param transport an object with `read(): Promise<string|null>`,
 *   `write(text): Promise<void>` and `close(): Promise<void>`. `read` resolves
 *   with whatever has arrived, or `null` once the port is finished. Keeping
 *   the console at arm's length from Web Serial is what makes it testable
 *   without a browser at all.
 */
export class SerialConsole {
  constructor(transport, options = {}) {
    const {
      terminator = TERMINATOR,
      commandTimeout = 25,
      idleTimeout = 1.2,
      onLog = null,
      onTraffic = null,
      logHistory = 512,
      logPatterns = LOG_PATTERNS,
    } = options;
    this.transport = transport;
    this.terminator = terminator;
    this.commandTimeout = commandTimeout;
    this.idleTimeout = idleTimeout;
    this.onLog = onLog;
    this.onTraffic = onTraffic;
    this.logPatterns = logPatterns;
    this.logHistory = logHistory;
    this.logs = [];
    this.transcript = [];
    this._buffer = "";
    this._holdover = "";
    this._closed = false;
    this._waiters = [];
    this._pumpError = null;
    this._pump = this._runPump();
  }

  // ------------------------------------------------------------------ lifecycle

  async _runPump() {
    try {
      for (;;) {
        const chunk = await this.transport.read();
        if (chunk === null || chunk === undefined) {
          break;
        }
        if (chunk.length > 0) {
          this._buffer += chunk;
          this._wake();
        }
      }
    } catch (err) {
      this._pumpError = err;
    } finally {
      this._closed = true;
      this._wake();
    }
  }

  _wake() {
    const waiters = this._waiters;
    this._waiters = [];
    for (const resolve of waiters) {
      resolve();
    }
  }

  /** Resolve as soon as bytes arrive, the port closes, or *ms* elapses. */
  _waitForData(ms) {
    if (this._buffer.length > 0 || this._closed) {
      return Promise.resolve();
    }
    return new Promise((resolve) => {
      let done = false;
      const finish = () => {
        if (!done) {
          done = true;
          clearTimeout(timer);
          resolve();
        }
      };
      const timer = setTimeout(finish, ms);
      this._waiters.push(finish);
    });
  }

  get isOpen() {
    return !this._closed;
  }

  async close() {
    this._closed = true;
    this._wake();
    try {
      await this.transport.close();
    } finally {
      // Bounded: a transport whose read() never resolves must not wedge the
      // panel on the way out.
      await Promise.race([
        Promise.resolve(this._pump).catch(() => {}),
        sleep(2000),
      ]);
    }
  }

  // ---------------------------------------------------------------------- logs

  _emitLog(text, during) {
    const entry = new LogLine(text, during);
    this.logs.push(entry);
    if (this.logs.length > this.logHistory) {
      this.logs.splice(0, this.logs.length - this.logHistory);
    }
    if (this.onLog) {
      try {
        this.onLog(entry);
      } catch {
        /* a noisy callback must not break the command in flight */
      }
    }
    return entry;
  }

  _linesAsLogs(data, during) {
    const text = this._holdover + data;
    const parts = text.replaceAll("\r\n", "\n").replaceAll("\r", "\n").split("\n");
    this._holdover = parts.pop();
    // A bare prompt at the end of the holdover is not a log line.
    if (this._holdover.trim() === "" || this._holdover.trim() === ">") {
      this._holdover = "";
    }
    const out = [];
    for (const part of parts) {
      const stripped = part.trim();
      if (stripped !== "" && stripped !== ">") {
        out.push(this._emitLog(part.replace(/\s+$/, ""), during));
      }
    }
    return out;
  }

  /**
   * Read everything already buffered and record it as asynchronous.
   *
   * Correct by construction: bytes sitting in the buffer before a command is
   * written cannot be that command's reply.
   */
  drainLogs({ during = null } = {}) {
    const data = this._buffer;
    this._buffer = "";
    if (data === "") {
      return [];
    }
    return this._linesAsLogs(data, during);
  }

  // ------------------------------------------------------------------- reading

  async _readFrame(timeout, expectPrompt) {
    let frame = "";
    const start = nowSeconds();
    let last = start;
    while (nowSeconds() - start < timeout) {
      if (this._buffer.length > 0) {
        frame += this._buffer;
        this._buffer = "";
        last = nowSeconds();
        const end = findPrompt(frame);
        if (end !== -1) {
          // Push anything after the prompt back: it is the next frame's, or an
          // async log line that happened to share the chunk.
          this._buffer = frame.slice(end) + this._buffer;
          return [frame.slice(0, end), "prompt"];
        }
        if (!expectPrompt) {
          const tail = frame.replaceAll("\r\n", "\n").trimEnd().split("\n").pop().trim();
          if (ASSERT_RE.test(tail)) {
            return [frame, "assert"];
          }
        }
        continue;
      }
      if (this._closed) {
        return [frame, "closed"];
      }
      if (nowSeconds() - last >= this.idleTimeout) {
        return [frame, "idle"];
      }
      await this._waitForData(Math.min(50, this.idleTimeout * 1000));
    }
    return [frame, "timeout"];
  }

  // ------------------------------------------------------------------ commands

  /**
   * Get the stream to a known boundary: at a fresh prompt, buffer empty.
   *
   * Sends a bare CR and reads to the prompt, discarding whatever comes back as
   * log. Cheap, and worth doing at the start of a session and after any result
   * whose `echoed` is false.
   */
  async sync({ attempts = 3 } = {}) {
    for (let i = 0; i < attempts; i += 1) {
      this.drainLogs();
      await this.transport.write(this.terminator);
      const [raw, how] = await this._readFrame(Math.min(this.commandTimeout, 5), true);
      if (how === "prompt") {
        this._holdover = "";
        const tail = raw.slice(0, -PROMPT.length);
        if (tail) {
          this._linesAsLogs(tail, null);
        }
        return;
      }
      if (how === "closed") {
        throw new ConsoleClosed("the serial port closed while synchronising");
      }
    }
    throw new PromptNotFound(
      `no prompt on the serial port after ${attempts} attempts at ${BAUD_RATE} baud. ` +
        "Either this is not a Visionect sign, the baud rate is wrong, or the " +
        "firmware's usb_cli_task has died on an assertion -- in which case only a " +
        "device reboot brings the console back.",
    );
  }

  /**
   * Send one command line and return its parsed reply.
   *
   * @param line the command and its arguments, no terminator.
   * @param secrets strings to mask out of everything this method records: the
   *   transcript, the traffic callback and the returned `raw`. The line still
   *   goes to the device verbatim -- the device needs the real passphrase --
   *   but nothing downstream of here ever sees it, including the device's own
   *   echo of it.
   */
  async command(line, options = {}) {
    const {
      timeout = this.commandTimeout,
      expectPrompt = true,
      raiseOnError = true,
      secrets = [],
    } = options;

    // The chokepoint. Everything that reaches the port comes through here.
    assertAllowed(line);

    const safeLine = redact(line, secrets);
    const before = this.drainLogs({ during: null });

    this._traffic("out", safeLine);
    await this.transport.write(line + this.terminator);

    const [rawFrame, how] = await this._readFrame(timeout, expectPrompt);
    const raw = redact(rawFrame, secrets);
    const result = this._parseFrame(safeLine, raw, how, before);
    this.transcript.push(result);
    for (const replyLine of result.lines) {
      this._traffic("in", replyLine);
    }

    if (how === "timeout" || how === "assert" || how === "closed" || (how === "idle" && expectPrompt)) {
      const assertion = findAssertion(raw);
      if (assertion !== null) {
        throw new ConsoleAssertionFailed(safeLine, assertion);
      }
      if (how === "closed") {
        throw new ConsoleClosed(`the serial port closed during ${JSON.stringify(safeLine)}`);
      }
      throw new ConsoleTimeout(safeLine, raw, timeout);
    }

    if (raiseOnError) {
      raiseForError(safeLine, result);
    }
    return result;
  }

  _traffic(direction, text) {
    if (this.onTraffic) {
      try {
        this.onTraffic({ direction, text });
      } catch {
        /* a noisy callback must not break the command in flight */
      }
    }
  }

  /**
   * Split one frame into reply lines, a return code and log lines.
   *
   * Pure apart from recording log lines, and the reason the whole interleaving
   * story is testable: hand it a frame with a heartbeat burst spliced into the
   * middle and the burst comes out in `logs`.
   */
  _parseFrame(line, text, how, before) {
    const body = how === "prompt" ? text.slice(0, -PROMPT.length) : text;
    const parts = body.replaceAll("\r\n", "\n").replaceAll("\r", "\n").split("\n");

    // 2. the echo anchors the start of the reply.
    let echoed = false;
    const pre = [];
    while (parts.length > 0) {
      const head = parts.shift();
      if (head.trim().toLowerCase() === line.trim().toLowerCase()) {
        echoed = true;
        break;
      }
      pre.push(head);
    }
    let candidates = parts;
    if (!echoed) {
      // No echo: the stream was out of sync. Keep everything as reply
      // candidates rather than throwing the answer away.
      candidates = pre.concat(parts);
      pre.length = 0;
    }

    const during = [...before];
    for (const stray of pre) {
      if (stray.trim() !== "") {
        during.push(this._emitLog(stray.replace(/\s+$/, ""), line));
      }
    }

    // 3. classify, and pull the return code out wherever it sits.
    const lines = [];
    let rv = null;
    for (const part of candidates) {
      const stripped = part.trim();
      if (stripped === "" || stripped === ">") {
        continue;
      }
      const match = RV_RE.exec(stripped);
      if (match !== null) {
        rv = Number.parseInt(match.groups.value, /^-?0[xX]/.test(match.groups.value) ? 16 : 10);
        continue;
      }
      if (classify(stripped, this.logPatterns)) {
        during.push(this._emitLog(part.replace(/\s+$/, ""), line));
        continue;
      }
      lines.push(stripped);
    }

    return new CommandResult({
      command: line,
      raw: text,
      lines,
      rv,
      logs: during,
      echoed,
      terminatedBy: how,
    });
  }
}

function findAssertion(text) {
  for (const part of text.replaceAll("\r\n", "\n").split("\n")) {
    const match = ASSERT_RE.exec(part.trim());
    if (match !== null) {
      return match.groups.where;
    }
  }
  return null;
}

function raiseForError(line, result) {
  for (const reply of result.lines) {
    const unknown = UNKNOWN_RE.exec(reply);
    if (unknown !== null) {
      throw new UnknownCommand(unknown.groups.cmd);
    }
    if (BAD_PARAMS_RE.test(reply)) {
      throw new IncorrectParameters(line);
    }
  }
}

export { MASK };
