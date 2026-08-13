# Instance Math

Sizing rationale for every node pool and the GPU memory budget. Every number here was
checked against regional quota before `terraform apply`.

Region: **australiacentral** (the only region with approved T4 quota).

---

## 1. Quota — two independent ceilings

Azure enforces a **per-family** limit and a **total regional** limit. Both must pass; a
request is denied by whichever binds first. A GPU vCPU counts against both — it is not billed
twice, it just has to satisfy two constraints.

Available quota in this region:

```bash
az vm list-usage --location australiacentral -o table \
  | grep -Ei "Total Regional|Standard DSv3|NCASv3_T4"
```

```
Total Regional Low-priority vCPUs         0    3
Total Regional vCPUs                      0    14
Standard DSv3 Family vCPUs                0    10
Standard NCASv3_T4 Family vCPUs           0    4
```

| Quota | Limit | This platform uses | Free after apply |
|---|---|---|---|
| Standard DSv3 Family vCPUs | 10 | 6 (system 2 + apps 4) | 4 |
| Standard NCASv3_T4 Family vCPUs | 4 | 4 (gpu) | **0** |
| **Total Regional vCPUs** | **14** | **10** | **4** |
| Total Regional Low-priority vCPUs | 3 | 0 | 3 |

The platform consumes 71% of total regional capacity and 100% of the T4 family. Re-run the
command before any apply — limits are subscription-level and another running cluster
invalidates the plan.

### What this implies

**The GPU pool has no retry margin.** `NCASv3_T4 = 4` is exactly one `NC4as_T4_v3`. A second
node cannot exist even transiently, so a bad node must be deleted before it is recreated.

**Spot/low-priority is unusable.** The low-priority ceiling is 3 vCPU; the smallest T4 node
needs 4. Regular priority only, no exceptions available.

**The 4 spare regional vCPU are a surge buffer, not capacity.** They exist so that exactly one
pool can perform a rolling upgrade (`max_surge = "1"`) at a time. Consuming them with a real
workload removes the ability to upgrade any pool.

**Therefore both AKS upgrade channels are disabled** in `terraform/aks.tf`:

```hcl
automatic_upgrade_channel = "none"
node_os_upgrade_channel   = "None"
```

AKS's default node-image channel performs a *surge* upgrade — it adds a node before draining the
old one, requiring 2× the pool's vCPU. On the GPU pool that needs 8 vCPU against a family limit
of 4 and fails with `ErrCode_InsufficientVCPUQuota`. Left enabled, this fires unattended.

| Attempted action | Blocked by |
|---|---|
| Second GPU node | `NCASv3_T4` (4/4) |
| Apps pool → 2 nodes | DSv3 (would be 10/10) **and** regional (14/14) |
| Concurrent surge on two pools | Total regional (14/14) |

---

## 2. Node pools

| Pool | VM | vCPU / RAM | Allocatable | Runs |
|---|---|---|---|---|
| `system` | `Standard_D2s_v3` | 2 / 8 GiB | ~1.9 / ~5.6 GiB | kube-system, ingress-nginx, cert-manager, GPU Operator controller |
| `apps` | `Standard_D4s_v3` | 4 / 16 GiB | ~3.86 / ~12.6 GiB | Qdrant, TEI, rag-service ×2, Langfuse, Postgres |
| `gpu` | `Standard_NC4as_T4_v3` | 4 / 28 GiB, 1× T4 16 GB | — | vLLM only |

"Allocatable" is what remains after AKS reserves memory and CPU for the kubelet and system
daemons. Roughly 25% of the first 4 GiB of RAM, then a sliding scale — always size against
allocatable, never against the VM spec sheet.

DSv3 is used because it is the family with **verified quota in this region**. DASv5 and DSv5
both report a limit of 0 here. Sizes verified in other regions do not transfer.

---

## 3. Apps pool — why 4 vCPU

One component dominates the footprint: TEI running `bge-m3` on CPU.

| Pod | RAM | Note |
|---|---|---|
| TEI (`bge-m3`, CPU) | **~2.5–3 GiB** | the driver — 568M params, fp32 on CPU |
| Langfuse v2 (Node) | ~0.8–1 GiB | |
| Postgres | ~0.5 GiB | Langfuse metadata + audit table |
| Qdrant | ~1 GiB | corpus is tiny; mostly process + HNSW overhead |
| rag-service × 2 | ~0.6 GiB | FastAPI, one per tenant namespace |
| kube-system daemonsets | ~0.5 GiB | kube-proxy, Calico, CSI drivers |
| **Total** | **~6.5–7 GiB** | of ~12.6 GiB allocatable |

### Rejected alternatives

**`D2s_v3` (2 vCPU / 8 GiB)** — allocatable is ~5.6 GiB. TEI alone consumes over half of it and
the total (~6.5 GiB) exceeds it outright. Result would be evictions or an OOMKill during ingest,
misattributed to Qdrant.

