# Sovereign Multi-Tenant RAG Platform — Implementation Plan

**Build window: 12–16 hours. Infrastructure is torn down immediately after — see Teardown.**

---

## Problem

Government and regulated organisations increasingly want their own LLM stack: self-hosted
weights, no data crossing the boundary, and answers grounded in their own legal corpus rather
than in a foundation model's training data. The economics only work if several entities share
the GPU — a dedicated accelerator per ministry is unaffordable. That produces the real
engineering problem: **shared compute, non-shared data.**

Most RAG systems don't address it. A single vector collection behind a single endpoint is fine
until the second tenant arrives, at which point "the retriever returned the wrong document"
stops being a quality bug and becomes a data-protection incident — under exactly the laws the
corpus contains.

This platform makes that boundary explicit and testable: isolation enforced independently at
the data plane, the control plane and the network, with an evaluation suite that fails the
build if any layer leaks.

### The thesis (one sentence)

> A self-hosted, multi-tenant RAG platform where two government tenants share expensive GPU
> infrastructure but are provably isolated at the data plane, control plane, and network.

### The demo centerpiece

Same question — *"What are my breach notification obligations?"* — asked by two tenants,
returns **two different correct answers with two different citations**:

| Tenant | Corpus | Answer cites |
|---|---|---|
| `tenant-a` | UAE Federal Decree-Law 45/2021 (PDPL) | PDPL Art. 9 |
| `tenant-b` | DIFC Data Protection Law No. 5 of 2020 | DIFC Art. 41 |

A leak is therefore **visible** — the ministry would get the wrong legal regime. Then show the
metadata filter preventing it, and the spoofing attempt being rejected and audited.

---

## Architecture

```
                        Ingress (nginx) + TLS (self-signed CA)
                    rag-a.<ip>.nip.io    rag-b.<ip>.nip.io
                            │                    │
              ns: tenant-a  ▼        ns: tenant-b ▼
              ┌──────────────────┐  ┌──────────────────┐
              │ rag-service      │  │ rag-service      │  own SA, Role,
              │ SA + Secret      │  │ SA + Secret      │  Secret, NetworkPolicy
              └────┬────┬────┬───┘  └───┬────┬────┬────┘
                   │    │    │          │    │    │
              ns: rag-platform ─────────┴────┴────┴──────
              ┌────────┐ ┌──────┐ ┌──────────┐ ┌──────────┐
              │ vLLM   │ │ TEI  │ │ Qdrant   │ │ Langfuse │
              │ (GPU)  │ │(CPU) │ │ 2 collns │ │ +Postgres│
              └────────┘ └──────┘ └──────────┘ └──────────┘
                  all ClusterIP — never exposed
```

**Node pools** (australiacentral, ~14 regional vCPU, 10 used, 4 for surge):

| Pool | VM | Count | Labels / Taints | Runs |
|---|---|---|---|---|
| `system` | `Standard_D2s_v3` | 1 | `pool=system` | kube-system, ingress-nginx, cert-manager, GPU Operator controller |
| `apps` | `Standard_D4s_v3` | 1 | `pool=apps`, **no taint** | Qdrant, TEI, rag-services, Langfuse, Postgres |
| `gpu` | `Standard_NC4as_T4_v3` | 1 | `workload=gpu`, taint `nvidia.com/gpu=present:NoSchedule` | vLLM only |

`D2s_v3`/`D4s_v3` because DSv3 is the family **already quota-verified in australiacentral**
(`vllm-serving-aks/terraform/variables.tf:20`). Do not introduce v6 sizes here — those were
verified in westeurope, which has zero T4 quota.

---

## Five landmines — decided up front, not discovered later

**1. `network_profile` must be set at cluster creation.** `vllm-serving-aks/terraform/aks.tf` has
no `network_profile`, so the cluster has **no network policy engine**. NetworkPolicies would be
accepted by the API server and silently never enforced. This cannot be added to a live cluster.

```hcl
network_profile {
  network_plugin      = "azure"
  network_plugin_mode = "overlay"
  network_policy      = "calico"
}
automatic_upgrade_channel = "none"   # AKS auto node-image upgrade does a SURGE upgrade
node_os_upgrade_channel   = "None"   # -> needs 2x pool vCPU -> ErrCode_InsufficientVCPUQuota
```

