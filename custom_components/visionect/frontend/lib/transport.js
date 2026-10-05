/**
 * Web Serial, behind the four-method transport {@link SerialConsole} expects.
 *
 * **Why Web Serial and not WebUSB.** The sign presents an FTDI FT232
 * (`0403:6001`) USB-serial bridge, which the operating system claims for
 * itself: `ftdi_sio` on Linux, the FTDI VCP driver on Windows, Apple's on
 * macOS. WebUSB cannot take an interface a kernel driver already owns, so it
 * is not an option here regardless of preference. Web Serial talks to the OS
 * serial port the driver published, which is exactly the right layer.
 *
 * The `0403:6001` filter is offered to the port picker as a *hint*: Chrome
 * shows filtered devices first and lets the user show everything anyway. It is
 * not a guarantee, which is why {@link identify} still has to prove what
 * answered.
 */

import { BAUD_RATE } from "./console.js";

/** FTDI FT232R, which is the bridge every sign this has been tested against uses. */
export const PORT_FILTERS = Object.freeze([
  Object.freeze({ usbVendorId: 0x0403, usbProductId: 0x6001 }),
]);

export class SerialUnavailable extends Error {}

/** Ask the user for a port. Must be called from a user gesture. */
export async function requestPort({ allFilters = true } = {}) {
  if (!globalThis.navigator?.serial) {
    throw new SerialUnavailable("navigator.serial is not available on this page");
  }
  return globalThis.navigator.serial.requestPort(
    allFilters ? { filters: [...PORT_FILTERS] } : {},
  );
}

/**
 * A `SerialPort` wrapped as a transport: `read`, `write`, `close`.
 *
 * Decoding happens here rather than in the console because the console is
 * string-based and happily unaware of Web Serial -- which is what lets it be
 * tested against a scripted fake instead of hardware.
 */
export class WebSerialTransport {
  constructor(port) {
    this.port = port;
    this._reader = null;
    this._writer = null;
    this._readableClosed = null;
    this._closing = false;
  }

  /** Open at 115200 8N1, which is what the console runs at. */
  async open({ baudRate = BAUD_RATE } = {}) {
    await this.port.open({
      baudRate,
      dataBits: 8,
      stopBits: 1,
      parity: "none",
      flowControl: "none",
      // Big enough that a 5 KB help dump or a heartbeat burst arriving while
      // nobody is reading does not overflow and lose the middle of a frame.
      bufferSize: 65536,
    });
    const decoder = new TextDecoderStream("ascii", { fatal: false });
    this._readableClosed = this.port.readable.pipeTo(decoder.writable).catch(() => {
      // A port yanked mid-read rejects here. The pump sees read() finish and
      // reports a closed console, which is the message worth showing.
    });
    this._reader = decoder.readable.getReader();
    this._writer = this.port.writable.getWriter();
    this._encoder = new TextEncoder();
  }

  async read() {
    if (this._reader === null) {
      return null;
    }
    try {
      const { value, done } = await this._reader.read();
      return done ? null : (value ?? "");
    } catch {
      return null;
    }
  }

  async write(text) {
    if (this._writer === null) {
      throw new SerialUnavailable("the port is not open");
    }
    await this._writer.write(this._encoder.encode(text));
  }

  async close() {
    if (this._closing) {
      return;
    }
    this._closing = true;
    try {
      await this._reader?.cancel();
    } catch {
      /* already gone */
    }
    try {
      this._reader?.releaseLock();
    } catch {
      /* already released */
    }
    try {
      await this._writer?.close();
    } catch {
      /* already gone */
    }
    try {
      this._writer?.releaseLock();
    } catch {
      /* already released */
    }
    await this._readableClosed?.catch(() => {});
    try {
      await this.port.close();
    } catch {
      /* already closed */
    }
    this._reader = null;
    this._writer = null;
  }
}
