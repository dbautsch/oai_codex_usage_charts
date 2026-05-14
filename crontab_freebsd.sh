#!/bin/sh
set -eu

# Source directory containing generated PNG files (adjust in crontab or environment).
SRC_DIR="${CODEX_USAGE_SOURCE_DIR:-/tmp/codex-demo}"

# Destination directory exposed by your web server (adjust in crontab or environment).
# The default keeps the value demo-like and non-sensitive.
DST_DIR="${CODEX_USAGE_DEST_DIR:-/var/www/html/demo}"

mkdir -p "$DST_DIR"
chmod 755 "$DST_DIR"

# Move only PNG charts so binary status assets stay separate.
for f in "$SRC_DIR"/*.png; do
    [ -f "$f" ] || continue
    mv "$f" "$DST_DIR/"
    chmod 0644 "$DST_DIR/$(basename "$f")"
done

printf "moved PNG charts from %s to %s\n" "$SRC_DIR" "$DST_DIR"
