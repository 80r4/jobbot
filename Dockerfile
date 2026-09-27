FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 DB_PATH=/data/jobs.db CONFIG_PATH=/app/config.yaml
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY jobbot ./jobbot
COPY config.yaml profile.md ./
EXPOSE 8000
# one worker only, so the scheduler runs once
CMD ["uvicorn", "jobbot.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers"]
