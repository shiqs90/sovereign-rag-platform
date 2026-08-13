"""Swappable embedding and generation backends.

Each is a protocol with a production implementation and a dependency-free local one, so the
leakage gate runs on a laptop and in CI with no GPU and no model downloads.

The split is deliberate and mirrors the eval's two metrics: the tenant filter is exercised
identically by both backends, so `leakage_rate` is meaningful without a model. Retrieval
*ranking* is not, so `gold_article_hit_rate` is only meaningful against real TEI.
"""

import hashlib
import os
import re
from typing import Protocol

import httpx

# MUST match the embedding model TEI is serving (k8s/20-tei.yaml). Qdrant fixes vector
# size at collection creation, so changing this means dropping and re-ingesting both
# collections — a mismatch is rejected on every upsert.
#   BAAI/bge-m3            -> 1024
#   BAAI/bge-small-en-v1.5 -> 384
VECTOR_SIZE = int(os.environ.get("VECTOR_SIZE", "1024"))


class Embedder(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class Generator(Protocol):
    def generate(self, prompt: str) -> str: ...


class TEIEmbedder:
    """Production: text-embeddings-inference serving bge-m3 on CPU."""

    # Two limits force batching. TEI rejects more than --max-client-batch-size inputs
    # (default 32), and bge-m3 on 2 CPU cores is slow enough that a batch of 32 long
    # articles blew a 120s timeout. 8 keeps each request well inside the budget.
    BATCH = 8
    # Ingest is a batch job, not a serving path — there is no latency SLO to protect here.
    TIMEOUT = 600

    def __init__(self, url: str):
        self.url = url.rstrip("/")

    def embed(self, texts):
        vectors = []
        for start in range(0, len(texts), self.BATCH):
            response = httpx.post(
                f"{self.url}/embed",
                json={"inputs": texts[start : start + self.BATCH]},
                timeout=self.TIMEOUT,
            )
            response.raise_for_status()
            vectors.extend(response.json())
        return vectors


class HashEmbedder:
    """Deterministic pseudo-embeddings — no model, no download, no GPU.

    Ranking is arbitrary but reproducible. That is enough for the isolation gate, which
    asserts *which tenant* the retrieved chunks belong to, not how well they rank.
    """

    def embed(self, texts):
        vectors = []
        for text in texts:
            digest = hashlib.sha256(text.encode()).digest()
            vectors.append([(digest[i % len(digest)] / 255.0) - 0.5 for i in range(VECTOR_SIZE)])
        return vectors


class VLLMGenerator:
    """Production: vLLM's OpenAI-compatible endpoint."""

    def __init__(self, url: str, model: str):
        self.url = url.rstrip("/")
        self.model = model

    def generate(self, prompt):
        response = httpx.post(
            f"{self.url}/v1/chat/completions",
            json={
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "max_tokens": 512,
            },
            timeout=120,
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]


class FixtureGenerator:
    """Cites every passage it was given. Exercises the marker -> citation mapping without a GPU."""

    def generate(self, prompt):
        markers = sorted({int(m) for m in re.findall(r"^\[(\d+)\]", prompt, flags=re.MULTILINE)})
        return "Based on the retrieved provisions " + " ".join(f"[{m}]" for m in markers) + "."
