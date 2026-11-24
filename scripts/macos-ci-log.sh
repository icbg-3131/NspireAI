# Sourced by bash in GitHub Actions. Keep a raw log and expose diagnostics
# on the run page, where they can be read without the Actions logs API.
run_logged() {
  local phase="$1"
  shift
  local log="$RUNNER_TEMP/nspire-$phase.log" details
  if "$@" 2>&1 | tee "$log"; then
    return 0
  fi
  details="$(tail -25 "$log")"
  details="${details//%/%25}"
  details="${details//$'\n'/%0A}"
  details="${details//$'\r'/%0D}"
  printf '::error title=macOS %s failed::%s\n' "$phase" "$details"

  {
    printf '## macOS %s failure\n\n' "$phase"
    if [[ "$(wc -c < "$log")" -lt 1000000 ]]; then
      printf '<pre>\n'
      # Escape raw build output for the GitHub job summary.
      sed -e 's/\&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' "$log"
      printf '</pre>\n'
    else
      printf 'The full log is in the macos-%s-diagnostics artifact.\n' "$phase"
    fi
  } >> "$GITHUB_STEP_SUMMARY"
  return 1
}
