FROM python:3.12-slim

# Virtual display + VNC + noVNC so you can drive the browser from your phone/laptop
RUN apt-get update \
 && apt-get install -y --no-install-recommends xvfb x11vnc novnc websockify fonts-liberation \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 DISPLAY=:99 PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && playwright install --with-deps chromium

COPY cita_watch.py entrypoint.sh ./
RUN chmod +x entrypoint.sh

# 8080 = control panel, 6080 = noVNC. Only Caddy reaches these (see docker-compose.yml).
EXPOSE 8080 6080
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz')"
ENTRYPOINT ["./entrypoint.sh"]
