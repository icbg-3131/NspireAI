from collections import deque
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import unittest

from bridge import native_navnet as native, protocol


PEER_PORT = 0x800F
CONVERSATION = 9
# Reference address assignment from libnspire's packet.c/init.c, wrapped in
# cx2.cpp's STREAM packet with sequence 3 and the ACK-request bit set.
ADDRESS_ASSIGNMENT = bytes.fromhex(
    "0004fe01000100200003e99054fd64004003640140031343040000fa6401ff00"
)


def calculator_stream(seq, payload, source=PEER_PORT, destination=native.AI_PORT):
    inner = native.NavPacket(
        source, destination, payload, native.CALCULATOR_ADDR, native.HOST_ADDR,
    )
    return native.Packet(native.STREAM, native.CALCULATOR, native.HOST, seq, inner.encode(), 1)


class NativeNavNetTests(unittest.TestCase):
    def setUp(self):
        self.tx = []
        self.stdout = []
        self.session = native.HostSession(self.tx.append, self.stdout.append, 0, 10, lambda: 100)

    def handshake(self):
        self.session.receive(native.Packet(native.ADDRESS, native.BROADCAST, native.BROADCAST, 7,
                                           bytes(65), 1), 0)
        self.assertEqual([p.service for p in self.tx], [0x81, 1, 1])
        self.assertEqual([p.payload for p in self.tx[1:]], [b"\x80", b"\x80"])
        self.session.receive(native.Packet(native.CLOCK, native.CALCULATOR, native.HOST, 8,
                                           b"\0"), 0)
        self.assertEqual(self.tx[-1].encode(), ADDRESS_ASSIGNMENT)
        self.assertEqual(self.tx[-2].payload, b"\x80\0\0\0d" + bytes(12))
        self.session.receive(self.tx[-1].acknowledgement(), 0.1)

    def connect(self):
        self.handshake()
        ping = protocol.encode(protocol.OP_PING, 0, CONVERSATION, b"")
        self.session.receive(calculator_stream(14, ping), 1)
        return ping

    def send_pong(self, now=1.1):
        pong = protocol.encode(protocol.OP_PONG, 0, CONVERSATION, b"PONG")
        self.session.command(b"SEND " + pong.hex().encode())
        self.session.tick(now)
        self.session.receive(self.tx[-1].acknowledgement(), now + 0.1)

    def test_packet_reader_handles_fragmented_and_coalesced_usb_reads(self):
        reader = native.PacketReader()
        wire = ADDRESS_ASSIGNMENT
        self.assertEqual(list(reader.feed(wire[:5], 0)), [])
        self.assertEqual(list(reader.feed(wire[5:] + wire, 0.1)),
                         [native.Packet.decode(wire), native.Packet.decode(wire)])
        self.assertEqual(list(reader.feed(wire[:5], 1)), [])
        with self.assertRaisesRegex(native.LinkError, "incomplete"):
            list(reader.feed(b"", 2))

    def test_packet_checksums_reject_corruption(self):
        wire = ADDRESS_ASSIGNMENT
        with self.assertRaisesRegex(native.LinkError, "checksum"):
            native.Packet.decode(wire[:-1] + b"x")
        with self.assertRaisesRegex(native.LinkError, "checksum"):
            native.NavPacket.decode(wire[12:-1] + b"x")

    def test_ack_must_match_peer_sequence_and_deadline(self):
        self.session.receive(native.Packet(native.CLOCK, native.CALCULATOR, native.HOST, 8,
                                           b"\0"), 0)
        ack = self.tx[-1].acknowledgement()
        self.session.receive(native.Packet(ack.service, native.BROADCAST, ack.destination,
                                           ack.sequence), 0.1)
        self.session.receive(native.Packet(ack.service, ack.source, ack.destination,
                                           ack.sequence + 1), 0.1)
        self.session.receive(ack, 1)
        self.assertFalse(self.session.link_ready)
        with self.assertRaisesRegex(native.LinkError, "did not ACK"):
            self.session.tick(1)
        self.assertEqual(len(self.tx), 2)  # No retransmit after ambiguity.

    def test_link_ready_is_not_an_application_connection(self):
        self.handshake()
        self.assertTrue(self.session.link_ready)
        data = protocol.encode(protocol.OP_REQUEST, 1, CONVERSATION, b"premature")
        self.session.receive(calculator_stream(11, data), 1)
        self.session.receive(calculator_stream(12, b"unknown control payload"), 2)
        self.assertIsNone(self.session.peer_port)
        with self.assertRaisesRegex(native.LinkError, "before the calculator"):
            self.session.command(b"SEND " + data.hex().encode())
        with self.assertRaisesRegex(native.LinkError, "0x5001 PING"):
            self.session.tick(10)

    def test_retries_acknowledge_without_duplicate_delivery_and_ignore_other_ports(self):
        self.connect()
        data = protocol.encode(protocol.OP_REQUEST, 1, CONVERSATION, b"hi")
        message = calculator_stream(15, data)
        self.session.receive(calculator_stream(15, data, source=0x800E), 2)
        self.session.receive(message, 2)
        self.session.receive(message, 2.1)
        self.assertEqual(self.stdout.count("RX " + data.hex()), 1)
        self.assertEqual(self.tx[-2:], [message.acknowledgement()] * 2)

    def test_idle_keepalive_and_peer_timeout(self):
        self.connect()
        previous_tx = len(self.tx)
        self.session.tick(2)
        self.assertEqual(len(self.tx), previous_tx)
        self.send_pong(2.1)
        previous_tx = len(self.tx)
        self.session.tick(3.2)
        self.assertEqual(len(self.tx), previous_tx + 1)
        keepalive = self.tx[-1]
        self.assertEqual(protocol.decode(native.NavPacket.decode(keepalive.payload).payload),
                         (protocol.OP_PING, native.KEEPALIVE_ID, CONVERSATION, b"PING"))
        self.session.receive(keepalive.acknowledgement(), 3.3)
        with self.assertRaisesRegex(native.LinkError, "stopped responding"):
            self.session.tick(9)
        self.session.request_disconnect(9)
        self.assertEqual(self.tx[-1], keepalive)

    def test_peer_disconnect_stops_queued_replies(self):
        self.connect()
        reply = protocol.encode(protocol.OP_RESPONSE, 1, CONVERSATION, b"hi")
        self.session.command(b"SEND " + reply.hex().encode())
        disconnect = calculator_stream(16, PEER_PORT.to_bytes(2, "big"),
                                       source=native.DISCONNECT_PORT)
        self.session.receive(disconnect, 2)
        self.assertTrue(self.session.closed)
        self.assertEqual(len(self.session.outgoing), 0)
        self.session.request_disconnect(2)
        self.assertEqual(self.tx[-1], disconnect.acknowledgement())

    def test_shutdown_uses_only_confirmed_peer(self):
        self.connect()
        self.assertFalse(self.session.request_disconnect(1))
        self.send_pong()
        self.session.request_disconnect(2)
        inner = native.NavPacket.decode(self.tx[-1].payload)
        self.assertEqual(inner, native.NavPacket(native.DISCONNECT_PORT, PEER_PORT, b"P\x01"))
        self.assertEqual(self.tx[-1].flags, 1)

    def test_simulated_usb_bytes_through_actual_echo_bridge(self):
        with tempfile.TemporaryDirectory(prefix="nspire-echo-", dir="/tmp") as home:
            helper = Path(home) / "helper"
            helper.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable)
                              + " -m bridge.test_native_navnet --simulate-usb\n")
            helper.chmod(0o700)
            result = subprocess.run(
                [sys.executable, "-m", "bridge.navnet_bridge", "--backend", "echo",
                 "--helper", str(helper)],
                env={**os.environ, "NSPIREAI_HOME": home, "NSPIREAI_WEB": "0"},
                capture_output=True, text=True, timeout=15,
            )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("LINK READY", result.stderr)
        self.assertIn("CONNECTED calculator NSAI PING", result.stderr)
        self.assertIn("RX opcode=3 request=1 conversation=9 bytes=2", result.stdout)
        self.assertIn("TX opcode=4 request=1 bytes=16", result.stdout)
        self.assertIn("SIMULATED calculator verified Mac received: hi", result.stderr)
        self.assertIn("DISCONNECTED calculator closed the service", result.stderr)


