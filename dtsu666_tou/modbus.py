"""Modbus RTU transport, register tables, and decode helpers.

CRC-16, the raw serial reader for one fixed slave address, the IEEE 754
float32 conversion (big-endian word order), the register map, and the
block readers used by the scheduler.  Also hosts the test ``FakeModbus``.
"""

import logging
import struct
import time

from . import config


# ============================================================
# Modbus CRC16
# ============================================================

def crc16(data):
    """Modbus CRC-16 (polynomial 0xA001, initial 0xFFFF)."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


# ============================================================
# Modbus RTU transport
# ============================================================

class ModbusError(RuntimeError):
    """Base class for Modbus faults.

    Kept a ``RuntimeError`` subclass so existing callers that catch
    ``RuntimeError`` keep working.
    """


class ModbusTransportError(ModbusError):
    """Frame/CRC/timeout fault - the link is unhealthy and is re-opened."""


class ModbusProtocolError(ModbusError):
    """The device answered, but with a Modbus exception code."""


class ModbusRTU:
    """Raw serial Modbus RTU reader for one fixed slave address.

    Usage is *not* thread-safe - all reads happen from the scheduler
    thread.

    The port is opened on construction and is automatically re-opened
    after repeated transport failures (for example a USB-RS485 adapter
    being unplugged and re-plugged), using exponential backoff, so the
    logger recovers without a process restart.
    """

    def __init__(self, port, baudrate, timeout, *,
                 reopen_after_failures=None, reopen_delay=None,
                 reopen_max_delay=None, log=None):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.ser = None
        self.consecutive_failures = 0
        self.reopen_count = 0
        self.last_reopen_attempt = 0.0
        self._delay = None
        self.reopen_after_failures = (
            config.serial_reopen_after_failures()
            if reopen_after_failures is None else reopen_after_failures)
        self.reopen_delay = (
            config.serial_reopen_delay()
            if reopen_delay is None else reopen_delay)
        self.reopen_max_delay = (
            config.serial_reopen_max_delay()
            if reopen_max_delay is None else reopen_max_delay)
        self.log = log or logging.getLogger("dtsu666.modbus")
        self.open()

    # ---- lifecycle ----------------------------------------------------

    def open(self):
        """(Re)open the serial port.  Returns True on success."""
        import serial
        try:
            if self.ser is not None:
                try:
                    self.ser.close()
                except Exception:  # noqa: BLE001
                    pass
            self.ser = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=self.timeout,
                write_timeout=self.timeout,
            )
            self.consecutive_failures = 0
            self._delay = self.reopen_delay
            self.last_reopen_attempt = time.monotonic()
            self.log.info("Serial port %s open (%d baud)",
                          self.port, self.baudrate)
            return True
        except Exception as exc:  # noqa: BLE001
            self.ser = None
            self.log.error("Cannot open serial port %s: %s", self.port, exc)
            return False

    def close(self):
        try:
            if self.ser is not None:
                self.ser.close()
        except Exception:  # noqa: BLE001
            pass
        finally:
            self.ser = None

    def is_open(self):
        return bool(self.ser is not None
                    and getattr(self.ser, "is_open", False))

    def next_reopen_delay(self):
        return self._delay if self._delay is not None else self.reopen_delay

    def ensure_open(self, reason="not open"):
        """Re-open the port when it is closed, honouring the backoff delay."""
        if self.is_open():
            return True
        wait = self.next_reopen_delay()
        if time.monotonic() - self.last_reopen_attempt < wait:
            return False      # still backing off; try again on a later cycle
        if self.open():
            self.reopen_count += 1
            self.log.warning("Serial port %s recovered (after %s)",
                             self.port, reason)
            return True
        self._delay = min(self.reopen_max_delay,
                          max(self.reopen_delay, wait * 2))
        self.log.error("Serial re-open of %s failed; next attempt in %.0f s",
                       self.port, self._delay)
        return False

    def _note_failure(self, reason):
        """Count a transport failure and re-open once the threshold is hit."""
        self.consecutive_failures += 1
        if (self.reopen_after_failures
                and self.consecutive_failures >= self.reopen_after_failures):
            self.close()
            self.ensure_open(
                f"{self.consecutive_failures} consecutive failures: {reason}")

    def read_registers(self, slave, address, count):
        """Read *count* contiguous holding registers (function 0x03).

        Returns a list of 16-bit unsigned integers.
        Raises :class:`ModbusError` (a ``RuntimeError``) on any transport
        or protocol fault; transport faults trigger automatic recovery.
        """
        if not self.is_open() and not self.ensure_open():
            raise ModbusTransportError(
                f"Serial port {self.port} is not open")

        # Build request
        request = struct.pack(">BBHH", slave, 0x03, address, count)
        request += struct.pack("<H", crc16(request))

        try:
            self.ser.reset_input_buffer()
            self.ser.write(request)
            self.ser.flush()
            response = self.ser.read(5 + count * 2)
        except Exception as exc:  # noqa: BLE001
            self._note_failure(f"serial I/O error: {exc}")
            raise ModbusTransportError(
                f"Serial I/O failed on {self.port}: {exc}") from exc

        expected = 5 + count * 2
        if len(response) < 5:
            self._note_failure("short response")
            raise ModbusTransportError(
                f"Short response: expected {expected}, got {len(response)}"
            )
        if response[0] != slave:
            self._note_failure("wrong slave response")
            raise ModbusTransportError(
                f"Wrong slave response: 0x{response[0]:02X}")

        if response[1] & 0x80:
            # A Modbus exception reply is only five bytes long
            # (slave, function|0x80, exception code, CRC).  The device did
            # answer, so this is a protocol fault and the link is healthy -
            # the port must NOT be re-opened for it.
            crc = struct.unpack("<H", response[3:5])[0]
            if crc != crc16(response[:3]):
                self._note_failure("crc mismatch in exception frame")
                raise ModbusTransportError(
                    f"CRC mismatch on exception frame: {crc:04X}")
            raise ModbusProtocolError(
                f"Modbus exception: code 0x{response[2]:02X}")

        if len(response) != expected:
            self._note_failure("short response")
            raise ModbusTransportError(
                f"Short response: expected {expected}, got {len(response)}"
            )
        if response[1] != 0x03:
            raise ModbusProtocolError(
                f"Unexpected function code: 0x{response[1]:02X}")
        if response[2] != count * 2:
            self._note_failure("wrong byte count")
            raise ModbusTransportError(
                f"Wrong byte count: {response[2]} (expected {count * 2})"
            )

        received_crc = struct.unpack("<H", response[-2:])[0]
        if received_crc != crc16(response[:-2]):
            self._note_failure("crc mismatch")
            raise ModbusTransportError(
                f"CRC mismatch: received {received_crc:04X}"
            )

        self.consecutive_failures = 0
        registers = []
        for i in range(count):
            pos = 3 + i * 2
            registers.append(struct.unpack(">H", response[pos:pos + 2])[0])
        return registers


# ============================================================
# Float32 conversion
# ============================================================

def float32(registers, index):
    """Interpret two consecutive Modbus registers as IEEE 754 float32
    (big-endian word order)."""
    raw = struct.pack(">HH", registers[index], registers[index + 1])
    return struct.unpack(">f", raw)[0]


# ============================================================
# Register tables
# ============================================================

BASIC_REGISTERS = {
    0x0000: "REV. Software Version",
    0x0001: "UCode Programming code",
    0x0002: "CLr.E Energy reset",
    0x0003: "net Network selection",
    0x0006: "IrAt Current transformer rate",
    0x0007: "UrAt Voltage transformer rate",
    0x000A: "Disp Rotating display time",
    0x000B: "B.LCD Backlight time control",
    0x000C: "Endian Reserve",
    0x002C: "Protocol",
    0x002D: "bAud Communication baud rate",
    0x002E: "Addr Communication address",
}

ELECTRICAL = [
    (0x2000, "Uab", "Line voltage A-B",    "V",   0.1,   1),
    (0x2002, "Ubc", "Line voltage B-C",    "V",   0.1,   1),
    (0x2004, "Uca", "Line voltage C-A",    "V",   0.1,   1),
    (0x2006, "Ua",  "Phase A voltage",     "V",   0.1,   1),
    (0x2008, "Ub",  "Phase B voltage",     "V",   0.1,   1),
    (0x200A, "Uc",  "Phase C voltage",     "V",   0.1,   1),
    (0x200C, "Ia",  "Phase A current",     "A",   0.001, 3),
    (0x200E, "Ib",  "Phase B current",     "A",   0.001, 3),
    (0x2010, "Ic",  "Phase C current",     "A",   0.001, 3),
    (0x2012, "Pt",  "Total active power",  "W",   0.1,   1),
    (0x2014, "Pa",  "Phase A active power","W",   0.1,   1),
    (0x2016, "Pb",  "Phase B active power","W",   0.1,   1),
    (0x2018, "Pc",  "Phase C active power","W",   0.1,   1),
    (0x201A, "Qt",  "Total reactive power","var", 0.1,   1),
    (0x201C, "Qa",  "Phase A reactive power","var",0.1,   1),
    (0x201E, "Qb",  "Phase B reactive power","var",0.1,   1),
    (0x2020, "Qc",  "Phase C reactive power","var",0.1,   1),
]

POWER_FACTOR = [
    (0x202A, "PFt", "Total power factor",  "",    0.001, 3),
    (0x202C, "PFa", "Phase A power factor","",    0.001, 3),
    (0x202E, "PFb", "Phase B power factor","",    0.001, 3),
    (0x2030, "PFc", "Phase C power factor","",    0.001, 3),
]

FREQUENCY = [
    (0x2044, "Freq","Frequency",           "Hz",  0.01,  2),
]

ENERGY = [
    (0x101E, "ImpEp",   "Total forward active energy",  "kWh", 1.0, 3),
    (0x1020, "ImpEpA",  "Phase A forward active energy", "kWh", 1.0, 3),
    (0x1022, "ImpEpB",  "Phase B forward active energy", "kWh", 1.0, 3),
    (0x1024, "ImpEpC",  "Phase C forward active energy", "kWh", 1.0, 3),
    (0x1026, "NetImpEp","Net forward active energy",     "kWh", 1.0, 3),
    (0x1028, "ExpEp",   "Total reverse active energy",   "kWh", 1.0, 3),
    (0x102A, "ExpEpA",  "Phase A reverse active energy", "kWh", 1.0, 3),
    (0x102C, "ExpEpB",  "Phase B reverse active energy", "kWh", 1.0, 3),
    (0x102E, "ExpEpC",  "Phase C reverse active energy", "kWh", 1.0, 3),
    (0x1030, "NetExpEp","Net reverse active energy",     "kWh", 1.0, 3),
]


# ============================================================
# Decode helpers
# ============================================================

def decode_block(registers, start, parameters):
    """Apply multiplier/decimals to a contiguous register block."""
    result = {}
    for address, name, description, unit, multiplier, decimals in parameters:
        offset = address - start
        if offset < 0:
            raise RuntimeError(f"{name} address {address:04X} before block start")
        if offset + 1 >= len(registers):
            raise RuntimeError(f"{name} outside block (registers available: {len(registers)})")
        raw = float32(registers, offset)
        result[name] = {
            "address":     f"{address:04X}",
            "description": description,
            "raw":         raw,
            "value":       round(raw * multiplier, decimals),
            "unit":        unit,
            "decimals":    decimals,
        }
    return result


# ============================================================
# Read functions
# ============================================================

def read_basic(modbus, address):
    """Read individual basic/info registers (one request each)."""
    result = {}
    for reg, name in BASIC_REGISTERS.items():
        values = modbus.read_registers(address, reg, 1)
        result[f"{reg:04X}"] = {"name": name, "raw": values[0]}
    return result


def read_electrical(modbus, address):
    """Return flat dict {name: scaled_value} for the electrical block."""
    registers = modbus.read_registers(address, 0x2000, 34)
    decoded = decode_block(registers, 0x2000, ELECTRICAL)
    return {name: item["value"] for name, item in decoded.items()}


def read_power_factor(modbus, address):
    registers = modbus.read_registers(address, 0x202A, 8)
    decoded = decode_block(registers, 0x202A, POWER_FACTOR)
    return {name: item["value"] for name, item in decoded.items()}


def read_frequency(modbus, address):
    registers = modbus.read_registers(address, 0x2044, 2)
    decoded = decode_block(registers, 0x2044, FREQUENCY)
    return {name: item["value"] for name, item in decoded.items()}


def read_energy(modbus, address):
    """Return decoded energy block (all ten registers, scaled)."""
    registers = modbus.read_registers(address, 0x101E, 20)
    return decode_block(registers, 0x101E, ENERGY)


def zero_electrical():
    """Return the electrical dict with every value set to 0.0."""
    return {name: 0.0 for _, name, _, _, _, _ in ELECTRICAL}


def zero_power_factor():
    return {name: 0.0 for _, name, _, _, _, _ in POWER_FACTOR}


def zero_frequency():
    return {name: 0.0 for _, name, _, _, _, _ in FREQUENCY}


# ============================================================
# Test mode helpers
# ============================================================

class FakeModbus:
    """Synthetic Modbus that returns register values from a dict."""
    def __init__(self, regs=None):
        self.regs = regs or {}
        self.fail = False

    def read_registers(self, slave, address, count):
        if self.fail:
            raise RuntimeError("fake timeout")
        words = []
        for i in range(count):
            words.append(self.regs.get(address + i, 0))
        return words


def _pack32(value):
    raw = struct.pack(">f", value)
    return struct.unpack(">HH", raw)
