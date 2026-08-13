"""Multi-tenant RAG service.

Two rules carry the isolation thesis, and both live in this file:

1. The tenant is resolved from the API key. A client-supplied X-Tenant header never selects
   a collection — it is rejected with 403 and audited. That is the difference between a
   tenant boundary and a tenant suggestion.

2. The Qdrant filter is applied inside `retrieve()`, not at the request boundary. Isolation
   therefore holds however many times retrieval is called, and whatever arguments a future
   agent loop invents for it.
"""

import hashlib
import json
import os
import re
import time
import uuid
from collections import deque

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PointStruct,
    VectorParams,
)

from app.providers import (
    VECTOR_SIZE,
    FixtureGenerator,
    HashEmbedder,
    TEIEmbedder,
    VLLMGenerator,
)

# Defaults are the local-dev values; Kubernetes supplies the rest from a Secret/ConfigMap.
API_KEYS = json.loads(os.environ.get("API_KEYS", '{"key-a": "tenant-a", "key-b": "tenant-b"}'))
QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")

embedder = (
    TEIEmbedder(os.environ["TEI_URL"]) if os.environ.get("TEI_URL") else HashEmbedder()
)
generator = (
    VLLMGenerator(os.environ["VLLM_URL"], os.environ.get("VLLM_MODEL", "Qwen/Qwen2.5-7B-Instruct-AWQ"))
    if os.environ.get("VLLM_URL")
    else FixtureGenerator()
)

client = QdrantClient(url=QDRANT_URL)
app = FastAPI(title="sovereign-rag")

# Block E moves this to the Langfuse Postgres. In-memory is enough to prove the shape.
AUDIT = deque(maxlen=500)

PROMPT = """Answer the question using only the numbered passages below.
Cite passages by their marker, like [1]. Do not write article numbers yourself.
If the passages do not answer the question, say so.

{passages}

Question: {question}
Answer:"""


def auth(authorization: str = Header(...)) -> str:
    """The only place a tenant identity is ever established."""
    key = authorization.removeprefix("Bearer ").strip()
    tenant = API_KEYS.get(key)
    if not tenant:
        raise HTTPException(401, "unknown API key")
    return tenant


def audit(tenant, question, articles, refused, started, note=""):
    AUDIT.append(
        {
            "ts": time.time(),
            "tenant_id": tenant,
            # Storing data-subject queries verbatim inside a data-protection system is a
            # self-own. The hash is enough to correlate without retaining the question.
            "question_sha256": hashlib.sha256(question.encode()).hexdigest(),
            "retrieved_article_ids": articles,
            "refused": refused,
            "latency_ms": round((time.time() - started) * 1000),
            "note": note,
        }
    )


def retrieve(tenant: str, question: str, k: int):
    """Tenant filter lives HERE — inside the retriever, not at the request boundary."""
    vector = embedder.embed([question])[0]
    return client.query_points(
        collection_name=tenant,
        query=vector,
        query_filter=Filter(
            must=[FieldCondition(key="tenant_id", match=MatchValue(value=tenant))]
        ),
        limit=k,
        with_payload=True,
    ).points


class Article(BaseModel):
    article: int
    title: str = ""
    text: str


class IngestRequest(BaseModel):
    law: str
    source_url: str = ""
    articles: list[Article]


class QueryRequest(BaseModel):
    question: str
    k: int = 5


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/audit")
def get_audit(tenant: str = Depends(auth)):
    return [row for row in AUDIT if row["tenant_id"] == tenant]


@app.post("/ingest")
def ingest(req: IngestRequest, tenant: str = Depends(auth)):
    if not client.collection_exists(tenant):
        client.create_collection(
            tenant, vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE)
        )

    texts = [f"Article {a.article} — {a.title}\n{a.text}" for a in req.articles]
    vectors = embedder.embed(texts)

    points = [
        PointStruct(
            # Deterministic id => re-running ingest upserts instead of duplicating.
            id=str(uuid.UUID(hashlib.sha256(f"{tenant}:{a.article}".encode()).hexdigest()[:32])),
            vector=vector,
            payload={
                "tenant_id": tenant,
                "law": req.law,
                "article": a.article,
                "title": a.title,
                "text": a.text,
                "source_url": req.source_url,
            },
        )
        for a, vector in zip(req.articles, vectors)
    ]
    client.upsert(tenant, points=points)
    return {"ingested": len(points), "collection": tenant}


@app.post("/query")
def query(
    req: QueryRequest,
    tenant: str = Depends(auth),
    x_tenant: str | None = Header(default=None),
):
    started = time.time()

    # A header may not override the key. Rejected loudly and recorded.
    if x_tenant and x_tenant != tenant:
        audit(tenant, req.question, [], True, started, note=f"rejected X-Tenant={x_tenant}")
        raise HTTPException(403, "X-Tenant does not match the authenticated tenant")

    hits = retrieve(tenant, req.question, req.k)
    articles = [hit.payload["article"] for hit in hits]

    if not hits:
        audit(tenant, req.question, [], True, started)
        return {"answer": None, "citations": [], "refused": True, "chunks": []}

    passages = "\n\n".join(
        f"[{i}] {hit.payload['text']}" for i, hit in enumerate(hits, start=1)
    )
    answer = generator.generate(PROMPT.format(passages=passages, question=req.question))

    # The model emits markers only; article numbers come from metadata. That makes a
    # hallucinated citation structurally impossible rather than statistically unlikely.
    cited = sorted({int(m) for m in re.findall(r"\[(\d+)\]", answer)})
    citations = [
        {
            "marker": marker,
            "law": hits[marker - 1].payload["law"],
            "article": hits[marker - 1].payload["article"],
        }
        for marker in cited
        if 1 <= marker <= len(hits)
    ]

    audit(tenant, req.question, articles, False, started)
    return {
        "answer": answer,
        "citations": citations,
        "refused": False,
        # Every retrieved chunk's tenant_id — this is what the leakage gate asserts on.
        "chunks": [
            {"article": hit.payload["article"], "tenant_id": hit.payload["tenant_id"]}
            for hit in hits
        ],
    }