**2. Never let the LLM write article numbers.** Number retrieved chunks `[1]`…`[k]` in the prompt;
the model cites markers only; the **service** maps marker → chunk metadata → `PDPL Art. 9`.
Consequence: hallucinated citations become structurally impossible, and citation accuracy becomes
a schema check instead of an LLM-judge problem. This is the single most important design call.

**3. Default-deny egress NetworkPolicy must explicitly allow DNS** to `kube-system` on port 53,
**UDP and TCP**. Forget it and every pod hangs on name resolution and you blame the wrong thing.

**4. Tenant is resolved server-side from the API key only.** A client-supplied `X-Tenant` header
must never influence collection selection — it gets rejected and audited (this is demo proof #5).

**5. `enableServiceLinks: false` on the vLLM pod.** A Service named `vllm` injects
`VLLM_PORT=tcp://…` which vLLM misparses as its listen port and crashes. Already proven in
`vllm-serving-aks/k8s/vllm-deployment.yaml`.

---

## Execution blocks

### Block A — infra up (0:00–2:00)

1. `terraform destroy` any live cluster first — two clusters at once blows the budget.
2. Quota gate: `az vm list-usage --location australiacentral -o table | grep -Ei "Total Regional|Standard D|NCASv3"`
3. Terraform apply: RG, AKS (with `network_profile` + upgrade channels off), 3 pools, GPU Operator.
4. **Do not idle-wait** the 45–90 min GPU node — start Block B's corpus work in parallel.

*Verify:* `kubectl get nodes -o wide` → gpu node `Ready`, `CONTAINER-RUNTIME` reads
`containerd://1.7.x` (not `unknown`). `kubectl get node -o json | jq '.items[].status.allocatable'`
shows `nvidia.com/gpu: 1`.

### Block B — corpus (runs parallel to A, ~1:00)

Convert both laws **once, offline** to `corpus/uae-pdpl.md` and `corpus/difc-dp-2020.md` with
`## Article N — Title` headers. Commit the markdown. Ingest reads markdown, never PDF.

Chunking: **one chunk = one Article.** Metadata `{tenant_id, law, article_number, source_url}`.
Expect ~50–80 chunks (PDPL), ~120–180 (DIFC).

*Cutoff:* if official English texts fight back for >30 min, switch to GDPR + EU AI Act and move on.

### Block C — data plane (2:00–4:00)

- Qdrant StatefulSet + PVC (`managed-csi`, 8Gi), ClusterIP.
- TEI Deployment on **CPU**, `BAAI/bge-m3`, model as an env var (fallback `BAAI/bge-small-en-v1.5`).
- vLLM Deployment — copy the proven manifest, swap flags:

```
--model=Qwen/Qwen2.5-7B-Instruct-AWQ
--quantization=awq         # NOT awq_marlin — Marlin needs SM 8.0+, T4 is SM 7.5
--dtype=float16            # Turing (SM 7.5) has no bf16
--enforce-eager
--gpu-memory-utilization=0.90
--max-model-len=8192
--max-num-seqs=8
--enable-auto-tool-choice  # emit OpenAI-format tool_calls, not raw text
--tool-call-parser hermes  # the Qwen2.5 tool-call parser
```
7B (not 1.5B) because tool-calling quality is model-bound — a 1.5B emits malformed tool JSON.
The two tool flags make vLLM parse calls into structured `tool_calls` objects, removing most
of the agentic implementation risk. Cost: ~25–35 tok/s vs ~60–80, so eval runs take ~6 min.
Memory: 5.6 GiB weights leaves ~7 GiB KV at util 0.90 — comfortable on a 16 GB T4.

Model selection — constraint first, not benchmark. T4 is Turing —
16 GB, no bf16, no Marlin, no FP8 — so it's a 7B at 4-bit AWQ or nothing. Within that, RAG needs
instruction-following, reliable refusal and clean structured output more than raw reasoning, and
the model must be **ungated** so there's no HF-token dependency in the deploy path. Qwen2.5-7B-
Instruct-AWQ hits all four (Llama-3.1-8B is gated; Mistral-7B is weaker at instruction-following;
Hermes-2-Pro is a better pure tool-caller but weaker at general RAG). It also has usable Arabic,
which keeps the "architecture is Arabic-ready" story honest.

Keep `enableServiceLinks: false`, the toleration, `nodeSelector: {workload: gpu}`, and the
startupProbe (`periodSeconds: 10`, `failureThreshold: 60`) verbatim.

*Verify:* `scripts/verify.sh` (adapted) — port-forward each, curl `/v1/models`, `/embed`, Qdrant `/collections`.

**Why TEI on CPU, not the GPU:** the math says both fit (1.5B leaves ~8.7 GiB KV headroom at
util 0.85; bge-m3 needs ~3 GiB). It's on CPU because two pods can't share one `nvidia.com/gpu: 1`
without time-slicing, and time-slicing gives scheduling isolation but **zero memory isolation** —
one OOM takes both down. Embedding is a one-time batch cost here, not on the serving hot path.
*Record this reasoning in the README — it is a non-obvious call that will be questioned.*

### Block D — RAG service (4:00–7:00) ← **HARD GATE**

`app/` — FastAPI, ~300 lines:
- `POST /ingest` — idempotent, upsert by deterministic ID (`sha256(tenant + article_number)`)
- `POST /query` — resolve tenant from API key → embed → Qdrant search **filtered by tenant** →
  number chunks `[1]..[k]` → prompt vLLM → map markers back to citations → structured JSON
- `GET /healthz`, `GET /audit`
- Generation behind a `Generator` protocol (`VLLMGenerator` / `FixtureGenerator`) from the first
  commit — makes the eval runnable without a warm GPU.

Response shape:
```json
{"answer": "...", "citations": [{"marker": 1, "law": "UAE PDPL", "article": 9}],
 "refused": false, "trace_id": "..."}
```

*Gate:* the centerpiece query returns two different correct answers with correct citations.
**If this isn't working, cut everything downstream and fix it.**

### Block D2 — agentic tool loop (+1:30, after the gate passes)

Two tools exposed to the model via vLLM's OpenAI tool-calling:

- `search_corpus(query, k)` — the retriever
- `list_articles(topic)` — browse article titles, so the model can refine before searching

A plain `while` loop: send messages + tools → if the response has `tool_calls`, execute them,
append results as `role: tool` messages, loop → else return. Cap at 4 iterations.

**Why this strengthens the thesis rather than diluting it.** The tenant filter is applied inside
the tool implementation, not at the request boundary:

> *"An agent decides to search five times instead of once — that's five chances to leak. The
> tenant filter is enforced at the tool layer, so isolation holds regardless of how many times
> the model chooses to retrieve, or what arguments it invents."*

Demo query needing multi-hop: *"Must I notify both the regulator and the data subject, and what
are the deadlines?"* — two searches, one trace.

Capacity consequence: an agent turns one request into N unpredictable GPU calls, which
breaks capacity planning, p99 budgets and per-request cost — the reason agentic workloads need
per-trace observability rather than per-request metrics.

*Paid for by:* eval set 24 → 16 questions (keep all 4 cross-tenant traps), Langfuse timebox
90 → 60 min.

### Block E — governance: RBAC, Secrets, Ingress, NetworkPolicy (7:00–9:30)

*This block is the control-plane and network half of the isolation thesis. Do not cut it —
without it, "multi-tenant" means nothing more than a WHERE clause.*

- Namespaces `rag-platform`, `tenant-a`, `tenant-b`.
- Per tenant: ServiceAccount, Role (get its own ConfigMap/Secret **only**), RoleBinding,
  Secret (API key), NetworkPolicy.
- NetworkPolicy per tenant ns: default-deny, allow egress to `rag-platform` + **DNS to kube-system
  53 UDP/TCP**, deny tenant↔tenant.
- ingress-nginx + cert-manager **self-signed root → CA Issuer → per-host Certificate**.
  Hosts `rag-a.<lb-ip>.nip.io`, `rag-b.<lb-ip>.nip.io`. No DNS ownership, no ACME, ~30 min.
- Guardrails (~60 lines, no framework): PII regexes (Emirates ID, passport, email, phone),
  prompt-injection heuristic, refuse when zero citations produced.
- Audit table in the Langfuse Postgres (no extra pod). **Hash the question** —
  storing data-subject queries verbatim inside a data-protection demo is a self-own:

```
{ts, tenant_id, service_account, question_sha256, retrieved_article_ids[],
 guardrail_decisions[], refused, latency_ms}
```

### Block F — proof + evaluation (9:30–11:00)

`scripts/verify-isolation.sh`, mirroring the port-forward/trap/retry/assert structure of
`vllm-serving-aks/scripts/verify.sh`:

| # | Proof | Expected |
|---|---|---|
| 1 | `kubectl auth can-i get secrets -n tenant-b --as=system:serviceaccount:tenant-a:rag-sa` | `no` |
| 2 | `kubectl auth can-i --list --as=system:serviceaccount:tenant-a:rag-sa -n tenant-a` | own Secret/ConfigMap, get-only |
| 3 | `kubectl exec -n tenant-a deploy/rag -- curl -m 5 http://rag.tenant-b:8080/healthz` | timeout |
| 4 | Same question, two API keys | two answers, two citation sets |
| 5 | Tenant A key + `X-Tenant: tenant-b` | `403` + audit record |
| 6 | Eval: every retrieved chunk's `tenant_id` == caller | zero exceptions |

`eval/questions.yaml` — 16 questions: 5 PDPL, 5 DIFC, 2 unanswerable, **4 cross-tenant traps**
(ask tenant A a DIFC-specific question → expect *refusal*, not a DIFC answer). The traps are
never trimmed — they are the thesis.

`eval/run_eval.py` — deterministic metrics only, no LLM judge:

| Metric | Threshold |
|---|---|
| **Tenant leakage rate** | **exactly 0** — no error budget |
| Retrieval recall@5 (gold article) | ≥ 0.90 |
| Citation validity (every citation traces to a retrieved chunk) | 1.0 |
| Refusal rate on unanswerable | ≥ 0.90 |
| **False-refusal on answerable** | **≤ 0.10** — without this, refusal is gameable by refusing everything |
| p95 latency | reported, not gated |

Plus **score separation** (~10 lines): mean top-1 similarity, answerable vs unanswerable. It's the
empirical justification for the refusal threshold.

### Block G — Langfuse, docs, teardown (11:00–14:00)

- **Langfuse v2 only** (`langfuse/langfuse:2` + Postgres, 2 pods, ~1.5 GiB). v3 needs
  ClickHouse + Redis + S3 and ~8 GiB — impossible here. **Pin `langfuse==2.60.*`**; SDK 3.x
  silently drops traces against a v2 server. `NEXTAUTH_URL` must exactly match the access URL.
  **Hard 60-min timebox** → fall back to JSONL traces + `GET /traces`.
- README: architecture, the five landmines, the TEI-on-CPU memory math, the Qdrant OSS
  authorization weakness (see below), teardown.
- **Record a 3-minute screen capture of the two-tenant demo.** Worth more than the README.
- `terraform destroy`.

---

## Deliberate scope cuts

| Cut | Why |
|---|---|
| GitHub Actions CI gate | Needs a warm GPU or a fixture harness. The `Generator` protocol leaves the door open; build it later. |
| Langfuse v3 | ~8 GiB RAM for a demo's worth of spans. |
| Let's Encrypt / real domain | 3-hour DNS+ACME rabbit hole. Self-signed CA proves the same Issuer/Certificate/Secret understanding. |
| Prometheus / Grafana | +1.5 GiB and +2h for system metrics that don't test the isolation boundary. vLLM's `/metrics` is scrapeable whenever it's wanted. |
| NeMo Guardrails / Llama Guard | Needs a GPU slot. 60 lines of Python is fully defensible. |
| Time-slicing TEI onto the GPU | Do the math in the README instead. |

**Never cut:** two-tenants-two-answers, leakage = 0, the `kubectl auth can-i` proof, the README
decision log.

---

## Future enhancements

### LangGraph for the agent loop

D2 ships as a plain `while` loop, deliberately. vLLM's `--enable-auto-tool-choice
--tool-call-parser hermes` already returns OpenAI-format `tool_calls`, so the parsing that agent
frameworks exist to solve is done in the serving layer — what's left is ~20 lines.

Scored against the alternative (higher = better; complexity scored as *cheapness*):

| Axis | Plain loop | LangGraph |
|---|---|---|
| Relevance to this project's thesis | **9** — the tenant filter is visibly inside the tool function | 5 — same filter, now behind `ToolNode` |
| Complexity (build + operate + defend) | **9** — no deps, fast pod start | 5 — heavy transitive tree, slower cold start |
| Industry relevance | 6 — infra-correct, no keyword match | **7** — common in LLMOps JDs |
| **Total** | **24/30** | 17/30 |

**Migration cost if adopted later: ~half a day.** The loop is ~20 lines in one place, tools are
already plain functions, and generation is already behind the `Generator` protocol. Nothing
architectural has to be unpicked — swapping in a `StateGraph` + `ToolNode` + `tools_condition`
is a local change.

**Adopt when** the loop needs durable checkpointing, resumable state, or human-in-the-loop
interrupts — that is what LangGraph actually buys. A capped 4-iteration retrieval loop needs
none of it. Adopting it for the CV keyword alone weakens the isolation argument, which is the
whole project.

---

## Known weakness — name it, don't hide it

Qdrant OSS has a **single instance-wide API key**; per-collection authorization is not first-class.
So the tenant boundary is enforced by the **application + NetworkPolicy**, not by Qdrant. A
compromised tenant-A pod could read tenant-B's collection.

> *"In production I'd close that with Qdrant's JWT collection-scoped tokens, or one Qdrant instance
> per tenant once the tenant count justified the cost."*

Optional 45-min timebox on Qdrant 1.9+ JWT scoping. Drop on resistance; document either way.

---

## Reuse map

| Copy verbatim | From |
|---|---|
| `providers.tf`, `versions.tf` (change workspace) | `vllm-serving-aks/terraform/` |
| `gpu-operator.tf` (v26.3.2 + `CONTAINERD_CONFIG` fix + tolerations) | `vllm-serving-aks/terraform/` |
| `scripts/verify.sh` structure, `scripts/gpu-nodes-scaling.sh` | `vllm-serving-aks/scripts/` |
| `kubernetes_secret` pattern | `mlops-model-lifecycle/terraform/storage.tf` |

| Copy + adapt | Change |
|---|---|
| `vllm-serving-aks/terraform/aks.tf` | **add `network_profile` + upgrade channels off**, add third pool, `os_disk_size_gb = 64` |
| `vllm-serving-aks/k8s/vllm-deployment.yaml` | swap model/flags per Block C; keep `enableServiceLinks`, toleration, startupProbe |

**Greenfield (no prior art in the repo):** all Python, Dockerfile, Ingress, cert-manager, RBAC,
ServiceAccounts, NetworkPolicies, PVCs, Qdrant, TEI, Langfuse, eval suite.

---

## Verification (end to end)

```bash
kubectl get nodes -o wide                      # gpu Ready, containerd 1.7.x
bash scripts/verify.sh                         # vLLM, TEI, Qdrant all answer
bash scripts/ingest.sh                         # idempotent, both collections populated
bash scripts/verify-isolation.sh               # all 6 proofs pass
python eval/run_eval.py                        # leakage=0, recall>=0.90, citations=1.0
curl -H "X-API-Key: $A_KEY" https://rag-a.<ip>.nip.io/query -d '{"q":"breach notification?"}'
curl -H "X-API-Key: $B_KEY" https://rag-b.<ip>.nip.io/query -d '{"q":"breach notification?"}'
# -> two different answers, PDPL Art.9 vs DIFC Art.41
```

## Teardown (run the same day)

```bash
az aks nodepool scale -g rg-sovereign-rag --cluster-name sovereign-rag --name gpu --node-count 0
cd terraform && terraform destroy    # takes PVCs + storage with it
```

`kubectl delete statefulset` does **not** delete `volumeClaimTemplates` PVCs — only
`terraform destroy` of the resource group does.

**Cost:** ~$1.07/hr all-up warm, ~$0.42/hr with GPU at zero. 14 hours ≈ **$15**.
Set an Azure budget alert at $35.
