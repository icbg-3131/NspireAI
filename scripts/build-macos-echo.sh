#!/usr/bin/env bash
set -Eeuo pipefail
trap 'printf "Build failed at line %s: %s\n" "$LINENO" "$BASH_COMMAND" >&2' ERR
stage() { printf '\n== %s ==\n' "$*"; }
cd "$(dirname "$0")/.."
if [[ "$(uname -s):$(uname -m)" != Darwin:arm64 ]]; then
  echo "Build this package on an ARM64 macOS runner." >&2
  exit 2
fi

stage "Runner identity and tools"
uname -sm
sw_vers
xcode-select -p
clang --version
autoreconf --version
ROOT="$PWD"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/nspireai-macos.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
PYTHON_BIN="${PYTHON_BIN:-python3.12}"
stage "Install pinned Python packaging dependencies"
"$PYTHON_BIN" -m venv "$WORK/venv"
PY="$WORK/venv/bin/python"
"$PY" -c 'import platform; assert platform.machine() == "arm64"'
"$PY" -m pip install -r bridge/requirements-macos-bundle.txt
"$PY" -m pip freeze
export MACOSX_DEPLOYMENT_TARGET=11.0

stage "Build pinned libusb for macOS ARM64"
git clone --quiet --depth 1 --branch v1.0.29 https://github.com/libusb/libusb.git "$WORK/libusb"
test "$(git -C "$WORK/libusb" rev-parse HEAD)" = 15a7ebb4d426c5ce196684347d2b7cafad862626
(
  cd "$WORK/libusb"
  ./autogen.sh --prefix="$WORK/usb-prefix" --disable-static --disable-examples-build --disable-tests-build
  make -j "$(sysctl -n hw.logicalcpu)"
  make install
)

LIBUSB="$WORK/usb-prefix/lib/libusb-1.0.0.dylib"
ls -l "$WORK/usb-prefix/lib"
file "$LIBUSB"
lipo -verify_arch arm64 "$LIBUSB"
stage "Freeze Python entry point and PyUSB with PyInstaller"
"$PY" -m PyInstaller --noconfirm --clean --console --onedir --target-arch arm64 \
  --name NspireAI-Echo --paths "$ROOT" --distpath "$WORK/package" \
  --workpath "$WORK/pyinstaller" --specpath "$WORK" \
  --exclude-module bridge.pagehost --exclude-module openai \
  --copy-metadata pyusb --copy-metadata pyinstaller \
  --add-binary "$LIBUSB:." scripts/frozen-echo.py

stage "Add console launcher, licenses, and source information"
BUNDLE="$WORK/package/NspireAI-Echo"
cat > "$BUNDLE/Start NspireAI Echo.command" <<'LAUNCH'
#!/bin/bash
cd "$(dirname "$0")" || exit 1
./NspireAI-Echo
status=$?
printf '\nPress Return to close this window.\n'
read -r _
exit "$status"
LAUNCH
chmod +x "$BUNDLE/Start NspireAI Echo.command"
cp LICENSE "$BUNDLE/LICENSE.txt"
cp "$WORK/libusb/COPYING" "$BUNDLE/libusb-LICENSE.txt"
PYTHON_LICENSE="$("$PY" -c 'import pathlib, sysconfig; print(pathlib.Path(sysconfig.get_path("stdlib")) / "LICENSE.txt")')"
if [[ -f "$PYTHON_LICENSE" ]]; then
  cp "$PYTHON_LICENSE" "$BUNDLE/Python-LICENSE.txt"
else
  PYTHON_VERSION="$("$PY" -c 'import platform; print(platform.python_version())')"
  curl -fsSL "https://raw.githubusercontent.com/python/cpython/v$PYTHON_VERSION/LICENSE" \
    -o "$BUNDLE/Python-LICENSE.txt"
fi
git -C "$WORK/libusb" archive --prefix=libusb-1.0.29/ --format=tar.gz HEAD > "$BUNDLE/libusb-1.0.29-source.tar.gz"
SOURCE_ROOT="${GITHUB_SERVER_URL:-https://github.com}/${GITHUB_REPOSITORY:-icbg-3131/NspireAI}"
printf 'NspireAI source and build instructions\n%s/tree/%s\n' \
  "$SOURCE_ROOT" "$(git rev-parse HEAD)" > "$BUNDLE/Source.txt"
cp docs/macos-echo-package.md "$BUNDLE/README.md"

stage "Create the candidate ZIP and SHA-256 file"
mkdir -p "$ROOT/dist"
ZIP="$ROOT/dist/NspireAI-Echo-macos-arm64.zip"
ditto -c -k --keepParent "$BUNDLE" "$ZIP"
(cd "$ROOT/dist" && shasum -a 256 "$(basename "$ZIP")" | tee "$(basename "$ZIP").sha256")
ls -lh "$ZIP"
