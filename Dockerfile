FROM python:3.11-slim

WORKDIR /app

# System deps for FAISS
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir faiss-cpu

# Application
COPY . .

# Pre-index the retriever at build time (optional – can be done at startup)
RUN python -c "from src.retrieval import HybridRetriever; \
    r = HybridRetriever(); \
    r.index_all(reset=True); \
    print(f'Indexed {len(r.docs)} docs')" || true

EXPOSE 8000

# Default: FastAPI server
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]
