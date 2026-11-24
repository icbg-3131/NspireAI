"""CX II USB host for the standalone Ndless page's NSAI service.

NNSE and inner packet formats follow Vogtinator/libnspire (GPL-3.0-or-later),
src/cx2.cpp, src/packet.c and src/init.c. Disconnect routing follows its
src/service.c. See docs/native-usb.md for the scope of hardware validation.
"""
from __future__ import annotations

import argparse
import binascii
from collections import deque
from contextlib import contextmanager
from ctypes import POINTER, Structure, c_char_p, c_int, c_uint16
from dataclasses import dataclass
import errno
import os
import select
import signal
import struct
import sys
import time
import traceback

from . import protocol

HOST, CALCULATOR, BROADCAST = 0xFE, 0x01, 0xFF
HOST_ADDR, CALCULATOR_ADDR = 0x6400, 0x6401
ADDRESS, CLOCK, STREAM, LINK_CONTROL, ACK = 1, 2, 4, 8, 0x80
ADDRESS_PORT, AI_PORT, DISCONNECT_PORT = 0x4003, 0x5001, 0x40DE
NNSE = struct.Struct(">6B3H")
NAVNET = struct.Struct(">6H4B")
MAX_PACKET = NNSE.size + 1472
MAX_COMMAND = 5 + 2 * (protocol.HEADER.size + protocol.MAX_PAYLOAD)
IO_TIMEOUT_MS = 100
ACK_TIMEOUT = 1.0
KEEPALIVE_INTERVAL = 1.0
PEER_TIMEOUT = 8.0
KEEPALIVE_ID = 0xFFFFFFFF
INBOUND_OPCODES = {
    protocol.OP_PING, protocol.OP_PONG, protocol.OP_REQUEST,
    protocol.OP_NEW, protocol.OP_CANCEL, protocol.OP_BYE,
}


class LinkError(Exception):
    pass


def nnse_checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\0"
    total = sum(word for (word,) in struct.iter_unpack(">H", data))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return total


@dataclass(frozen=True)
class Packet:
    service: int
    source: int
    destination: int
    sequence: int
    payload: bytes = b""
    flags: int = 0
    misc: int = 0
    reserved: int = 0

    def encode(self) -> bytes:
        header = NNSE.pack(
            self.misc, self.service, self.source, self.destination, self.reserved,
            self.flags, NNSE.size + len(self.payload), self.sequence, 0,
        )
        checksum = nnse_checksum(header + self.payload) ^ 0xFFFF
        return header[:-2] + struct.pack(">H", checksum) + self.payload

    @classmethod
    def decode(cls, wire: bytes) -> Packet:
        if len(wire) < NNSE.size:
            raise LinkError("short NNSE header")
        misc, service, source, dest, reserved, flags, size, seq, _ = NNSE.unpack_from(wire)
        if size != len(wire) or nnse_checksum(wire) != 0xFFFF:
            raise LinkError("invalid NNSE packet length or checksum")
        return cls(service, source, dest, seq, wire[NNSE.size:], flags, misc, reserved)

    def acknowledgement(self) -> Packet:
        return Packet(
            self.service | ACK, self.destination, self.source, self.sequence,
            flags=self.flags & ~1, misc=self.misc, reserved=self.reserved,
        )


def navnet_checksum(payload: bytes) -> int:
    # The NavNet checksum in libnspire's src/packet.c (not standard CRC-CCITT).
    checksum = 0
    for byte in payload:
        low = checksum & 0xFF
        mixed = (((low & 0xF) << 4) ^ low) << 8
        shifted = mixed >> 5
        checksum = (byte << 8 | checksum >> 8) ^ mixed ^ shifted ^ (shifted >> 7)
    return checksum


