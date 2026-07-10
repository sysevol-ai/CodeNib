FROM python:3.10-slim

RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /codeminer
COPY . /codeminer
RUN pip install --no-cache-dir -e ".[dev]"

ENTRYPOINT ["python", "scripts/guardian_cycle.py"]
