#!/bin/sh
set -e
[ -f /data/config.yaml ] || { echo "Missing data/config.yaml — copy config.example.yaml to data/config.yaml and fill it in." >&2; exit 1; }

rm -f /tmp/.X99-lock
Xvfb :99 -screen 0 1280x960x24 -nolisten tcp &
sleep 1
# VNC only listens inside the container; websockify turns it into a WebSocket for noVNC
x11vnc -display :99 -forever -shared -nopw -localhost -rfbport 5900 -quiet &
websockify --web /usr/share/novnc 6080 localhost:5900 >/dev/null 2>&1 &

exec python cita_watch.py --config /data/config.yaml --server "$@"
