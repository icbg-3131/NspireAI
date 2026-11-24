# NspireAI Echo for Apple Silicon

This standalone package contains ARM64 CPython 3.12.16, PyUSB 1.3.1, and
libusb 1.0.30. It uses the existing NspireAI calculator program and starts
the echo backend. No manual runtime installation is needed.

## Open the package

1. If you want to check the download, put the ZIP and its `.sha256` file
   together in Downloads, then run this in Terminal.

   ```sh
   cd "$HOME/Downloads" &&
   shasum -a 256 -c NspireAI-Echo-macos-arm64-relocatable.zip.sha256
   ```
2. Double-click the ZIP. Keep the entire extracted `NspireAI-Echo` folder
   together. Close N-Link and other USB clients. Connect the calculator and
   leave it at **My Documents**.
3. Open **Start NspireAI Echo.command**. Terminal will check the bundled
   Python and USB library. When it shows **LINK READY CX II**, open the
   existing NspireAI document. Once it shows **USB idle**, press **Enter**
   once. After it connects, send `hi` and look for **Mac received: hi**.
4. Press **Esc** on the calculator before stopping the bridge with **Ctrl-C**.

Startup and echo logs are saved under `~/Library/Logs/NspireAI-Echo/`.
If the runtime or launcher fails, keep the startup log and the exact
message macOS displays.

The upstream binaries retain their ad-hoc signatures. The package is
unnotarized. If Gatekeeper blocks a verified download you trust, follow
[Apple's per-item instructions](https://support.apple.com/en-us/102445).
For a runtime-only check from Terminal, if the folder is in Downloads:

```sh
"$HOME/Downloads/NspireAI-Echo/NspireAI-Echo" --self-test
```

## Validation scope

The package was assembled on Linux from verified upstream archives.
Archive extraction, permissions, ARM64 Mach-O headers, dynamic load paths,
and embedded code-page hashes can be checked on Linux. Gatekeeper approval,
actual process startup on macOS, USB interface access, and calculator echo
require an Apple Silicon Mac.
