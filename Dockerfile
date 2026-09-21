FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Add non-root user
RUN useradd -m -u 1000 -s /bin/bash appuser

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY alembic.ini .
COPY alembic/ alembic/
COPY src/ src/
COPY scripts/ scripts/

USER appuser

EXPOSE 8000

# --workers 1 is load-bearing, not a default nobody revisited. Every /mcp rate
# control is IN-PROCESS state: the two per-principal token buckets, the
# per-address failed-authentication table and the refusal coalescer all live in
# this worker's memory and are deliberately not persisted or shared. A second
# worker therefore does not split the configured rates between them — it gives
# each worker a full set, multiplying every effective rate by the worker count,
# and splits the coalescer so the same key writes a row per worker. Raising it
# means revisiting docs/architecture/rate-limits.md first.
#
# --no-proxy-headers is deliberate, not a removal of a control (#189).
# TRUSTED_PROXY_IPS is now the single place that states which peers may set
# X-Forwarded-*, and it drives the app's own ProxyHeadersMiddleware. uvicorn's
# layer is switched OFF rather than aligned with it, because uvicorn's
# proxy_headers defaults to *enabled* and its forwarded_allow_ips reads
# $FORWARDED_ALLOW_IPS: two lists that happen to agree is a state an operator
# can break from the environment without editing either file, and the previous
# --forwarded-allow-ips value here excluded the real proxy subnet and was
# therefore inert. Narrow the trust list in .env, not here.
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-proxy-headers"]
