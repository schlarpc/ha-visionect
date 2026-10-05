/**
 * A scripted stand-in for a serial port.
 *
 * The frames in {@link FRAMES} are verbatim captures from a live sign on
 * firmware 7.4.4407, lifted from `pyvisionect.io.usb.fake`: echo first, prompt
 * last, exactly what a reader has to cope with. Using the real captures is the
 * point -- a hand-written reply would agree with whatever the parser happens to
 * do.
 */

/** Command -> the complete frame the device sent back, echo and prompt included. */
export const FRAMES = {
  uuid_get:
    "uuid_get\r\n\r\nUUID: 0x21 0x00 0x2b 0x00 0x05 0x51 0x37 0x30 0x32 0x34 0x39 0x36 0x00 0x00 0x00 0x00 \r\n> ",
  fw_version_get:
    'fw_version_get\r\nFW Version: 7.4.4407\r\nFW Build date: 10 9 2025\r\n' +
    "FW Crc=0x652be4ad, Hash=0x8281f596, Length=408876\r\nBL Version: 7.4.4407\r\n" +
    "BL Build date: 10 9 2025\r\nBuild Version: 7.4.4407\r\n" +
    "HW: PP32 v1.1, BOM: 0, APP: Joan\r\n" +
    'EPD: 31.2",2x2880x640,WF=31.2_C296,IC=31.2_p224rev0050\r\n> ',
  cli_version_get: "cli_version_get\r\nCLI version: 1.2\r\n> ",
  server_tcp_get:
    "server_tcp_get\r\nServer IP/DNS: visionect.internal.example.com\r\nServer port: 11113\r\n> ",
  wifi_conf_get: "wifi_conf_get\r\nSSID: ExampleAP\r\nSecurity: wpa2\r\nBand: 0\r\n> ",
  conn_state_get: "conn_state_get\r\nConn: tcp open\r\n> ",
  cc3100_rssi: "cc3100_rssi\r\nRSSI:-31 dBm\r\n> ",
  bq24023_mode_get:
    "bq24023_mode_get\r\nBQ: fast charge\r\nIbatt: 50 mA\r\nVbatt: 4214 mV\r\n> ",
  battery_conf_get:
    "battery_conf_get\r\nThreshold OFF: 3500 mV\r\nThreshold ON:  3400 mV\r\nThreshold CNT: 1\r\n> ",
  lmr: "lmr\r\nLM75: 24 degC\r\n> ",
  uptime: "uptime\r\nUptime in min: 5\r\n> ",
  flash_save: "flash_save\r\nrv: 0\r\n> ",
  "cs 1": "cs 1\r\nConnectivity in state 1\r\nrv: 0\r\n> ",
  "cs 3": "cs 3\r\nConnectivity in state 3\r\nrv: 0\r\n> ",
  wifi_security_set: "wifi_security_set wpa2\r\nrv: 0\r\n> ",
};

/** A heartbeat burst, verbatim. Eight lines, every minute, unprompted. */
export const HEARTBEAT =
  "sys evt vplatform_heartbeat.c:19, Heartbeat (7)\r\n" +
  "Received event: Heartbeat (7)\r\n" +
  "Heart-beat event\r\n" +
  "sending pv2 status on heartbeat\r\n" +
  "make packet type=3, id=4\r\n" +
  "Frame send 552 bytes\r\n" +
  "Setting heartbeat after 1 min\r\n" +
  "Got response (id=4)\r\n";

/**
 * A transport whose replies are decided by a handler.
 *
 * `read()` resolves when there is something queued, so a test never has to
 * sleep. `chunkSize` splits replies, because a real port delivers a 5 KB frame
 * in pieces and a parser that only works on whole frames is not a parser.
 */
export class FakeTransport {
  constructor({ reply, chunkSize = 0, prelude = "" } = {}) {
    this.reply = reply ?? ((line) => FRAMES[line] ?? echoUnknown(line));
    this.chunkSize = chunkSize;
    this.written = [];
    this._queue = [];
    this._waiters = [];
    this._closed = false;
    if (prelude) {
      this.inject(prelude);
    }
  }

  /** Push unsolicited output, as the firmware's logger does. */
  inject(text) {
    this._push(text);
  }

  _push(text) {
    if (this.chunkSize > 0) {
      for (let at = 0; at < text.length; at += this.chunkSize) {
        this._queue.push(text.slice(at, at + this.chunkSize));
      }
    } else {
      this._queue.push(text);
    }
    const waiters = this._waiters;
    this._waiters = [];
    for (const resolve of waiters) {
      resolve();
    }
  }

  async write(text) {
    this.written.push(text);
    if (!text.endsWith("\r")) {
      throw new Error(`write without a CR terminator: ${JSON.stringify(text)}`);
    }
    const line = text.slice(0, -1);
    if (line === "") {
      // A bare CR: the device answers with a fresh prompt.
      this._push("\r\n> ");
      return;
    }
    const reply = this.reply(line, this);
    if (reply !== null && reply !== undefined) {
      this._push(reply);
    }
  }

  async read() {
    for (;;) {
      if (this._queue.length > 0) {
        return this._queue.shift();
      }
      if (this._closed) {
        return null;
      }
      await new Promise((resolve) => this._waiters.push(resolve));
    }
  }

  async close() {
    this._closed = true;
    const waiters = this._waiters;
    this._waiters = [];
    for (const resolve of waiters) {
      resolve();
    }
  }
}

export function echoUnknown(line) {
  return (
    `${line}\r\nCommand '${line.split(" ")[0]}' not recognised.  Enter 'help' to ` +
    "view a list of available commands.\r\n\r\n> "
  );
}
