# Self-hosting image (Raspberry Pi, NAS, VPS).
# The primary deployment target is AWS Lambda — see deploy/deploy.sh.

FROM python:3.13-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# uvicorn is not a Lambda dependency, so it is installed explicitly here.
RUN pip install --no-cache-dir -r requirements.txt "uvicorn>=0.32"

COPY myq ./myq

# Placeholder; mount a real token file over this.
RUN echo '{}' > myq_tokens.json

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["python", "-m", "uvicorn", "myq.api:app", "--host", "0.0.0.0", "--port", "8000"]
