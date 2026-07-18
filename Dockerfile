FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

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
RUN python -m playwright install --with-deps --only-shell chromium \
    && rm -rf /var/lib/apt/lists/* /root/.cache
CMD ["python", "main.py"]

FROM runtime AS ralph
CMD ["python", "-m", "ralph.watch_events"]
