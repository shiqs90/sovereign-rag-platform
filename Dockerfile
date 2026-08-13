# Serving image for the rag-service. Deliberately does NOT carry the corpus tooling —
# no PyMuPDF, no PDF parsers. Extraction happens once on a laptop; the container only
# embeds, retrieves and generates.
#
# MUST be built for linux/amd64. AKS nodes are amd64; an arm64 image built on Apple
# silicon starts and immediately dies with "exec format error".
#
#   docker build --platform linux/amd64 -t $ACR/rag-service:v1 .

FROM python:3.13-slim

WORKDIR /srv

COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

# Non-root. Nothing here needs to write to the filesystem.
RUN useradd --uid 10001 --no-create-home rag
USER 10001

EXPOSE 8080

# No --reload. One worker: the service is I/O-bound on TEI, Qdrant and vLLM, and
# concurrency belongs in vLLM's continuous batching, not in extra Python processes.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