@dataclass(frozen=True)
class NavPacket:
    source_port: int
    destination_port: int
    payload: bytes
    source_address: int = HOST_ADDR
    destination_address: int = CALCULATOR_ADDR
    ack: int = 0
    sequence: int = 0

    def encode(self) -> bytes:
        if len(self.payload) > 254:
            raise ValueError("this host sends only short NavNet packets")
        header = NAVNET.pack(
            0x54FD, self.source_address, self.source_port,
            self.destination_address, self.destination_port,
            navnet_checksum(self.payload), len(self.payload), self.ack, self.sequence, 0,
        )
        return header[:-1] + bytes([sum(header) & 0xFF]) + self.payload

    @classmethod
    def decode(cls, wire: bytes) -> NavPacket:
        if len(wire) < NAVNET.size:
            raise LinkError("short NavNet header")
        magic, src, src_port, dst, dst_port, crc, size, ack, seq, header_sum = NAVNET.unpack_from(wire)
        raw_payload = wire[NAVNET.size:]
        if magic != 0x54FD or (sum(wire[:NAVNET.size - 1]) & 0xFF) != header_sum:
            raise LinkError("invalid NavNet header")
        if navnet_checksum(raw_payload) != crc:
            raise LinkError("invalid NavNet payload checksum")
        payload = raw_payload
        if size == 255:
            if len(payload) < 4:
                raise LinkError("missing extended NavNet length")
            size = int.from_bytes(payload[:4], "big")
            payload = payload[4:]
        if size != len(payload):
            raise LinkError("invalid NavNet payload length")
        return cls(src_port, dst_port, payload, src, dst, ack, seq)


class PacketReader:
    def __init__(self):
        self.buffer = bytearray()
        self.partial_since = None

    def feed(self, data: bytes, now: float):
        if self.partial_since is not None and now - self.partial_since >= ACK_TIMEOUT:
            raise LinkError("incomplete NNSE packet timed out")
        self.buffer.extend(data)
        while len(self.buffer) >= NNSE.size:
            size = int.from_bytes(self.buffer[6:8], "big")
            if not NNSE.size <= size <= MAX_PACKET:
                raise LinkError(f"invalid NNSE packet length {size}")
            if len(self.buffer) < size:
                break
            wire = bytes(self.buffer[:size])
            del self.buffer[:size]
            self.partial_since = None
            yield Packet.decode(wire)
        if not self.buffer:
            self.partial_since = None
        elif self.partial_since is None:
            self.partial_since = now


