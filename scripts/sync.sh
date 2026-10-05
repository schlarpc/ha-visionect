#!/usr/bin/env bash
# Copy custom_components/visionect/ to one or more deployed trees, or check
# that they already match.
#
# This exists because there were three hand-copied copies of this integration
# and they had already diverged. Hand-copying N trees is not a workflow, it is
# a future bug report, so the copy is a script and the script can also just
# look.
#
# Usage:
#   scripts/sync.sh [--check] [--library <pyvisionect checkout>] <destination>...
#   scripts/sync.sh --check            # with destinations from .sync-targets
#
# pyvisionect is not on PyPI, so a deployed Home Assistant holds a hand-copied
# copy of it under <config>/deps as well -- a fourth tree, which had drifted
# far enough to be missing a whole module. --library syncs that too, from a
# checkout, for any destination that already has one.
#
# A destination is the directory the integration lives in, i.e. the one
# containing manifest.json -- for Home Assistant that is
# <config>/custom_components/visionect.
#
# Destinations may also be listed one per line in .sync-targets (gitignored,
# because they are local paths). Lines starting with # are ignored.

set -euo pipefail

cd "$(dirname "$0")/.."

SOURCE="custom_components/visionect"
CHECK=0
LIBRARY=""
declare -a TARGETS=()

while [ "$#" -gt 0 ]; do
  arg="$1"
  shift
  case "$arg" in
    --check) CHECK=1 ;;
    --library)
      LIBRARY="${1:?--library needs a path to a pyvisionect checkout}"
      shift
      ;;
    -h | --help)
      sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    -*)
      echo "unknown option: $arg" >&2
      exit 2
      ;;
    *) TARGETS+=("$arg") ;;
  esac
done

if [ "${#TARGETS[@]}" -eq 0 ] && [ -f .sync-targets ]; then
  while IFS= read -r line; do
    case "$line" in '' | '#'*) continue ;; esac
    TARGETS+=("${line/#\~/$HOME}")
  done <.sync-targets
fi

if [ "${#TARGETS[@]}" -eq 0 ]; then
  echo "no destinations given, and no .sync-targets file" >&2
  exit 2
fi

# translations/en.json is a copy of strings.json for a custom integration, and
# it had drifted six keys behind. Regenerate it here rather than trusting
# anyone to remember, so the thing being synced is correct before it is copied.
regenerate_en() {
  python3 - "$SOURCE" <<'PY'
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
strings = (root / "strings.json").read_text()
json.loads(strings)  # fail loudly on malformed JSON rather than copying it
en = root / "translations" / "en.json"
if en.read_text() != strings:
    if "--check" in sys.argv:
        print("translations/en.json differs from strings.json")
        sys.exit(1)
    en.write_text(strings)
    print("regenerated translations/en.json from strings.json")
PY
}

# <config>/custom_components/visionect -> the site-packages beside it that
# Home Assistant adds to sys.path for a custom integration's requirements.
library_dir() {
  local config
  config="$(cd "$1/../.." && pwd)"
  local found
  found="$(find "$config/deps/lib" -maxdepth 2 -type d -name site-packages \
    2>/dev/null | head -1)"
  [ -n "$found" ] && printf '%s/pyvisionect\n' "$found"
}

fingerprint() {
  # Sorted hash of every tracked-shaped file, so "do these two trees match"
  # has one answer and __pycache__ cannot affect it.
  (
    cd "$1"
    find . -type f \
      -not -path '*/__pycache__/*' \
      -not -name '*.py[cod]' \
      -print0 |
      sort -z |
      xargs -0 sha256sum |
      sha256sum |
      cut -d' ' -f1
  )
}

if [ "$CHECK" -eq 1 ]; then
  status=0
  regenerate_en --check || status=1
  want="$(fingerprint "$SOURCE")"
  for target in "${TARGETS[@]}"; do
    if [ ! -d "$target" ]; then
      echo "MISSING  $target"
      status=1
      continue
    fi
    if [ "$(fingerprint "$target")" = "$want" ]; then
      echo "ok       $target"
    else
      echo "DRIFTED  $target"
      diff -r -q \
        --exclude=__pycache__ \
        "$SOURCE" "$target" || true
      status=1
    fi
    if [ -n "$LIBRARY" ]; then
      lib="$(library_dir "$target" || true)"
      if [ -z "$lib" ] || [ ! -d "$lib" ]; then
        echo "no library  $target"
      elif [ "$(fingerprint "$lib")" = "$(fingerprint "$LIBRARY/src/pyvisionect")" ]; then
        echo "ok       $lib"
      else
        echo "DRIFTED  $lib"
        status=1
      fi
    fi
  done
  exit "$status"
fi

regenerate_en

for target in "${TARGETS[@]}"; do
  mkdir -p "$target"
  rsync -a --delete \
    --exclude='__pycache__/' \
    --exclude='*.py[cod]' \
    "$SOURCE/" "$target/"
  echo "synced   $target"

  if [ -n "$LIBRARY" ]; then
    lib="$(library_dir "$target" || true)"
    if [ -z "$lib" ]; then
      echo "         (no deps/ beside it, so no library to sync)"
    else
      mkdir -p "$lib"
      rsync -a --delete \
        --exclude='__pycache__/' \
        --exclude='*.py[cod]' \
        "$LIBRARY/src/pyvisionect/" "$lib/"
      echo "synced   $lib"
    fi
  fi
done

echo
echo "Home Assistant caches the integration, so restart it to pick this up:"
echo "  podman restart hass-visionect"
