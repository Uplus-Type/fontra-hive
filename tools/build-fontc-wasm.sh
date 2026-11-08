#!/bin/sh
# Fontra Hive: build fontc (googlefonts/fontc) at the revision pinned here,
# from one clone, for the two places that compile fonts:
#
# - native: the program the server runs for TrueType and WOFF2 downloads
#   (src/fontra_hive/export.py), installed by hive-api's install.sh in
#   $HIVE/fontc/fontc-<rev> (link $HIVE/fontc/current);
# - wasm: "Try Fontra" in the browser, a WASI program (wasm32-wasip1) that the
#   page runs in a worker (client/try/try-fontc-worker.js, on the in-memory
#   WASI host of client/try/try-wasi.js).
#
# Both read the .fontra package directly (fontc's fontra2fontir). No OTF:
# fontc writes TrueType outlines; the server builds OTF with fontmake.
#
# Usage: tools/build-fontc-wasm.sh [output-folder]   (default: ./build)
#   FONTC_TARGETS="native wasm" (the default), or one of the two.
# Needs rustup (https://rustup.rs) and git. Takes ten to twenty minutes for
# both; the two targets compile their dependencies separately.
#
# Writes fontc-<rev7> and fontc-<rev7>.wasm, and prints their sha256. The
# revision is read from here by install.sh: one place for the server and the
# page, so that both build the same font.
#
# Copyright (c) 2026 Jérémie Hornus / U+Type — GPLv3, see LICENSE.
set -eu

FONTC_REV=${FONTC_REV:-d73bdfae3b08ae80db6b248f8a03cbe00eaf3bb6}
OUT=${1:-build}
TARGETS=${FONTC_TARGETS:-native wasm}
WASM=wasm32-wasip1
REV7=$(printf %s "$FONTC_REV" | cut -c1-7)

command -v rustup >/dev/null || { echo "rustup is needed: https://rustup.rs" >&2; exit 1; }
for target in $TARGETS; do
  case $target in
    native|wasm) ;;
    *) echo "unknown target $target (native, wasm)" >&2; exit 1 ;;
  esac
done
case " $TARGETS " in *" wasm "*) rustup target add "$WASM" ;; esac

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
# A full clone: fontc's build reads its tags and commit (git describe), which
# `fontc --vv` prints, and install.sh checks.
git clone --quiet https://github.com/googlefonts/fontc "$WORK/fontc"
git -C "$WORK/fontc" checkout --quiet "$FONTC_REV"

mkdir -p "$OUT"
sum() { if command -v shasum >/dev/null; then shasum -a 256 "$1"; else sha256sum "$1"; fi; }
for target in $TARGETS; do
  if [ "$target" = native ]; then
    # With rayon (the default features): the server has several cores.
    ( cd "$WORK/fontc" && cargo build --locked --release -p fontc )
    cp "$WORK/fontc/target/release/fontc" "$OUT/fontc-$REV7"
    built="$OUT/fontc-$REV7"
  else
    # Without rayon: no threads in the browser (fontc then works sequentially).
    ( cd "$WORK/fontc" && cargo build --locked --release -p fontc \
        --no-default-features --features cli --target "$WASM" )
    cp "$WORK/fontc/target/$WASM/release/fontc.wasm" "$OUT/fontc-$REV7.wasm"
    built="$OUT/fontc-$REV7.wasm"
  fi
  ls -l "$built"
  sum "$built"
done