class HostSession:
    def __init__(self, write, emit, now: float, startup_timeout: float,
                 wall_time=time.time):
        self.write = write
        self.emit = emit
        self.wall_time = wall_time
        self.startup_deadline = now + startup_timeout
        self.sequence = 0
        self.pending_ack: tuple[int, float] | None = None
        self.link_ready = False
        self.peer_port: int | None = None
        self.conversation = 0
        self.last_rx = now
        self.last_tx: float | None = None
        self.last_stream: Packet | None = None
        self.closed = False
        self.outgoing: deque[bytes] = deque()
        self.unhandled: set[tuple[int, int]] = set()

    def send(self, service: int, payload: bytes, now: float, *, acknowledged=False):
        seq = self.sequence
        self.sequence = (seq + 1) & 0xFFFF
        self.write(Packet(service, HOST, CALCULATOR, seq, payload, int(acknowledged)))
        if acknowledged:
            self.pending_ack = seq, now + ACK_TIMEOUT

    def receive(self, packet: Packet, now: float):
        if self.closed:
            return
        if packet.destination not in (HOST, BROADCAST):
            return
        # Initial address requests can come from the broadcast address.
        if packet.source != CALCULATOR and packet.service != ADDRESS:
            return
        if packet.service & ACK:
            if (packet.service == (STREAM | ACK) and packet.destination == HOST
                    and not packet.payload and self.pending_ack is not None
                    and packet.sequence == self.pending_ack[0]
                    and now < self.pending_ack[1]):
                self.pending_ack = None
                if not self.link_ready:
                    self.link_ready = True
                    self.emit("LINK READY CX II; waiting for calculator NSAI on 0x5001")
            return
        if packet.service == ADDRESS and len(packet.payload) == 65 and packet.payload[0] == 0:
            if self.link_ready:
                raise LinkError("calculator restarted its USB address handshake")
            if packet.flags & 1:
                self.write(packet.acknowledgement())
            # libnspire sends two address replies because some units miss the first.
            self.send(ADDRESS, b"\x80", now)
            self.send(ADDRESS, b"\x80", now)
            return
        if packet.flags & 1:
            self.write(packet.acknowledgement())
        if packet.service == CLOCK and packet.payload == b"\0":
            self.send(CLOCK, struct.pack(">BIQI", 0x80, int(self.wall_time()), 0, 0), now)
            if self.pending_ack is None and not self.link_ready:
                assignment = NavPacket(ADDRESS_PORT, ADDRESS_PORT, b"\x64\x01\xff\0")
                self.send(STREAM, assignment.encode(), now, acknowledged=True)
        elif packet.service == LINK_CONTROL and packet.payload == b"\x01":
            self.send(LINK_CONTROL, b"\x81\x03", now)
        elif packet.service == STREAM and packet.destination == HOST:
            # ACK retries again but do not deliver the application frame twice.
            previous = self.last_stream
            self.last_stream = packet
            if previous is None or (previous.sequence, previous.payload) != (
                packet.sequence, packet.payload
            ):
                self.receive_stream(NavPacket.decode(packet.payload), now)

    def receive_stream(self, packet: NavPacket, now: float):
        if packet.source_address != CALCULATOR_ADDR or packet.destination_address != HOST_ADDR:
            return
        if (packet.source_port == DISCONNECT_PORT and packet.destination_port == AI_PORT
                and self.peer_port is not None and packet.payload == struct.pack(">H", self.peer_port)):
            self.emit("DISCONNECTED calculator closed the service")
            self.closed = True
            self.outgoing.clear()
            return
        if packet.destination_port != AI_PORT:
            key = packet.source_port, packet.destination_port
            if len(self.unhandled) < 12 and key not in self.unhandled:
                self.unhandled.add(key)
                self.emit(f"NAVNET other service src=0x{key[0]:04x} dst=0x{key[1]:04x}")
            return
        if not self.link_ready or not 0x8000 <= packet.source_port < 0xFFFF or packet.ack:
            return
        try:
            opcode, request, conversation, _ = protocol.decode(packet.payload)
        except ValueError:
            self.emit("NAVNET 0x5001 received a non-NSAI payload; no application reply sent")
            return
        if self.peer_port is None:
            # The installed calculator writes PING, request 0, before reading.
            if opcode != protocol.OP_PING or request != 0:
                return
            self.peer_port = packet.source_port
            self.emit(f"CONNECTED calculator NSAI PING src=0x{self.peer_port:04x} service=0x5001")
        if packet.source_port != self.peer_port or opcode not in INBOUND_OPCODES:
            return
        if opcode == protocol.OP_BYE:
            self.emit("DISCONNECTED calculator sent BYE")
            self.closed = True
            self.outgoing.clear()
            return
        self.last_rx = now
        if opcode != protocol.OP_PONG or request != KEEPALIVE_ID:
            self.conversation = conversation
            self.emit("RX " + packet.payload.hex())

    def command(self, line: bytes):
        if line == b"QUIT" or line.startswith(b"IDLE "):
            return False
        if not line.startswith(b"SEND ") or len(line) > MAX_COMMAND:
            raise LinkError("invalid stdio helper command")
        if self.peer_port is None:
            raise LinkError("bridge tried to send before the calculator opened 0x5001")
        try:
            frame = binascii.unhexlify(line[5:])
            protocol.decode(frame)
        except ValueError as error:
            raise LinkError("invalid NSAI frame from bridge") from error
        if len(self.outgoing) >= 128:
            raise LinkError("bridge exceeded the USB send queue")
        self.outgoing.append(frame)
        return True

    def tick(self, now: float):
        if self.pending_ack and now >= self.pending_ack[1]:
            raise LinkError("calculator did not ACK the USB stream packet")
        if self.peer_port is None:
            if now >= self.startup_deadline:
                raise LinkError("timed out waiting for the calculator's 0x5001 PING")
            return
        if now - self.last_rx >= PEER_TIMEOUT:
            raise LinkError("calculator application stopped responding")
        if self.pending_ack:
            return
        if self.outgoing:
            frame = self.outgoing.popleft()
        elif self.last_tx is not None and now - self.last_tx >= KEEPALIVE_INTERVAL:
            frame = protocol.encode(protocol.OP_PING, KEEPALIVE_ID, self.conversation, b"PING")
        else:
            return
        self.send(STREAM, NavPacket(AI_PORT, self.peer_port, frame).encode(), now, acknowledged=True)
        self.last_tx = now

    def request_disconnect(self, now: float) -> bool:
        # Only a live, acknowledged channel is safe to address. A failed link
        # closes locally without writing to a possibly expired peer port.
        if (not self.closed and self.peer_port is not None and self.last_tx is not None
                and self.pending_ack is None
                and now - self.last_rx < PEER_TIMEOUT):
            packet = NavPacket(DISCONNECT_PORT, self.peer_port, struct.pack(">H", AI_PORT))
            self.send(STREAM, packet.encode(), now, acknowledged=True)
            return True
        return False


