# MyQ Garage Door Controller
# Docker image for the MyQ API server

FROM python:3.13-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code (exclude sensitive files via .dockerignore)
COPY server.py .
COPY myq_api.py .
COPY auto_capture_proxy.py .

# Create placeholder for tokens (will be mounted as volume)
RUN echo '{}' > myq_tokens.json

# Expose ports
# 8000 = API server
# 8888 = Token capture proxy
# 8889 = Capture status page
EXPOSE 8000 8888 8889

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Default command runs the API server
CMD ["python", "server.py"]