class SimulatedCalculator:
    """USB-side fixture that asserts host packet order, routing and echo bytes."""
    def __init__(self):
        address = native.Packet(native.ADDRESS, native.BROADCAST, native.BROADCAST, 7,
                                bytes(65), 1).encode()
        self.input = deque([address[:8], address[8:]])
        self.address_replies = 0
        self.finished = False

    def read(self):
        if self.input:
            return self.input.popleft()
        time.sleep(0.01)
        return b""

    def write(self, packet):
        if packet.service == native.ADDRESS:
            assert packet.payload == b"\x80"
            self.address_replies += 1
            if self.address_replies == 2:
                clock = native.Packet(native.CLOCK, native.CALCULATOR, native.HOST, 8, b"\0")
                self.input.append(clock.encode())
        elif packet.service == native.STREAM:
            inner = native.NavPacket.decode(packet.payload)
            assert (inner.source_address, inner.destination_address) == (
                native.HOST_ADDR, native.CALCULATOR_ADDR)
            ack = packet.acknowledgement().encode()
            if inner.destination_port == native.ADDRESS_PORT:
                assert packet.encode() == ADDRESS_ASSIGNMENT
                ping = protocol.encode(protocol.OP_PING, 0, CONVERSATION, b"")
                self.input.append(ack + calculator_stream(14, ping).encode())
                return
            assert (inner.source_port, inner.destination_port) == (native.AI_PORT, PEER_PORT)
            message = protocol.decode(inner.payload)
            if message == (protocol.OP_PONG, 0, CONVERSATION, b"PONG"):
                request = protocol.encode(protocol.OP_REQUEST, 1, CONVERSATION, b"hi")
                self.input.append(ack + calculator_stream(15, request).encode())
            else:
                assert message == (protocol.OP_RESPONSE, 1, CONVERSATION, b"Mac received: hi")
                self.finished = True
                print("SIMULATED calculator verified Mac received: hi", flush=True)
                disconnect = calculator_stream(16, PEER_PORT.to_bytes(2, "big"),
                                               source=native.DISCONNECT_PORT)
                self.input.append(ack + disconnect.encode())


if __name__ == "__main__":
    if sys.argv[1:] == ["--simulate-usb"]:
        calculator = SimulatedCalculator()
        native.run_session(calculator, sys.stdin.fileno(), lambda line: print(line, flush=True),
                           lambda: False, 5)
        sys.exit(0 if calculator.finished else 1)
    unittest.main()
