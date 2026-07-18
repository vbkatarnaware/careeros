#!/usr/bin/env bash
# Cuts the site's real product-demo clips from the source screen recordings.
#
# Source footage (not in the repo — lives on the maintainer's machine):
#   B = ~/Desktop/Screen Recording 2026-07-13 at 11.52.08 AM.mov  (careeros daily, gate stage)
#   C = ~/Desktop/Screen Recording 2026-07-13 at 12.13.28 PM.mov  (artifacts + apply stage)
#
# Re-run this script to regenerate website/public/media/ if the clips ever need
# re-cutting (different trim points, resolution, etc). Requires ffmpeg.
set -euo pipefail

SRC_B="$HOME/Desktop/Screen Recording 2026-07-13 at 11.52.08 AM.mov"
SRC_C="$HOME/Desktop/Screen Recording 2026-07-13 at 12.13.28 PM.mov"
OUT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/public/media"
mkdir -p "$OUT"

WIDTH=1280

# name | source | start(s) | duration(s)
CLIPS=(
  "hero-pipeline|$SRC_B|634|9.5"
  "gate-parallel|$SRC_B|358|14"
  "pipeline-start|$SRC_B|27|15"
  "resume-cover|$SRC_C|36|14"
  "apply-honest|$SRC_C|156|14"
)

for entry in "${CLIPS[@]}"; do
  IFS='|' read -r name src start dur <<< "$entry"
  echo "== $name (${dur}s @ ${start}s) =="

  ffmpeg -y -v error -ss "$start" -i "$src" -t "$dur" \
    -vf "scale=${WIDTH}:-2" -an -c:v libx264 -pix_fmt yuv420p -crf 23 -preset slow \
    -movflags +faststart "$OUT/$name.mp4"

  ffmpeg -y -v error -ss "$start" -i "$src" -t "$dur" \
    -vf "scale=${WIDTH}:-2" -an -c:v libvpx-vp9 -crf 32 -b:v 0 -row-mt 1 \
    "$OUT/$name.webm"

  # Poster: a frame roughly a third into the clip (usually past any scroll-jump).
  # JPEG, not WEBP — most ffmpeg builds ship without a webp encoder.
  poster_ts=$(awk -v s="$start" -v d="$dur" 'BEGIN { printf "%.2f", s + d/3 }')
  ffmpeg -y -v error -ss "$poster_ts" -i "$src" -frames:v 1 \
    -vf "scale=${WIDTH}:-2" -q:v 4 "$OUT/$name.jpg"

  ls -lh "$OUT/$name.mp4" "$OUT/$name.webm" "$OUT/$name.jpg"
done

echo "Done. Output in $OUT"
