# No Playwright/Chromium here on purpose: scan-onboarding only accepts
# submissions and serves status/report pages now -- it enqueues a Cloud
# Task and never touches the pipeline itself. See Dockerfile.worker for
# the image that actually does, sized and scaled for that workload
# separately.
#
# Pinned by digest, not just by the `3.13-slim` tag: that tag is mutable,
# so two builds a week apart were not the same image and a base-image
# regression could not be told apart from a change of ours. The tag is
# kept alongside the digest purely as a human-readable label -- Docker
# resolves the digest and ignores it. To move to a newer base, resolve
# the new digest deliberately and update all four Dockerfiles together
# -- they must stay on the same base.
FROM python:3.13-slim@sha256:9d2e5553305c7c7b0097999bb17187c69b921ccd6bc9d40e4bb5ebe652c00285

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY mad_platform ./mad_platform

# Drop root. Nothing in this image needs to write to the filesystem or
# bind a privileged port, so UID 0 buys nothing and costs the difference
# between a contained process compromise and a root one. Must come after
# every apt/pip step (those need root) and before CMD.
RUN useradd --create-home --uid 1000 app
USER app

ENV PORT=8080
EXPOSE 8080

CMD ["sh", "-c", "uvicorn mad_platform.web.app:app --host 0.0.0.0 --port ${PORT}"]
