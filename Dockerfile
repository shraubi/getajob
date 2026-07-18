FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/app/storage/playwright \
    PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT=120000

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir \
    --trusted-host pypi.org \
    --trusted-host files.pythonhosted.org \
    -r requirements.txt

COPY jobbot ./jobbot
COPY ralph ./ralph
COPY scripts ./scripts
COPY main.py ./
RUN mkdir -p /app/data/resumes /app/storage

FROM runtime AS bot
RUN python -m playwright install-deps chromium \
    && rm -rf /var/lib/apt/lists/* /root/.cache
# Ashby's invisible reCAPTCHA issues its token in a normal headed browser but
# stalls in Chromium's headless mode. Xvfb supplies a private virtual display
# without exposing a remote desktop or weakening challenge handling.
CMD ["xvfb-run", "-a", "--server-args=-screen 0 1280x1024x24", "python", "main.py"]

FROM runtime AS ralph
CMD ["python", "-m", "ralph.watch_events"]
