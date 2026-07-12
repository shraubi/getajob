FROM python:3.12-slim

WORKDIR /app

ARG REQUIREMENTS_FILE=requirements.txt
COPY requirements*.txt ./
RUN pip install --no-cache-dir \
    --trusted-host pypi.org \
    --trusted-host files.pythonhosted.org \
    -r "${REQUIREMENTS_FILE}"

COPY . .

CMD ["python", "main.py"]
