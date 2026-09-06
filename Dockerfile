# No Playwright/Chromium here on purpose: scan-onboarding only accepts
# submissions and serves status/report pages now -- it enqueues a Cloud
# Task and never touches the pipeline itself. See Dockerfile.worker for
# the image that actually does, sized and scaled for that workload
# separately.
FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY mad_platform ./mad_platform

ENV PORT=8080
EXPOSE 8080

CMD ["sh", "-c", "uvicorn mad_platform.web.app:app --host 0.0.0.0 --port ${PORT}"]
