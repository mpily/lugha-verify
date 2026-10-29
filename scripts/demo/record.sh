#!/bin/sh
# Record the demo: screenshots + video (webm, mp4, gif) into $1 (default demo_out).
#   sh scripts/demo/record.sh [out_dir]
# Needs: python deps (requirements.txt), Node with the playwright package +
# Chromium, and ffmpeg for mp4/gif (optional).
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
OUT=$(mkdir -p "${1:-demo_out}" && cd "${1:-demo_out}" && pwd)
export DEMO_PORT=${DEMO_PORT:-8765}
python "$HERE/demo_server.py" & SERVER=$!
trap 'kill $SERVER 2>/dev/null' EXIT
for i in $(seq 1 30); do curl -sf "http://127.0.0.1:$DEMO_PORT/health" >/dev/null && break; sleep 0.5; done
rm -f "$OUT"/*.webm
node "$HERE/record.js" "$OUT"
VIDEO=$(ls -t "$OUT"/*.webm | head -1); mv "$VIDEO" "$OUT/demo.webm"
if command -v ffmpeg >/dev/null; then
  ffmpeg -loglevel error -y -i "$OUT/demo.webm" -c:v libx264 -pix_fmt yuv420p -movflags +faststart "$OUT/demo.mp4"
  ffmpeg -loglevel error -y -i "$OUT/demo.webm" -vf "fps=6,scale=900:-1:flags=lanczos,split[a][b];[a]palettegen[p];[b][p]paletteuse" "$OUT/demo.gif"
fi
ls -la "$OUT"
