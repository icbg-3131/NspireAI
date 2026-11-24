# macOS USB diagnostics for the relocatable echo package

Apply the diagnostics ZIP to the existing `NspireAI-Echo` folder.
It contains the Python helper, the shell launcher and a local WebUSB probe.
The Python executable, its local signature, the dylib, and the calculator
program remain byte-for-byte identical.

## Native probe

With other USB clients closed and the calculator awake at **My Documents**,
run:

```sh
bundle="$HOME/Downloads/NspireAI-Echo"
LIBUSB_DEBUG=4 "$bundle/NspireAI-Echo" --usb-helper --probe-usb 2>&1 |
  tee "$HOME/Downloads/nspire-native-usb-probe.log"
```

This separates enumeration, `libusb_open`, configuration lookup or
selection, descriptor inspection, and `libusb_claim_interface`. It releases
the handle afterwards and sends no NavNet or file packets. The original
launcher discarded `LIBUSB_DEBUG` via `env -i`. The updated launcher passes
that variable explicitly, with level 4 by default. The native library
writes debug output to stderr. The Terminal log captures it along with
the Python traceback and libusb's numeric and symbolic error.

If the probe succeeds, open **Start NspireAI Echo.command** with the
calculator still at **My Documents**. Wait for **LINK READY CX II** before
opening and arming NspireAI on the calculator.

## What macOS can be telling us

Enumeration in System Information establishes device visibility. An app
still needs a usable device handle and access to its USB interface. The
bundled Darwin libusb 1.0.30 maps IOKit `kIOReturnExclusiveAccess` to
`LIBUSB_ERROR_ACCESS (-3)`. Its device-open code tolerates a failed
exclusive device open if it can create an event source; later configuration
or interface-open work can still fail. The bridge uses ordinary open and
claim operations and does not call the libusb device-capture path that
requires elevated privileges or an entitlement. The failing stage and
native diagnostic are needed before attributing the error to an owner,
the OS, or a particular libusb build. [libusb Darwin implementation](https://github.com/libusb/libusb/blob/v1.0.30/libusb/os/darwin_usb.c)

On Apple Silicon laptops macOS may prompt to allow a newly attached
accessory. Connect while unlocked and approve that accessory if
prompted. Leave the global security policy in place. A local ad-hoc
signature lets the executable run but does not release an interface
another process holds. [Apple’s USB accessory guidance](https://support.apple.com/en-us/102282)

## Compare Chromium WebUSB

After the native probe exits, serve the local probe with bundled Python:

```sh
bundle="$HOME/Downloads/NspireAI-Echo"
"$bundle/runtime/bin/python3.12" -I -m http.server 8765 --bind 127.0.0.1 \
  --directory "$bundle/usb-diagnostics"
```

Open `http://127.0.0.1:8765` in the same Chrome or Chromium browser used
for N-Link. With the N-Link tab closed, click **Choose calculator and test
USB**. Save the stage results. Use **Ctrl-C** in Terminal to stop the server.

N-Link's web build uses the browser chooser, then WebUSB open, configuration,
interface claim and bulk transfers. Chromium's macOS USB service reaches
libusb as well, so WebUSB still depends on macOS allowing that process to
claim the interface. A browser pass after a native failure establishes
that the browser path works under those conditions, for those operations.
It does not establish that the browser can already exchange NspireAI
NavNet messages. A browser transport would have to reuse the existing
CX II packet framing and connect it to echo, with the browser owning the
USB handle. [N-Link device handling](https://github.com/lights0123/n-link/blob/0472908ef4961e7eec92cc0b97376420a71e5bc7/web/components/devices.ts) · [Chromium macOS USB service](https://github.com/chromium/chromium/blob/main/services/device/usb/usb_service.cc) · [Chromium interface claim](https://github.com/chromium/chromium/blob/main/services/device/usb/usb_device_handle_impl.cc)
