#!/usr/bin/env bash
set -Eeuo pipefail
trap 'printf "Verification failed at line %s: %s\n" "$LINENO" "$BASH_COMMAND" >&2' ERR
stage() { printf '\n== %s ==\n' "$*"; }
die() { printf '%s\n' "$*" >&2; exit 1; }

stage "Identify the fresh verification runner"
uname -sm
sw_vers
[[ "$(uname -s):$(uname -m)" == Darwin:arm64 ]] || die "Expected a native ARM64 macOS runner"
PACKAGE="$(cd "${1:?usage: verify-macos-echo.sh PACKAGE_DIRECTORY}" && pwd -P)"
ZIP=NspireAI-Echo-macos-arm64.zip
WORK="$(mktemp -d "${TMPDIR:-/tmp}/nspireai-verify.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

stage "Check the delivered ZIP and extract to a new location"
(cd "$PACKAGE" && shasum -a 256 -c "$ZIP.sha256")
mkdir "$WORK/Extracted copy" "$WORK/Clean Home"
ditto -x -k "$PACKAGE/$ZIP" "$WORK/Extracted copy"
BUNDLE="$(cd "$WORK/Extracted copy/NspireAI-Echo" && pwd -P)"
EXECUTABLE="$BUNDLE/NspireAI-Echo"
LAUNCHER="$BUNDLE/Start NspireAI Echo.command"
[[ -x "$EXECUTABLE" && -x "$LAUNCHER" ]] || die "Executable permission lost after extraction"
LIBUSB="$BUNDLE/_internal/libusb-1.0.0.dylib"
[[ -f "$LIBUSB" ]] || die "Bundled libusb is missing"

inside_bundle() {
  local path="$1" resolved
  [[ -e "$path" ]] || return 1
  resolved="$(realpath "$path")" || return 1
  [[ "$resolved" == "$BUNDLE" || "$resolved" == "$BUNDLE/"* ]]
}

while IFS= read -r -d '' link; do
  inside_bundle "$link" || die "Broken or external symlink in the bundle: $link"
done < <(find "$BUNDLE" -type l -print0)

expand_loader_path() {
  local path="$1" binary="$2"
  case "$path" in
    @loader_path) printf '%s\n' "${binary%/*}" ;;
    @loader_path/*) printf '%s/%s\n' "${binary%/*}" "${path#@loader_path/}" ;;
    @executable_path) printf '%s\n' "$BUNDLE" ;;
    @executable_path/*) printf '%s/%s\n' "$BUNDLE" "${path#@executable_path/}" ;;
    *) return 1 ;;
  esac
}

read_rpaths() {
  otool -l "$1" | awk '
    $1 == "cmd" { kind = $2 }
    kind == "LC_RPATH" && $1 == "path" {
      sub(/^[[:space:]]*path /, "")
      sub(/ \(offset [0-9]+\)$/, "")
      print
    }'
}

add_rpaths() {
  local binary="$1" listing path resolved
  listing="$(read_rpaths "$binary")"
  while IFS= read -r path; do
    [[ -n "$path" ]] || continue
    resolved="$(expand_loader_path "$path" "$binary")" || die "Unsupported RPATH $path in $binary"
    inside_bundle "$resolved" || die "RPATH escapes or is missing from the bundle: $path in $binary"
    RPATHS+=("$resolved")
  done <<< "$listing"
}

verify_load_path() {
  local path="$1" binary="$2" root resolved
  case "$path" in
    /usr/lib/*|/System/Library/*) return 0 ;;
    @rpath/*)
      for root in ${RPATHS[@]+"${RPATHS[@]}"}; do
        if inside_bundle "$root/${path#@rpath/}"; then return 0; fi
      done
      ;;
    @loader_path/*|@executable_path/*)
      resolved="$(expand_loader_path "$path" "$binary")"
      if inside_bundle "$resolved"; then return 0; fi
      ;;
  esac
  die "External or unresolved dependency $path in $binary"
}

stage "Audit Mach-O architectures, signatures and dylib dependencies"
while IFS= read -r -d '' binary; do
  description="$(file -b "$binary")"
  [[ "$description" == *Mach-O* ]] || continue
  printf '%s: %s\n' "${binary#"$BUNDLE/"}" "$description"
  [[ "$(lipo -archs "$binary")" == arm64 ]] || die "Expected a thin ARM64 binary: $binary"
  codesign --verify --strict "$binary"

  # The executable's RPATH can be used by binaries it loads. Also inspect
  # each library's own RPATH and reject locations outside the extracted ZIP.
  RPATHS=()
  add_rpaths "$EXECUTABLE"
  if [[ "$binary" != "$EXECUTABLE" ]]; then add_rpaths "$binary"; fi
  dependencies="$(otool -L "$binary")"
  printf '%s\n' "$dependencies"
  install_id=
  if [[ "$description" == *'shared library'* ]]; then
    install_id="$(otool -D "$binary" | sed -n '2p')"
    if [[ -n "$install_id" ]]; then
      case "$install_id" in
        @rpath/*|@loader_path/*|@executable_path/*) ;;
        *) die "Non-relocatable Mach-O install ID $install_id in $binary" ;;
      esac
    fi
  fi
  while IFS= read -r path; do
    [[ -n "$path" && "$path" != "$install_id" ]] || continue
    verify_load_path "$path" "$binary"
  done < <(printf '%s\n' "$dependencies" | sed -n '2,$s/^[[:space:]]*\(.*\) (compatibility version.*$/\1/p')
done < <(find "$BUNDLE" -type f -print0)

stage "Self-test bundled Python, PyUSB and libusb with a system-only PATH"
env -i HOME="$WORK/Clean Home" PATH=/usr/bin:/bin "$EXECUTABLE" --self-test

stage "Start the shipped launcher and confirm status and the diagnostics file"
status=0
printf '\n' | env -i HOME="$WORK/Clean Home" PATH=/usr/bin:/bin "$LAUNCHER" 2>&1 | tee "$WORK/startup.log" || status=$?
[[ "$status" -eq 1 ]] || die "Expected the no-device exit on the hosted runner, got $status"
grep -F 'native USB echo · arm64' "$WORK/startup.log"
grep -F 'connect exactly one TI-Nspire CX II on USB (found 0)' "$WORK/startup.log"
grep -F 'connect exactly one TI-Nspire CX II on USB (found 0)' "$WORK/Clean Home/Library/Logs/NspireAI-Echo/"*.log

stage "Verified ZIP"
cat "$PACKAGE/$ZIP.sha256"
