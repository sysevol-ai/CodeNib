FROM python:3.11.9-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    MPLBACKEND=Agg \
    MPLCONFIGDIR=/tmp/codeminer-matplotlib \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=0

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        fonts-liberation \
        libgomp1 \
        poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /artifact
COPY figure/requirements-paper.txt /tmp/requirements-paper.txt
RUN python -m pip install --no-cache-dir -r /tmp/requirements-paper.txt

COPY . /artifact

ENTRYPOINT ["python", "artifact_eval.py"]
CMD ["smoke"]
