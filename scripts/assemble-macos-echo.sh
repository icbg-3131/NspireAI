#!/usr/bin/env bash
# Assemble a macOS ARM64 package on Linux from pinned, precompiled inputs.
set -Eeuo pipefail
trap 'printf "Assembly failed at line %s: %s\n" "$LINENO" "$BASH_COMMAND" >&2' ERR
cd "$(dirname "$0")/.."
ROOT="$PWD"
CACHE="${1:-$ROOT/.build/macos-ci/prebuilt-research}"
mkdir -p "$CACHE" "$ROOT/dist"
CACHE="$(cd "$CACHE" && pwd -P)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/nspire-relocatable.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

get_asset() {
  local url="$1" digest="$2" name="$3"
  if [[ ! -f "$CACHE/$name" ]]; then
    curl -fsSL --retry 2 --max-time 120 "$url" -o "$WORK/$name"
    printf '%s  %s\n' "$digest" "$WORK/$name" | sha256sum -c
    mv "$WORK/$name" "$CACHE/$name"
  fi
  printf '%s  %s\n' "$digest" "$CACHE/$name" | sha256sum -c
}

PYTHON_ASSET='cpython-3.12.16+20261123-aarch64-apple-darwin-install_only_stripped.tar.gz'
USB_ASSET='libusb-1.0.30-h602ebc4_1.conda'
PYUSB_ASSET='pyusb-1.3.1-py3-none-any.whl'
get_asset "https://github.com/astral-sh/python-build-standalone/releases/download/20261123/$PYTHON_ASSET" \
  0de87cfc2649849da10a54084f87fd01fa1e653478e32eee92198c213550e15c "$PYTHON_ASSET"
get_asset "https://conda.anaconda.org/conda-forge/osx-arm64/$USB_ASSET" \
  f70b1196dee785900d7e35432feade41b0e7884aad8fb27db680529f01da05e2 "$USB_ASSET"
get_asset "https://files.pythonhosted.org/packages/28/b8/27e6312e86408a44fe16bd28ee12dd98608b39f7e7e57884a24e8f29b573/$PYUSB_ASSET" \
  bf9b754557af4717fe80c2b07cc2b923a9151f5c08d17bdb5345dac09d6a0430 "$PYUSB_ASSET"
get_asset 'https://github.com/libusb/libusb/releases/download/v1.0.30/libusb-1.0.30.tar.bz2' \
  fea36f34f9156400209595e300840767ab1a385ede1dc7ee893015aea9c6dbaf libusb-1.0.30.tar.bz2

BUNDLE="$WORK/NspireAI-Echo"
mkdir -p "$BUNDLE/app/bridge" "$BUNDLE/lib" "$BUNDLE/third-party" "$WORK/conda"
tar -xf "$CACHE/$PYTHON_ASSET" -C "$BUNDLE"
mv "$BUNDLE/python" "$BUNDLE/runtime"
# Keep the stdlib and the exact interpreter bytes. Drop the packaged
# development headers and tools, pip, and any cached bytecode.
rm -rf "$BUNDLE/runtime/include" "$BUNDLE/runtime/share" "$BUNDLE/runtime/lib/pkgconfig" \
  "$BUNDLE/runtime/lib/python3.12/site-packages"
rm -f "$BUNDLE/runtime/lib/"*.a
# This is a Terminal launcher. Keep Tk and its plug-ins out of the package.
rm -rf "$BUNDLE/runtime/lib/"{itcl*,thread*,tcl*,tk*,libtcl*,libtk*} \
  "$BUNDLE/runtime/lib/python3.12/"{idlelib,tkinter} \
  "$BUNDLE/runtime/lib/python3.12/lib-dynload/"_tkinter*.so
find "$BUNDLE/runtime/bin" -mindepth 1 -maxdepth 1 ! -name python3.12 -delete
find "$BUNDLE/runtime/lib/python3.12" -type d -name __pycache__ -prune -exec rm -rf {} +
mkdir "$BUNDLE/runtime/lib/python3.12/site-packages"
unzip -q "$CACHE/$PYUSB_ASSET" -d "$BUNDLE/runtime/lib/python3.12/site-packages"

