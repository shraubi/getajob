FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir \
    --trusted-host pypi.org \
    --trusted-host files.pythonhosted.org \
    -r requirements.txt \
    && python -m playwright install --with-deps chromium

COPY jobbot ./jobbot
COPY ralph ./ralph
COPY scripts ./scripts
COPY main.py ./
RUN mkdir -p /app/data/resumes /app/storage

CMD ["python", "main.py"]
