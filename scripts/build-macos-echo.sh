#!/usr/bin/env bash
set -Eeuo pipefail
trap 'printf "Build failed at line %s: %s\n" "$LINENO" "$BASH_COMMAND" >&2' ERR
cd "$(dirname "$0")/.."
if [[ "$(uname -s):$(uname -m)" != Darwin:arm64 ]]; then
  echo "Build this package on an ARM64 macOS runner." >&2
  exit 2
fi

ROOT="$PWD"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/nspireai-macos.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
PYTHON_BIN="${PYTHON_BIN:-python3.12}"
"$PYTHON_BIN" -m venv "$WORK/venv"
PY="$WORK/venv/bin/python"
"$PY" -c 'import platform; assert platform.machine() == "arm64"'
"$PY" -m pip install -r bridge/requirements-macos-bundle.txt
export MACOSX_DEPLOYMENT_TARGET=11.0

git clone --quiet --depth 1 --branch v1.0.29 https://github.com/libusb/libusb.git "$WORK/libusb"
test "$(git -C "$WORK/libusb" rev-parse HEAD)" = 15a7ebb4d426c5ce196684347d2b7cafad862626
(
  cd "$WORK/libusb"
  ./autogen.sh --prefix="$WORK/usb-prefix" --disable-static --disable-examples-build --disable-tests-build
  make -j "$(sysctl -n hw.logicalcpu)"
  make install
)

LIBUSB="$WORK/usb-prefix/lib/libusb-1.0.0.dylib"
"$PY" -m PyInstaller --noconfirm --clean --console --onedir --target-arch arm64 \
  --name NspireAI-Echo --paths "$ROOT" --distpath "$WORK/package" \
  --workpath "$WORK/pyinstaller" --specpath "$WORK" \
  --exclude-module bridge.pagehost --exclude-module openai \
  --copy-metadata pyusb --copy-metadata pyinstaller \
  --add-binary "$LIBUSB:." scripts/frozen-echo.py

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
"$PY" -c 'import pathlib, sysconfig; print(pathlib.Path(sysconfig.get_path("stdlib")) / "LICENSE.txt")' |
  while IFS= read -r path; do cp "$path" "$BUNDLE/Python-LICENSE.txt"; done
git -C "$WORK/libusb" archive --prefix=libusb-1.0.29/ --format=tar.gz HEAD > "$BUNDLE/libusb-1.0.29-source.tar.gz"
SOURCE_ROOT="${GITHUB_SERVER_URL:-https://github.com}/${GITHUB_REPOSITORY:-icbg-3131/NspireAI}"
printf 'NspireAI source and build instructions\n%s/tree/%s\n' \
  "$SOURCE_ROOT" "$(git rev-parse HEAD)" > "$BUNDLE/Source.txt"
cp docs/macos-echo-package.md "$BUNDLE/README.md"

# Verify the actual shipping binaries and startup path. In particular, the
# child helper must load libusb from the unpacked bundle, even with no Homebrew.
for binary in "$BUNDLE/NspireAI-Echo" "$BUNDLE/_internal/libusb-1.0.0.dylib"; do
  lipo -verify_arch arm64 "$binary"
  codesign --verify --strict "$binary"
done
otool -L "$BUNDLE/_internal/libusb-1.0.0.dylib"
if otool -L "$BUNDLE/_internal/libusb-1.0.0.dylib" | tail -n +3 | grep -Eq '/opt/homebrew|/usr/local|nspireai-macos\.'; then
  echo "Bundled libusb has an external build-machine dependency" >&2
  exit 1
fi

mkdir -p "$ROOT/dist"
ZIP="$ROOT/dist/NspireAI-Echo-macos-arm64.zip"
ditto -c -k --keepParent "$BUNDLE" "$ZIP"
mkdir "$WORK/verify"
ditto -x -k "$ZIP" "$WORK/verify"
env -i HOME="$HOME" PATH=/usr/bin:/bin \
  "$WORK/verify/NspireAI-Echo/NspireAI-Echo" --self-test
(cd "$ROOT/dist" && shasum -a 256 "$(basename "$ZIP")" > "$(basename "$ZIP").sha256")
