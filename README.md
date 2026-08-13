# Sovereign Multi-Tenant RAG Platform

A self-hosted retrieval-augmented generation platform where multiple regulated tenants share
one GPU but are provably isolated at the **data plane**, the **control plane**, and the
**network**.

Two tenants, each with their own legal corpus, ask the same question and get different
correct answers with different citations:

| Tenant | Corpus | "What are my breach notification obligations?" |
|---|---|---|
| `tenant-a` | UAE Federal Decree-Law 45/2021 (PDPL) | cites **PDPL Art. 9** |
| `tenant-b` | DIFC Data Protection Law No. 5 of 2020 | cites **DIFC Art. 41** |

A retrieval leak is therefore *visible*: the tenant would be told the obligations of a legal
regime that does not apply to them.

---

## Why this is the hard part

Government and regulated organisations want their own LLM stack — weights on their own
hardware, no data crossing the boundary, answers grounded in their own corpus. The economics
only work if several entities **share the GPU**; a dedicated accelerator per organisation is
unaffordable.

That produces the actual engineering problem: **shared compute, non-shared data.**

Most RAG systems don't address it. One vector collection behind one endpoint is fine until the
second tenant arrives, at which point "the retriever returned the wrong document" stops being a
quality bug and becomes a data-protection incident — under exactly the laws the corpus contains.

So isolation here is not a single `WHERE tenant_id = ?`. It is enforced independently at three
layers, and the evaluation suite fails the build if any of them leaks.

---

## Architecture

```
                        Ingress (nginx) + TLS (self-signed CA)
                    rag-a.<ip>.nip.io    rag-b.<ip>.nip.io
                            │                    │
              ns: tenant-a  ▼        ns: tenant-b ▼
              ┌──────────────────┐  ┌──────────────────┐
              │ rag-service      │  │ rag-service      │  own ServiceAccount,
              │ SA + Secret      │  │ SA + Secret      │  Role, Secret, NetworkPolicy
              └────┬────┬────┬───┘  └───┬────┬────┬────┘
                   │    │    │          │    │    │
              ns: rag-platform ─────────┴────┴────┴──────
              ┌────────┐ ┌──────┐ ┌──────────┐ ┌──────────┐
              │ vLLM   │ │ TEI  │ │ Qdrant   │ │ Langfuse │
              │ (GPU)  │ │(CPU) │ │ 2 collns │ │ +Postgres│
              └────────┘ └──────┘ └──────────┘ └──────────┘
                  all ClusterIP — never exposed externally
```

| Layer | Isolation mechanism | How it's proven |
|---|---|---|
| **Data** | Qdrant metadata filter on `tenant_id`, applied inside the retriever — not at the request boundary | Eval asserts every retrieved chunk's `tenant_id` equals the caller's, across all queries |
| **Control** | Namespace per tenant; ServiceAccount + Role scoped to its own Secret/ConfigMap only | `kubectl auth can-i get secrets -n tenant-b --as=…:tenant-a:rag-sa` → `no` |
| **Network** | Calico NetworkPolicy: default-deny, egress to `rag-platform` and DNS only | `kubectl exec` from tenant-a to tenant-b's Service → timeout |

### Stack

| Component | Choice | Why |
|---|---|---|
| Cluster | AKS, 3 node pools (`system` / `apps` / `gpu`) | GPU node is the scarce resource; taint keeps everything else off it |
| Inference | vLLM, `Qwen2.5-7B-Instruct-AWQ` | see *Model selection* below |
| Embeddings | TEI, `BAAI/bge-m3`, **on CPU** | see *Why embeddings run on CPU* below |
| Vector store | Qdrant, one collection per tenant | Filtered-search correctness; see *Vector store* below. Named-vector alternative would put both tenants in one collection — a worse blast radius |
| Observability | Langfuse v2 (traces, spans, scores) | Per-trace, not per-request: an agentic turn is N GPU calls |
| Ingress | ingress-nginx + cert-manager, self-signed CA | Proves the Issuer → Certificate → Secret path without owning DNS |

---

## Design decisions worth defending

### Model selection — constraint first, not benchmark

The T4 is Turing (SM 7.5): 16 GB, **no bf16, no Marlin kernels, no FP8**. That narrows the field
to a 7B at 4-bit AWQ before quality is even discussed.

