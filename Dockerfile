FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends unar \
    && rm -rf /var/lib/apt/lists/*

COPY requirements*.txt ./
RUN pip install --no-cache-dir -r requirements.lock.txt \
    && groupadd --gid 10001 leitor \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin leitor \
    && mkdir /data \
    && chown 10001:10001 /data

COPY leitor_dados ./leitor_dados
COPY scripts/init_secrets.py ./scripts/init_secrets.py

USER 10001:10001
EXPOSE 8000

CMD ["python", "-m", "uvicorn", "leitor_dados.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--no-access-log"]
