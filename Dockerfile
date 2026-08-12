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
#
# --workers 1        the in-memory rate limiter only works with a single worker
# --no-proxy-headers NOT optional, and not a performance tweak. uvicorn enables
#                    --proxy-headers by DEFAULT, and its ProxyHeadersMiddleware
#                    rewrites request.client from the client-supplied
#                    X-Forwarded-For before any application code runs. That
#                    silently defeats this app's own TRUST_PROXY hop-count
#                    logic — which reads the header correctly, from the right —
#                    and, worse, poisons request.client.host, the value the
#                    failed-unlock backstop relies on precisely because it is
#                    supposed to be unforgeable. Two layers rewriting caller
#                    identity is how the bypass came back; the app's layer is
#                    the one that understands hop counts, so uvicorn's is off.
CMD ["sh", "-c", "uvicorn api:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --no-proxy-headers"]
