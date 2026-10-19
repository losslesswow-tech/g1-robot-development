#!/usr/bin/env bash
set -euo pipefail

readonly SDK_URL="https://github.com/unitreerobotics/unitree_sdk2.git"
readonly HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly DEST="${2:-$HERE/../unitree_sdk2}"
readonly REF="${1:-}"

if [[ ! "$REF" =~ ^[0-9a-fA-F]{40}$ ]]; then
  echo "Usage: $0 <full-40-character-unitree-sdk2-commit> [destination]" >&2
  echo "ERROR: an immutable full commit SHA is required; branches and moving tags are rejected." >&2
  exit 2
fi
if [[ -e "$DEST" ]]; then
  echo "ERROR: destination already exists; refusing to overwrite it: $DEST" >&2
  exit 1
fi

mkdir -p "$DEST"
git -C "$DEST" init
git -C "$DEST" remote add origin "$SDK_URL"
git -C "$DEST" fetch --depth 1 origin "$REF"
git -C "$DEST" checkout --detach FETCH_HEAD

actual="$(git -C "$DEST" rev-parse HEAD)"
if [[ "$actual" != "${REF,,}" ]]; then
  echo "ERROR: checked out '$actual', expected '${REF,,}'." >&2
  exit 1
fi

test -f "$DEST/include/unitree/robot/g1/loco/g1_loco_client.hpp"
test -f "$DEST/lib/aarch64/libunitree_sdk2.a"
test -f "$DEST/thirdparty/lib/aarch64/libddsc.so"
test -f "$DEST/thirdparty/lib/aarch64/libddscxx.so"

echo "Prepared official unitree_sdk2 commit $actual at $DEST"
echo "The Docker build will independently verify all ARM64 ELF headers."