**Two × `D2s_v3` instead of one `D4s_v3`** — identical vCPU count, identical quota cost, strictly
worse. The ~0.5 GiB daemonset tax is paid twice, and memory is split into two pools where TEI's
3 GiB still has to fit inside a single 5.6 GiB node. Bin-packing defeats it.

**`D8s_v3` (8 vCPU)** — pushes DSv3 to 10/10 and regional to 14/14, eliminating the surge buffer,
at double the cost for headroom nothing uses.

`D4s_v3` is the smallest size that holds the workload.

### The lever, if the pool must shrink

`bge-m3` is chosen for multilingual coverage. Substituting `BAAI/bge-small-en-v1.5` (~33M params,
~0.4 GiB) drops the total to ~4 GiB, which fits a `D2s_v3` and frees 2 regional vCPU. The corpus
is English, so this is a genuine option — it costs multilingual capability, not correctness.

---

## 4. Apps pool is deliberately not tainted

The GPU pool carries `nvidia.com/gpu=present:NoSchedule` because it guards a scarce, expensive
device. The apps pool guards nothing — a taint there protects no resource and creates a
`Pending`-forever failure mode the moment a platform pod lacks the toleration.

Placement is steered from the workload side instead, with `nodeSelector: {pool: apps}`.

---

## 5. GPU memory budget

Hardware: **NVIDIA T4, 16 GB, Turing (SM 7.5)**.

Architecture constraints that precede any quality discussion:

| Feature | T4 | Consequence |
|---|---|---|
| bf16 | ✗ | must run `--dtype=float16` |
| Marlin kernels | ✗ (needs SM 8.0+) | `--quantization=awq`, **not** `awq_marlin` |
| FP8 | ✗ | no FP8 weights or FP8 KV cache |

### Budget at `--gpu-memory-utilization=0.90`

| Item | Size |
|---|---|
| Total VRAM | 16.0 GiB |
| Usable at util 0.90 | ~14.4 GiB |
| `Qwen2.5-7B-Instruct-AWQ` weights (4-bit) | ~5.6 GiB |
| Activations + framework overhead (`--enforce-eager`, no CUDA graphs) | ~1.0 GiB |
| **Remaining for KV cache** | **~7.5 GiB** |

### KV cache capacity

Qwen2.5-7B: 28 layers, 4 KV heads (GQA), head_dim 128, fp16.

```
bytes/token = 2 (K and V) × 28 layers × 4 kv_heads × 128 head_dim × 2 bytes
            = 57,344 bytes ≈ 56 KiB per token
```

```
7.5 GiB / 56 KiB ≈ 140,000 tokens of KV cache
```

At `--max-model-len=8192`, that is ~17 sequences at full context length. The configured
`--max-num-seqs=8` sits well inside the budget, so the KV cache is not the binding constraint —
throughput is.

Grouped-query attention is why this works: with 28 attention heads but only 4 KV heads, the KV
cache is 7× smaller than multi-head attention would require. Without GQA this model would not
serve 8K context on a 16 GB card.

---

## 6. Why embeddings do not share the GPU

The arithmetic permits it — `bge-m3` needs ~3 GiB and ~7.5 GiB of KV headroom exists. It is
still the wrong call.

Two pods cannot both claim `nvidia.com/gpu: 1`; sharing requires time-slicing, which provides
**scheduling isolation with zero memory isolation**. A single OOM would take down inference and
embedding together — the entire platform, for both tenants, from one bad batch.

Embedding is a one-time batch cost at ingest, not on the serving hot path. CPU is the correct
placement, and the GPU stays dedicated to the latency-sensitive workload.

---

## 7. Cost

Approximate australiacentral on-demand rates; confirm against the current price sheet.

| Resource | $/hr |
|---|---|
| `NC4as_T4_v3` (gpu) | ~0.60 |
| `D4s_v3` (apps) | ~0.23 |
| `D2s_v3` (system) | ~0.12 |
| Managed disks + LoadBalancer | ~0.03 |
| **Warm total** | **~0.98–1.10** |
| **GPU pool scaled to 0** | **~0.38–0.42** |

**Budget is $10 per project.** At ~$1.00/hr warm that is **ten hours of cluster time in
total**, model downloads included. This platform overran it (~38h ≈ $38), almost entirely
while components were broken but still billing.

The GPU is ~60% of the burn. Scale it to zero between working sessions:

```bash
az aks nodepool scale -g rg-sovereign-rag --cluster-name sovereign-rag --name gpu --node-count 0
```

Scaling to zero keeps the cluster, PVCs and all non-GPU workloads intact. Scaling back up
re-pulls the vLLM image and re-downloads the model — budget 10–15 minutes before it serves.

Full teardown:

```bash
cd terraform && terraform destroy
```

`kubectl delete statefulset` does **not** remove `volumeClaimTemplates` PVCs. Only destroying
the resource group reclaims them.