def read_commands(fd: int, buffer: bytes) -> tuple[list[bytes], bytes, bool]:
    if not select.select([fd], [], [], 0)[0]:
        return [], buffer, False
    chunk = os.read(fd, 4096)
    if not chunk:
        return [], b"", True
    *lines, remaining = (buffer + chunk).split(b"\n")
    if len(remaining) > MAX_COMMAND:
        raise LinkError("stdio command exceeded maximum length")
    return lines, remaining, False


def run_session(transport, fd: int, emit, stop, startup_timeout: float):
    reader = PacketReader()
    session = HostSession(transport.write, emit, time.monotonic(), startup_timeout)
    command_buffer = b""

    def receive():
        data = transport.read()
        now = time.monotonic()
        for packet in reader.feed(data, now):
            session.receive(packet, now)
            if session.closed:
                break

    while not session.closed:
        if stop():
            break
        lines, command_buffer, eof = read_commands(fd, command_buffer)
        if eof or not all(session.command(line) for line in lines):
            break
        session.tick(time.monotonic())
        receive()

    if session.request_disconnect(time.monotonic()):
        deadline = time.monotonic() + 0.3
        while session.pending_ack is not None and time.monotonic() < deadline:
            data = transport.read()
            now = time.monotonic()
            for packet in reader.feed(data, now):
                if packet.service == (STREAM | ACK):
                    session.receive(packet, now)


def load_usb_backend():
    # Delay the import so packet tests need neither libusb nor a device.
    import usb.backend.libusb1

    path = os.environ.get("NSPIRE_LIBUSB_PATH")
    if path:
        backend = usb.backend.libusb1.get_backend(find_library=lambda _name: path)
    else:
        backend = usb.backend.libusb1.get_backend()
    if backend is None:
        raise LinkError("libusb unavailable; check the USB helper installation")
    return backend


class LibUSBVersion(Structure):
    _fields_ = [
        (part, c_uint16) for part in ("major", "minor", "micro", "nano")
    ] + [("rc", c_char_p), ("description", c_char_p)]


def usb_error_detail(error: Exception, backend) -> str:
    code = getattr(error, "backend_error_code", None)
    if code is None or backend is None:
        return str(error)
    error_name = backend.lib.libusb_error_name
    error_name.argtypes = [c_int]
    error_name.restype = c_char_p
    native_name = error_name(code).decode("ascii", errors="replace")
    return f"{error}; libusb={native_name} ({code})"


@contextmanager
def usb_operation(label: str, backend=None):
    print(f"USB BEGIN {label}", flush=True)
    try:
        yield
    except Exception as error:
        raise LinkError(f"{label}: {usb_error_detail(error, backend)}") from error
    print(f"USB OK {label}", flush=True)


