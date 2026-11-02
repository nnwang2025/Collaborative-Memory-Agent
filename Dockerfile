FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# System deps for FAISS
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir faiss-cpu

# Application. .dockerignore must keep .env, live data, databases and reports out.
COPY . .

# Fail closed if a secret file nevertheless entered the build context.
RUN test ! -e .env \
    && test ! -e .env.local \
    && test ! -e .env.production \
    && test ! -e id_rsa \
    && test ! -e id_ed25519

# Pre-index the fixture retriever at build time. Live retrieval does not use this index.
RUN python -c "from src.retrieval import HybridRetriever; \
    r = HybridRetriever(); \
    r.index_all(reset=True); \
    print(f'Indexed {len(r.docs)} docs')" \
    || (echo "WARNING: fixture FAISS index build skipped; the app will use BM25/non-index paths" >&2)

# Run as an unprivileged numeric user; no account/home is required at runtime.
RUN chown -R 10001:10001 /app
USER 10001:10001

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)" || exit 1

# Default: FastAPI server
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]