unzip -p "$CACHE/$USB_ASSET" 'pkg-*.tar.zst' | zstd -dq | tar -xf - -C "$WORK/conda"
unzip -p "$CACHE/$USB_ASSET" 'info-*.tar.zst' | zstd -dq | tar -xf - -C "$WORK/conda"
cp "$WORK/conda/lib/libusb-1.0.0.dylib" "$BUNDLE/lib/"
cp "$WORK/conda/info/licenses/COPYING" "$BUNDLE/third-party/libusb-LICENSE.txt"
cp -R "$WORK/conda/info/recipe" "$BUNDLE/third-party/libusb-recipe"
cp "$CACHE/libusb-1.0.30.tar.bz2" "$BUNDLE/third-party/libusb-1.0.30-source.tar.bz2"
cp "$BUNDLE/runtime/lib/python3.12/LICENSE.txt" "$BUNDLE/third-party/Python-LICENSE.txt"
cp LICENSE "$BUNDLE/LICENSE.txt"
cp bridge/{__init__,bridge,native_navnet,navnet_bridge,protocol}.py "$BUNDLE/app/bridge/"
cp scripts/frozen-echo.py "$BUNDLE/app/run_echo.py"
cp docs/macos-echo-relocatable.md "$BUNDLE/README.md"

cat > "$BUNDLE/NspireAI-Echo" <<'LAUNCH'
#!/bin/bash
set -eu
root="$(cd "$(dirname "$0")" && pwd -P)"
if [[ "$(/usr/bin/uname -s)" != Darwin || "$(/usr/sbin/sysctl -n hw.optional.arm64 2>/dev/null || true)" != 1 ]]; then
  printf '%s\n' 'NspireAI Echo requires an Apple Silicon Mac.' >&2
  exit 2
fi

python="$root/runtime/bin/python3.12"
library="$root/lib/libusb-1.0.0.dylib"
[[ -x "$python" && -r "$library" ]] || {
  printf '%s\n' 'The bundled runtime is missing. Extract the entire ZIP and keep the folder together.' >&2
  exit 1
}
exec /usr/bin/env -i HOME="$HOME" PATH=/usr/bin:/bin \
  PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 PYTHONIOENCODING=utf-8 \
  NSPIRE_LIBUSB_PATH="$library" \
  /usr/bin/arch -arm64 "$python" "$root/app/run_echo.py" "$@"
LAUNCH

cat > "$BUNDLE/Start NspireAI Echo.command" <<'COMMAND'
#!/bin/bash
set -uo pipefail
cd "$(dirname "$0")" || exit 1
umask 077
logs="$HOME/Library/Logs/NspireAI-Echo"
mkdir -p "$logs" || exit 1
startup_log="$(mktemp "$logs/startup.XXXXXX")" || exit 1
printf 'Startup log: %s\n' "$startup_log"
{
  printf 'NspireAI Echo bundled runtime check\n'
  /usr/bin/sw_vers
  ./NspireAI-Echo --self-test && ./NspireAI-Echo
} 2>&1 | /usr/bin/tee "$startup_log"
status=$?
printf '\nExit status: %s\nPress Return to close this window.\n' "$status"
IFS= read -r _ || true
exit "$status"
COMMAND
chmod 755 "$BUNDLE/NspireAI-Echo" "$BUNDLE/Start NspireAI Echo.command"

cat > "$BUNDLE/third-party/Source-and-build.txt" <<EOF
NspireAI GPL-3.0 source and Linux assembly recipe
https://github.com/icbg-3131/NspireAI/tree/native-echo-relocatable-20261124

CPython 3.12.16 source
https://github.com/python/cpython/tree/v3.12.16
Companion build instructions and prebuilt archive
https://github.com/astral-sh/python-build-standalone/tree/20261123
https://github.com/astral-sh/python-build-standalone/releases/download/20261123/$PYTHON_ASSET

libusb 1.0.30 source and Conda Forge build recipe are included.
https://conda.anaconda.org/conda-forge/osx-arm64/$USB_ASSET

PyUSB 1.3.1 source and license
https://github.com/pyusb/pyusb/tree/v1.3.1
The installed wheel also includes the PyUSB license.
EOF

ZIP=NspireAI-Echo-macos-arm64-relocatable.zip
(cd "$WORK" && zip -q -r -y "$ZIP" NspireAI-Echo)
unzip -tq "$WORK/$ZIP"
mkdir "$WORK/extracted"
unzip -q "$WORK/$ZIP" -d "$WORK/extracted"
EXTRACTED="$WORK/extracted/NspireAI-Echo"
for launcher in "$EXTRACTED/NspireAI-Echo" "$EXTRACTED/Start NspireAI Echo.command"; do
  test -x "$launcher"
  bash -n "$launcher"
done
test -x "$EXTRACTED/runtime/bin/python3.12"
cmp "$EXTRACTED/runtime/bin/python3.12" "$BUNDLE/runtime/bin/python3.12"
cmp "$EXTRACTED/lib/libusb-1.0.0.dylib" "$WORK/conda/lib/libusb-1.0.0.dylib"
python3 scripts/audit-macos-echo-archive.py "$EXTRACTED"
cp "$WORK/$ZIP" "$ROOT/dist/$ZIP"
(cd "$ROOT/dist" && sha256sum "$ZIP" | tee "$ZIP.sha256")