class USBTransport:
    def __init__(self):
        import usb.core
        import usb.util

        with usb_operation("initialize libusb"):
            backend = load_usb_backend()
            get_version = backend.lib.libusb_get_version
            get_version.argtypes = []
            get_version.restype = POINTER(LibUSBVersion)
            version = get_version().contents
            print(f"USB libusb {version.major}.{version.minor}.{version.micro}.{version.nano}; "
                  f"LIBUSB_DEBUG={os.environ.get('LIBUSB_DEBUG', 'unset')}; "
                  f"dylib={os.environ.get('NSPIRE_LIBUSB_PATH', 'system lookup')}", flush=True)
        with usb_operation("enumerate TI-Nspire CX II 0451:e022", backend):
            devices = list(usb.core.find(find_all=True, backend=backend, idVendor=0x0451, idProduct=0xE022))
        if len(devices) != 1:
            raise LinkError(f"connect exactly one TI-Nspire CX II on USB (found {len(devices)})")

        self.device = devices[0]
        self.device.default_timeout = IO_TIMEOUT_MS
        self.usb_core = usb.core
        self.usb_util = usb.util
        self.backend = backend
        try:
            with usb_operation("libusb_open", backend):
                # PyUSB 1.3.1 opens lazily. Open through its resource manager
                # now to separate a failure here from configuration lookup.
                self.device._ctx.managed_open()
            with usb_operation("read active USB configuration", backend):
                try:
                    configuration = self.device.get_active_configuration()
                except usb.core.USBError as error:
                    if error.strerror != "Configuration not set":
                        raise
                    configuration = None
                    print("USB no active configuration reported by PyUSB", flush=True)
            if configuration is None:
                with usb_operation("libusb_set_configuration(1)", backend):
                    self.device.set_configuration(1)
                with usb_operation("read configuration after selection", backend):
                    configuration = self.device.get_active_configuration()
            with usb_operation("inspect interface and bulk endpoint descriptors", backend):
                interface = configuration[(0, 0)]
                endpoints = {
                    ep.bEndpointAddress for ep in interface
                    if usb.util.endpoint_type(ep.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK
                }
                print(f"USB configuration={configuration.bConfigurationValue} "
                      f"interface={interface.bInterfaceNumber} "
                      f"alternate={interface.bAlternateSetting} "
                      f"bulk endpoints={sorted(hex(ep) for ep in endpoints)}", flush=True)
                if not {0x01, 0x81}.issubset(endpoints):
                    raise LinkError("calculator does not expose the expected CX II bulk endpoints")
            # Keep the same interface ownership policy. No reset or detach.
            with usb_operation(f"libusb_claim_interface({interface.bInterfaceNumber})", backend):
                usb.util.claim_interface(self.device, interface.bInterfaceNumber)
        except BaseException:
            try:
                self.close()
            except Exception:
                print("USB cleanup also failed", flush=True)
                traceback.print_exc(file=sys.stdout)
                sys.stdout.flush()
            raise

    def write(self, packet: Packet):
        wire = packet.encode()
        try:
            transferred = self.device.write(0x01, wire, timeout=IO_TIMEOUT_MS)
        except self.usb_core.USBError as error:
            raise LinkError(f"bulk USB write to 0x01: {usb_error_detail(error, self.backend)}") from error
        if transferred != len(wire):
            raise LinkError("short USB write")

    def read(self) -> bytes:
        try:
            return bytes(self.device.read(0x81, 4096, timeout=IO_TIMEOUT_MS))
        except self.usb_core.USBTimeoutError:
            return b""
        except self.usb_core.USBError as error:
            if error.errno == errno.ENODEV:
                raise LinkError("calculator was disconnected") from error
            raise LinkError(f"bulk USB read from 0x81: {usb_error_detail(error, self.backend)}") from error

    def close(self):
        with usb_operation("release claimed interface and close USB handle", self.backend):
            self.usb_util.dispose_resources(self.device)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--stdio", action="store_true")
    mode.add_argument("--probe-usb", action="store_true",
                      help="open and claim USB, then close without starting the echo session")
    parser.add_argument("--startup-timeout", type=int, default=45)
    args = parser.parse_args()
    if not 1 <= args.startup_timeout <= 120:
        parser.error("--startup-timeout must be between 1 and 120 seconds")

    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        transport = USBTransport()
        try:
            if args.probe_usb:
                print("USB PROBE PASS: device opened and interface claimed", flush=True)
            else:
                print("USB CLAIMED; waiting for the CX II link handshake", flush=True)
                run_session(transport, sys.stdin.fileno(), lambda line: print(line, flush=True),
                            lambda: stopping, args.startup_timeout)
        finally:
            transport.close()
    except Exception as error:
        print(f"ERROR {error}", flush=True)
        traceback.print_exc(file=sys.stdout)
        sys.stdout.flush()
        return 1
    print("STOPPED native USB helper released the calculator", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
