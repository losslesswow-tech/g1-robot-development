#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 <arm64|amd64> <ELF-or-archive> [...]" >&2
  exit 2
fi

case "$1" in
  arm64|aarch64)
    expected_dpkg="arm64"
    expected_machine="AArch64"
    ;;
  amd64|x86_64)
    expected_dpkg="amd64"
    expected_machine="Advanced Micro Devices X86-64"
    ;;
  *)
    echo "ERROR: unsupported expected architecture '$1'." >&2
    exit 2
    ;;
esac
shift

actual_dpkg="$(dpkg --print-architecture)"
if [[ "$actual_dpkg" != "$expected_dpkg" ]]; then
  echo "ERROR: image architecture is '$actual_dpkg', expected '$expected_dpkg'." >&2
  exit 1
fi

for artifact in "$@"; do
  if [[ ! -f "$artifact" ]]; then
    echo "ERROR: required architecture artifact is missing: $artifact" >&2
    exit 1
  fi

  mapfile -t machines < <(
    LC_ALL=C readelf -h "$artifact" 2>/dev/null \
      | awk -F: '/^[[:space:]]*Machine:/{sub(/^[[:space:]]+/, "", $2); print $2}' \
      | sort -u
  )
  if [[ ${#machines[@]} -eq 0 ]]; then
    echo "ERROR: '$artifact' contains no readable ELF header." >&2
    exit 1
  fi
  for machine in "${machines[@]}"; do
    if [[ "$machine" != "$expected_machine" ]]; then
      echo "ERROR: '$artifact' contains machine '$machine'; expected '$expected_machine'." >&2
      exit 1
    fi
  done
  echo "ARCH OK: $artifact -> $expected_machine"
done