Within that constraint, RAG needs three behaviours more than it needs raw reasoning:
**instruction-following** (stay inside the retrieved context), **reliable refusal** (say "not in
these documents"), and **clean structured output** (citation markers, tool-call JSON). It also
needs to be **ungated**, so there is no Hugging Face token dependency in the deploy path.

`Qwen2.5-7B-Instruct-AWQ` satisfies all four. Llama-3.1-8B is gated; Mistral-7B is weaker at
instruction-following; Hermes-2-Pro is a better pure tool-caller but weaker at general RAG.
On an H100 the calculus changes entirely — FP8 becomes available and a 32B or an FP8 70B is back
on the table.

```
--model=Qwen/Qwen2.5-7B-Instruct-AWQ
--quantization=awq          # NOT awq_marlin — Marlin requires SM 8.0+, T4 is 7.5
--dtype=float16             # Turing has no bf16
--gpu-memory-utilization=0.90
--max-model-len=8192
--enable-auto-tool-choice
--tool-call-parser hermes   # the Qwen2.5 tool-call parser
```

Memory: ~5.6 GiB of weights leaves ~7 GiB of KV cache at util 0.90 — comfortable on 16 GB.

### The LLM never writes an article number

Retrieved chunks are numbered `[1]…[k]` in the prompt. The model may cite **markers only**. The
service maps marker → chunk metadata → `PDPL Art. 9`.

This makes hallucinated citations *structurally impossible* rather than statistically unlikely,
and turns citation accuracy from an LLM-judge problem into a schema check. It is the single most
important design call in the system.

### Vector store — three of the four candidates are eliminated by constraints

| Candidate | Verdict |
|---|---|
| **Pinecone** | Eliminated by the thesis. Managed SaaS means the corpus leaves the boundary — indexing data-protection law on someone else's infrastructure contradicts the premise. Not a tradeoff; a contradiction. |
| **FAISS** | Wrong category. A library, not a database: no server, no auth, no concurrent writers, metadata filtering not first-class. The index would live *inside* `rag-service`, so ingest becomes redeploy — or you write a server around it and have rebuilt a worse Qdrant. |
| **OpenSearch** | Viable, priced out. Better hybrid BM25+vector, which suits a corpus users cite by exact string. But JVM heap alone wants 2–4 GiB against Qdrant's ~1 GiB budget, in a pool where TEI already takes 3 GiB. Forces a bigger VM against a 6/10 regional vCPU quota. |
| **pgvector** | The strongest objection — Postgres is already running for Langfuse, so it costs zero extra pods. Rejected because it makes the *observability* database the tenant data store: Langfuse filling its disk would take retrieval down, and a compromised Langfuse pod would sit inside the corpus. |

**Why Qdrant wins on the one axis that matters here: filtered-search correctness.**

The tenant filter is a security boundary, not a preference. Stores that implement filtering as
*post*-filtering — retrieve global top-k, then discard non-matching rows — fail silently under a
tenant filter: if this tenant's chunks miss the global top-k, the retriever returns fewer than k
results, or none, and degrades exactly as the corpus grows. Qdrant applies payload filters
*during* HNSW traversal, so a tenant-scoped search returns the true top-k **within that tenant**.

That is what makes `recall@5 ≥ 0.90` a real gate — it is measured under the filter, and
post-filtering would make the number a lie.

Secondary: Rust single binary, no JVM, ~1 GiB; collections are a native isolation unit, so
one-per-tenant is idiomatic rather than a convention the app enforces.

**What would change the answer:** hybrid keyword search dominating quality → OpenSearch. The
sovereignty requirement dropped → Pinecone, cheaper to operate. Millions of vectors per tenant →
collection-per-tenant stops scaling; move to single-collection with payload partitioning.

### Why embeddings run on CPU

Both models fit on the T4 on paper. They run separately because two pods cannot share one
`nvidia.com/gpu: 1` without time-slicing — and time-slicing gives scheduling isolation with
**zero memory isolation**. One OOM would take down inference and embedding together.

Embedding is a one-time batch cost at ingest, not on the serving hot path, so CPU is the correct
place for it. The GPU stays dedicated to the latency-sensitive workload.

### Tenant identity is resolved server-side

The tenant is derived from the API key alone. A client-supplied `X-Tenant` header never
influences collection selection — it is rejected with `403` and written to the audit log. This is
the difference between a tenant boundary and a tenant *suggestion*.

### Audit records hash the question

Storing data-subject queries verbatim inside a data-protection system is a self-own. The audit
row keeps `question_sha256`, the retrieved article IDs, guardrail decisions, refusal status and
latency — enough to reconstruct *what the system did* without retaining *what was asked*.

---

## Evaluation

Deterministic metrics only — no LLM judge, so results are reproducible and the gate is meaningful.

| Metric | Threshold |
|---|---|
| **Tenant leakage rate** | **exactly 0** — no error budget |
| Retrieval recall@5 (gold article) | ≥ 0.90 |
| Citation validity (every citation traces to a retrieved chunk) | 1.00 |
| Refusal rate on unanswerable questions | ≥ 0.90 |
| **False-refusal rate on answerable questions** | **≤ 0.10** |
| p95 latency | reported, not gated |

The false-refusal metric is load-bearing. Without it, refusal rate is trivially gamed by
refusing everything.

The question set includes **cross-tenant traps** — asking tenant A a question only DIFC answers.
The correct behaviour is *refusal*, not a DIFC answer. These are never trimmed from the set.

---

## Known limitation

Qdrant OSS has a single instance-wide API key; per-collection authorization is not first-class.
The tenant boundary is therefore enforced by the **application and NetworkPolicy**, not by the
vector store. A compromised tenant-A pod could reach tenant-B's collection.

Closing it properly means Qdrant JWT collection-scoped tokens, or one Qdrant instance per tenant
once the tenant count justifies the cost. It is documented rather than hidden because knowing
where your boundary *isn't* is part of owning it.

---

## Repository layout

```
terraform/     AKS (3 pools), GPU Operator, resource group
k8s/           Namespaces, RBAC, Secrets, NetworkPolicies, Ingress, workloads
app/           FastAPI RAG service — /ingest, /query, /healthz, /audit
corpus/        Normalised legal texts (## Article N — Title)
eval/          questions.yaml + run_eval.py
scripts/       verify.sh, ingest.sh, verify-eval-isolation.sh
docs/          IMPLEMENTATION-PLAN.md, instance_math.md
```

- [`docs/P10-STATUS.md`](docs/P10-STATUS.md) — what is built, what is not, the next command,
  and the decisions already settled. **Read this first when resuming.**
- [`docs/P10-TROUBLESHOOTING.md`](docs/P10-TROUBLESHOOTING.md) — five real failures hit while
  bringing this up, what each looked like versus what actually caused it
- [`docs/ENHANCEMENTS.md`](docs/ENHANCEMENTS.md) — work deliberately not done, with the reason
  and the production answer for each
- [`docs/instance_math.md`](docs/instance_math.md) — node pool sizing, regional quota ceilings,
  GPU memory and KV cache budget, cost breakdown
- [`docs/IMPLEMENTATION-PLAN.md`](docs/IMPLEMENTATION-PLAN.md) — execution order and the
  pre-identified failure modes

---

## Status

| Component | State |
|---|---|
| Terraform (AKS, 3 pools, GPU Operator) | written, not yet applied |
| Corpus normalisation | not started |
| Data plane (Qdrant, TEI, vLLM) | not started |
| RAG service | not started |
| RBAC / Secrets / NetworkPolicy / Ingress | not started |
| Evaluation suite | not started |
| Langfuse | not started |

See `docs/IMPLEMENTATION-PLAN.md` for the execution order and the pre-identified failure modes.

---

## Running it

```bash
cd terraform && terraform init && terraform apply

az aks get-credentials -g rg-sovereign-rag -n sovereign-rag --overwrite-existing
kubectl get nodes -o wide            # gpu node Ready, containerd 1.7.x

bash scripts/verify.sh               # vLLM, TEI, Qdrant reachable
bash scripts/ingest.sh               # idempotent; populates both collections
bash scripts/verify-eval-isolation.sh     # the six isolation proofs
python eval/run_eval.py              # leakage=0, recall>=0.90, citations=1.00
```

## Teardown

The GPU node is the cost. Scale it to zero between sessions; destroy everything when done.

```bash
az aks nodepool scale -g rg-sovereign-rag --cluster-name sovereign-rag --name gpu --node-count 0
cd terraform && terraform destroy
```

`kubectl delete statefulset` does **not** remove `volumeClaimTemplates` PVCs — only destroying
the resource group does.

Approximate cost: **~$1.07/hr** warm, **~$0.42/hr** with the GPU pool at zero.
