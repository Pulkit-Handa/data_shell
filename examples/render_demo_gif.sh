#!/usr/bin/env bash
#
# Render docs/demo.gif from the real output of `datashell demo`.
#
# Requires ImageMagick (`convert`, `identify`). This is a lightweight, recorder-free
# alternative to examples/demo.tape (which needs VHS): it captures the actual demo
# transcript and reveals it line by line.
#
#   ./examples/render_demo_gif.sh
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

OUT="docs/demo.gif"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# ImageMagick 7 renamed `convert` to `magick`; support both.
if command -v magick >/dev/null 2>&1; then
  CONVERT=(magick)
else
  CONVERT=(convert)
fi

# Monospace font + terminal palette.
FONT="Liberation-Mono"
POINTSIZE=14
BG="#1e1e2e"
FG="#e6e6e6"
ACCENT="#a6e3a1"
WIDTH=1000
PAD=24

# Prefer the installed console script; fall back to the module when running
# straight from a checkout that has not been pip-installed.
if command -v datashell >/dev/null 2>&1; then
  DATASHELL=(datashell)
else
  DATASHELL=(python -m datashell.cli.main)
fi

echo "== Capturing demo transcript =="
"${DATASHELL[@]}" demo >"$WORK/demo.txt" 2>&1
mapfile -t LINES <"$WORK/demo.txt"
TOTAL=${#LINES[@]}
echo "Captured $TOTAL lines"

# The final (fully revealed) frame determines the canvas height.
printf '%s\n' "${LINES[@]}" >"$WORK/full.txt"
"${CONVERT[@]}" -background "$BG" -fill "$FG" -font "$FONT" -pointsize "$POINTSIZE" \
  -size "$((WIDTH - 2 * PAD))x" caption:@"$WORK/full.txt" "$WORK/probe.png"
CANVAS_H=$(( $(identify -format '%h' "$WORK/probe.png") + 2 * PAD ))

render_frame() {  # $1 = number of lines to reveal, $2 = output png
  head -n "$1" "$WORK/demo.txt" >"$WORK/partial.txt"
  "${CONVERT[@]}" -background "$BG" -fill "$FG" -font "$FONT" -pointsize "$POINTSIZE" \
    -size "$((WIDTH - 2 * PAD))x" caption:@"$WORK/partial.txt" \
    -gravity northwest -extent "${WIDTH}x${CANVAS_H}" \
    -bordercolor "$BG" -border "$PAD" "$2"
}

echo "== Rendering ${TOTAL} frames (${WIDTH}x${CANVAS_H}) =="
for ((i = 1; i <= TOTAL; i++)); do
  render_frame "$i" "$WORK/frame_$(printf '%03d' "$i").png"
done

# Hold the final frame a little longer so the result is readable.
LAST_FRAME="$WORK/frame_$(printf '%03d' "$TOTAL").png"
cp "$LAST_FRAME" "$WORK/frame_$(printf '%03d' "$((TOTAL + 1))").png"

mkdir -p "$(dirname "$OUT")"
"${CONVERT[@]}" -delay 12 -loop 0 "$WORK"/frame_*.png -layers Optimize "$OUT"
echo "== Wrote $OUT ($(du -h "$OUT" | cut -f1)) =="
