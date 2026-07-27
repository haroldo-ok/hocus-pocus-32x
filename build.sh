#!/usr/bin/env bash
#
# One-shot build for Hocus Pocus 32X.
#
# Fetches the 32XDK Sega toolchain if it is not already present, builds the
# ROM, copies it to rom/, and runs the point-to-point test suite.
#
#   ./build.sh              build + test
#   ./build.sh --no-test    build only
#
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

# Keep the toolchain outside the workspace: it is ~400 MB and would blow the
# workspace snapshot limit.
TCROOT="${TCROOT:-/var/tmp/toolchain}"
GENDEV="${GENDEV:-$TCROOT/opt/toolchains/sega}"
DEVKIT_URL="https://github.com/viciious/32XDK/releases/download/20220418/chillys-sega-devkit-20220418-opt.tar.zst"

if [ ! -x "$GENDEV/sh-elf/bin/sh-elf-gcc" ]; then
    echo "==> Sega toolchain not found at $GENDEV, fetching..."
    mkdir -p /var/tmp/tcdl "$TCROOT"
    if [ ! -f /var/tmp/tcdl/devkit.tar ]; then
        curl -L -o /var/tmp/tcdl/devkit.tar.zst "$DEVKIT_URL"
        python3 - <<'PY'
import zstandard
d = zstandard.ZstdDecompressor()
with open('/var/tmp/tcdl/devkit.tar.zst', 'rb') as f, open('/var/tmp/tcdl/devkit.tar', 'wb') as o:
    d.copy_stream(f, o)
PY
        rm -f /var/tmp/tcdl/devkit.tar.zst
    fi
    tar xf /var/tmp/tcdl/devkit.tar -C "$TCROOT" \
        --exclude='*/share/locale/*' --exclude='*/share/man/*' \
        --exclude='*/share/info/*'   --exclude='*/share/doc/*'
    echo "==> toolchain installed"
fi

RUN_TESTS=1
MAKEARGS=()
for a in "$@"; do
    case "$a" in
        --no-test) RUN_TESTS=0 ;;
        *)         MAKEARGS+=("$a") ;;
    esac
done

echo "==> building with GENDEV=$GENDEV"
make GENDEV="$GENDEV" "${MAKEARGS[@]+"${MAKEARGS[@]}"}" 2>&1 \
    | grep -viE "linker input file unused|Assembler messages" || true

mkdir -p rom
cp -f build/hocus32x.32x rom/hocus32x.32x
echo "==> ROM: $HERE/rom/hocus32x.32x ($(stat -c %s rom/hocus32x.32x) bytes)"

if [ "$RUN_TESTS" = "1" ]; then
    echo "==> running point-to-point tests"
    python3 tests/test_rom.py
fi
