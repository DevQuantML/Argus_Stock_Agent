# Dockerfile — containerize the stock research agent for cloud deployment
#
# Build:  docker build -t stock-agent .
# Run:    docker run -p 8000:8000 --env-file .env stock-agent
#         NOTE: use --env-file, NOT -v mounting .env — keep secrets out of image
# Test:   curl http://localhost:8000/health

FROM python:3.11-slim

# Prevent Python from writing .pyc files and enable unbuffered output
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first (cached layer — only rebuilds if requirements.txt changes)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application
# NOTE: .dockerignore ensures .env is never copied into this image
COPY . .

# ── Security: run as non-root user ────────────────────────────────────────
# Running as root inside a container is a significant security risk.
# If the app is compromised, an attacker gains root inside the container,
# making container escape much easier.
RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser
USER appuser

# The port FastAPI listens on
EXPOSE 8000

# Start the API server
# Cloud providers (Railway, Fly.io, Render) inject PORT env var automatically
# Set workers=1 — in-memory rate limiter only works correctly with a single worker
CMD ["sh", "-c", "uvicorn api:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
