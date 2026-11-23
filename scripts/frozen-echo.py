"""Console entry point for the standalone native USB echo package."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys
import traceback

from bridge.native_navnet import load_usb_backend, main as usb_main
from bridge.navnet_bridge import NavNetBridge


class LoggedStream:
    def __init__(self, terminal, log):
        self.terminal = terminal
        self.log = log

    def write(self, text):
        self.log.write(text)
        return self.terminal.write(text)

    def flush(self):
        self.log.flush()
        self.terminal.flush()


def bundle_library():
    if getattr(sys, "frozen", False):
        name = "libusb-1.0.0.dylib" if sys.platform == "darwin" else "libusb-1.0.so.0"
        library = Path(sys._MEIPASS) / name
        if not library.is_file():
            raise FileNotFoundError(f"bundled libusb is missing from {library.parent}")
        os.environ["NSPIRE_LIBUSB_PATH"] = str(library)


def launch_echo(command):
    log_dir = Path.home() / "Library" / "Logs" / "NspireAI-Echo"
    log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    log_path = log_dir / f"echo-{datetime.now():%Y%m%d-%H%M%S}-{os.getpid()}.log"

    def private_file(path, flags):
        return os.open(path, flags, 0o600)

    with open(
        log_path, "x", encoding="utf-8", buffering=1, opener=private_file
    ) as log:
        with redirect_stdout(LoggedStream(sys.stdout, log)), redirect_stderr(LoggedStream(sys.stderr, log)):
            print(f"NspireAI · native USB echo · {platform.machine()}", flush=True)
            print(f"Diagnostic log: {log_path}", flush=True)
            print("Wait for LINK READY CX II before opening NspireAI on the calculator.", flush=True)
            os.environ["NSPIRE_SERVICE_ID"] = "0x5001"
            os.environ["NSPIREAI_WEB"] = "0"
            try:
                bridge = NavNetBridge([*command, "--usb-helper"], "echo", "", None, 10)

                def stop(_signum, _frame):
                    bridge.close()

                signal.signal(signal.SIGINT, stop)
                signal.signal(signal.SIGTERM, stop)
                status = bridge.run()
            except Exception:
                traceback.print_exc()
                return 1
            print(f"Echo bridge stopped (exit {status}). Log: {log_path}", flush=True)
            return status


def main():
    bundle_library()
    args = sys.argv[1:]
    if args[:1] == ["--usb-helper"]:
        sys.argv = [sys.argv[0], *args[1:]]
        return usb_main()
    if args == ["--usb-runtime-check"]:
        load_usb_backend()
        print(f"Bundled USB helper runtime ready ({platform.machine()})")
        return 0

    command = [sys.executable]
    if not getattr(sys, "frozen", False):
        command.append(str(Path(__file__).resolve()))
    if args == ["--self-test"]:
        subprocess.run([*command, "--usb-runtime-check"], check=True, timeout=15)
        return 0
    if args:
        print("Usage: NspireAI-Echo [--self-test]", file=sys.stderr)
        return 2
    return launch_echo(command)


if __name__ == "__main__":
    raise SystemExit(main())
