# NspireAI Echo for Apple Silicon

The standalone macOS ZIP contains the Python runtime, PyUSB, and libusb.
It starts the local echo backend and targets the existing NspireAI NGC
calculator executable. Keep your installed `nspire_ai.tns`.

## Download and start

1. Download `NspireAI-Echo-macos-arm64.zip` by signing in to GitHub and opening the successful
   **macOS ARM64 echo bundle** Actions run. Download the
   **NspireAI-Echo-macos-arm64** artifact under **Artifacts**. GitHub wraps
   the ZIP and checksum in an outer ZIP.
2. Extract the application ZIP. Keep the entire `NspireAI-Echo` folder
   together. No developer tools or package manager are needed on the Mac.
3. Close N-Link and other USB clients, connect the calculator, and leave
   it at **My Documents**. Open **Start NspireAI Echo.command** in the
   extracted folder. It opens Terminal and displays the connection status
   and the path to a diagnostic log under
   `~/Library/Logs/NspireAI-Echo/`.
4. Once Terminal shows `LINK READY CX II`, open NspireAI on the calculator
   and press **Enter** when it shows **USB idle**. After it connects, send
   `hi`. Look for **Mac received: hi** on the calculator. Press **Esc**
   on the calculator before stopping the host with **Ctrl-C**.

The executable has an ad-hoc signature. It is distributed without Apple
notarization to keep the build free. If macOS blocks a build you have
verified and trust, follow Apple's instructions for opening that specific
app in **System Settings → Privacy & Security**. Do not change Gatekeeper
settings globally. See the [Apple support guide](https://support.apple.com/en-us/102445).

For an archive extracted to Downloads, these use only macOS system tools.

```sh
cd "$HOME/Downloads"
shasum -a 256 -c NspireAI-Echo-macos-arm64.zip.sha256
"$HOME/Downloads/NspireAI-Echo/NspireAI-Echo"
```

## Build and publication

The workflow uses GitHub's standard public `macos-26` ARM64 runner and
skips private repositories. The build job tests the packet simulation
and creates a ZIP containing a PyInstaller console executable and pinned
libusb. A separate macOS job checks the extracted package's architecture,
signatures, permissions, and dynamic library paths. It starts both the
bundle self-test and normal launcher with a fresh home and system-only
PATH before uploading the 30-day download and SHA-256 checksum.

The packaged startup checks the expected no-device path on the runner.
A real calculator is required to test physical USB echo.
