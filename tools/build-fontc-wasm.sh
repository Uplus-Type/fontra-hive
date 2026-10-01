#!/bin/sh
# Fontra Hive, "Try Fontra": build fontc (googlefonts/fontc) for the browser,
# as a WASI program (wasm32-wasip1). The page runs it in a worker
# (client/try/try-fontc-worker.js, on the in-memory WASI host of
# client/try/try-wasi.js) to export TTF and WOFF2 without a server.
# fontc reads the font as designspace + UFOs, prepared by
# client/try/py/hive_try_convert.py (fontcSources): it cannot compile
# .fontra yet at this revision. No OTF: fontc writes TrueType outlines.
#
# Usage: tools/build-fontc-wasm.sh [output-folder]   (default: ./build)
# Needs rustup (https://rustup.rs) and git. Takes a few minutes.
#
# The fontc revision is pinned: the page and the server expect this build
# (FONTC_REV, and the sha256 printed at the end, go in hive-api's install.sh).
#
# Copyright (c) 2026 Jérémie Hornus / U+Type — GPLv3, see LICENSE.
set -eu

FONTC_REV=${FONTC_REV:-4c75e67852cb9bde966e7532b1f2a1ae53e87e55}
OUT=${1:-build}
TARGET=wasm32-wasip1

command -v rustup >/dev/null || { echo "rustup is needed: https://rustup.rs" >&2; exit 1; }
rustup target add "$TARGET"

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
git clone --quiet https://github.com/googlefonts/fontc "$WORK/fontc"
git -C "$WORK/fontc" checkout --quiet "$FONTC_REV"

# Without rayon: no threads in the browser (fontc then works sequentially).
( cd "$WORK/fontc" && cargo build --locked --release -p fontc \
    --no-default-features --features cli --target "$TARGET" )

mkdir -p "$OUT"
NAME="fontc-${FONTC_REV%"${FONTC_REV#???????}"}.wasm"
cp "$WORK/fontc/target/$TARGET/release/fontc.wasm" "$OUT/$NAME"
ls -l "$OUT/$NAME"
if command -v shasum >/dev/null; then shasum -a 256 "$OUT/$NAME"; else sha256sum "$OUT/$NAME"; fi
