# Native USB echo on Apple Silicon

This mode targets the standalone NspireAI NGC executable that connects to
host service `0x5001`, including the package already launched on a CX II-T
running OS 6.4.0.74. It uses PyUSB and libusb with the existing Python echo
backend. No calculator rebuild is needed for this test.

Physical NavNet service acceptance on the CX II-T still needs verification.
The host exposes the working assumptions in its status messages. The
calculator's synchronous NavNet calls are unchanged and have previously
stalled on other CX II builds. The host's time limits cannot interrupt an
Ndless syscall on the calculator.

## Install

Use a native Apple Silicon Terminal (`uname -m` should show `arm64`).
Install Homebrew from [brew.sh](https://brew.sh/) if needed. Run the
following from the NspireAI checkout containing the native USB changes.

```sh
/opt/homebrew/bin/brew install python@3.12 libusb
PYTHON_BIN=/opt/homebrew/bin/python3.12 ./scripts/setup-bridge-python.sh
bridge/.venv/bin/python -m pip install -r bridge/requirements-native-usb.txt
bridge/.venv/bin/python -m unittest -v bridge.test_native_navnet
```

The tests cover USB packet framing, address assignment, ACKs, timeouts,
peer routing and disconnect. The end-to-end simulation sends a calculator
PING and `hi` as USB packet bytes and checks the PONG and exact
`Mac received: hi` response from the real Python bridge. The simulated
calculator is a test fixture.

## Try one physical round trip

1. Close N-Link and other calculator USB clients. Connect the awake
   calculator with a USB data cable and leave it at **My Documents**
   so the OS can complete USB setup.
2. Start the native bridge from the NspireAI checkout.

   ```sh
   set -o pipefail
   NSPIREAI_ENV=/dev/null NSPIREAI_WEB=0 NSPIRE_NAVNET_TRANSPORT=native \
     NSPIRE_LIBUSB_PATH="$(/opt/homebrew/bin/brew --prefix libusb)/lib/libusb-1.0.dylib" \
     ./scripts/run-navnet-bridge.sh echo 2>&1 | tee /tmp/nspire-native-echo.log
   ```
3. Wait for `LINK READY CX II`, then open NspireAI. When the page
   shows **USB idle**, press **Enter** once to arm NavNet.
   A real first calculator PING produces
   `CONNECTED calculator NSAI PING`. Wait for the calculator to show
   **connected**.
4. Type `hi`, then press **Enter**. Look for `RX opcode=3` and matching
   `TX opcode=4` in Terminal, and **Mac received: hi** on the calculator.
   The physical test passes when the reply appears on the same page.
5. Press **Esc** on the calculator before stopping the Mac bridge. If
   the helper is still running after the page closes, press **Ctrl-C**
   in Terminal.

`USB CLAIMED` means the Mac opened the bulk interface. If it times out
before `LINK READY`, stop the bridge, return to **My Documents** if the
page responds, unplug and reconnect USB, and try once more. If
`LINK READY` appears but `CONNECTED` does not, the device acknowledged the
USB address packet without completing the application handshake. Save
`/tmp/nspire-native-echo.log` and the calculator's on-screen status.
A `NAVNET other service` line reports port numbers without logging its
payload. If the page or USB stops responding, stop the host helper and
use the handheld's normal restart procedure if necessary.

## Protocol sources and limits

[Vogtinator/libnspire](https://github.com/Vogtinator/libnspire/tree/7d7d962ca49ff9017f6f146336c51ef7f2edf4e0)
provides the CX II NNSE envelope, address and clock exchange, ACKs, inner
NavNet framing, and disconnect packet. Its `service_connect` sets an
outbound destination port. Exposing it through Rust FFI would still need
inbound routing and would inherit the upstream blocking reads.
[libnspire-rs](https://github.com/lights0123/libnspire-rs) and
[N-Link](https://github.com/lights0123/n-link) confirm the CX II USB IDs,
single interface ownership, and the file-service API available to their
callers.

The native helper uses one libusb handle and waits for the installed page's
request-zero NSAI PING before binding the calculator's source port to
`0x5001`. It does not implement the TI desktop login protocol or respond to
unrelated inner service traffic. Bulk transfers use 100 ms timeouts. The
host stops after a missing stream ACK or an unresponsive peer. A clean
shutdown can send one disconnect to an active peer. Device removal and
protocol errors release the local interface without retrying an ambiguous
write. A device-originated close cancels queued replies.

[Hackspire USB Protocol](https://www.hackspire.org/USB_Protocol/) and
[Hackspire Syscalls](https://www.hackspire.org/Syscalls/) are additional
references. Direct access to both was blocked in the build environment.
The syscall names and CX II-T 6.4.0.74 mapping were cross-checked against
the project's pinned Ndless SDK and its
`OS_ncascx2t-6.4.0.74.idc` file.
