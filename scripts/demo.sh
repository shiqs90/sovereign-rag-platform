#!/usr/bin/env bash
# Demo: two government tenants share one GPU but cannot see each other's data.
# Run against a deployed platform with both corpora ingested.
#
#   bash scripts/demo.sh
set -u

kubectl port-forward -n tenant-a svc/rag 18080:8080 >/dev/null 2>&1 &
kubectl port-forward -n tenant-b svc/rag 18081:8080 >/dev/null 2>&1 &
trap 'pkill -f "port-forward.*svc/rag"' EXIT
sleep 4


# ─────────────────────────────────────────────────────────────────────────────
# 1. Same question, two tenants.
#    WHY: the two corpora are different legal regimes, so a leak is VISIBLE —
#         the tenant would get the wrong law, not a subtly wrong answer.
#    EXPECT: A cites UAE PDPL Art.9 (Bureau), B cites DIFC Art.41 (Commissioner).
# ─────────────────────────────────────────────────────────────────────────────
echo "=== TENANT A — UAE PDPL ==="
curl -s -X POST localhost:18080/query -H 'Authorization: Bearer key-a' \
  -H 'Content-Type: application/json' \
  -d '{"question":"What are my breach notification obligations?"}' | python3 -m json.tool

echo "=== TENANT B — DIFC Data Protection Law ==="
curl -s -X POST localhost:18081/query -H 'Authorization: Bearer key-b' \
  -H 'Content-Type: application/json' \
  -d '{"question":"What are my breach notification obligations?"}' | python3 -m json.tool


# ─────────────────────────────────────────────────────────────────────────────
# 2. Cross-tenant trap.
#    WHY: "Commissioner" is DIFC vocabulary and appears nowhere in PDPL. If
#         semantic similarity alone could reach another corpus, this finds it.
#    EXPECT: answers from PDPL only. Any DIFC citation here is a leak.
# ─────────────────────────────────────────────────────────────────────────────
echo "=== TRAP — asking tenant A a DIFC-flavoured question ==="
curl -s -X POST localhost:18080/query -H 'Authorization: Bearer key-a' \
  -H 'Content-Type: application/json' \
  -d '{"question":"What must I report to the Commissioner about a personal data breach?"}' \
  | python3 -m json.tool


# ─────────────────────────────────────────────────────────────────────────────
# 3. Header cannot override the API key.
#    WHY: if a client-supplied header picked the collection, any authenticated
#         tenant could read any other tenant's corpus by editing one line.
#    EXPECT: HTTP 403.
# ─────────────────────────────────────────────────────────────────────────────
echo "=== SPOOF — tenant A's key claiming to be tenant B ==="
curl -s -o /dev/null -w 'HTTP %{http_code}\n' -X POST localhost:18080/query \
  -H 'Authorization: Bearer key-a' -H 'X-Tenant: tenant-b' \
  -H 'Content-Type: application/json' \
  -d '{"question":"What are my breach notification obligations?"}'


# ─────────────────────────────────────────────────────────────────────────────
# 4. The rejection is recorded.
#    WHY: a control you cannot evidence afterwards is not a control.
#    EXPECT: note="rejected X-Tenant=tenant-b", retrieved_article_ids=[] (refused
#            before any search ran), and the question stored only as a sha256.
# ─────────────────────────────────────────────────────────────────────────────
echo "=== AUDIT ==="
curl -s localhost:18080/audit -H 'Authorization: Bearer key-a' | python3 -m json.tool | tail -20


# ─────────────────────────────────────────────────────────────────────────────
# 5. Control plane: RBAC.
#    WHY: the app could be compromised; Kubernetes must deny it independently.
#    EXPECT: no / yes / no.  list is denied because resourceNames cannot scope
#            list — granting it would return every secret in the namespace.
# ─────────────────────────────────────────────────────────────────────────────
echo "=== RBAC ==="
echo -n "tenant-a SA -> tenant-b secrets:  "
kubectl auth can-i get secrets -n tenant-b --as=system:serviceaccount:tenant-a:rag-sa
echo -n "tenant-a SA -> its own secret:    "
kubectl auth can-i get secret/rag-api-keys -n tenant-a --as=system:serviceaccount:tenant-a:rag-sa
# Get on the one named Secret rag-api-keys in tenant-a, and nothing else: no other secret, no listing, no other namespace.
echo -n "tenant-a SA -> LIST secrets:      "
kubectl auth can-i list secrets -n tenant-a --as=system:serviceaccount:tenant-a:rag-sa


# ─────────────────────────────────────────────────────────────────────────────
# 6. Each pod holds only its own key.
#    WHY: so a pod cannot authenticate the other tenant even if handed its key.
#         Isolation does not rest on the Qdrant filter alone.
#    EXPECT: one key each, not both.
# ─────────────────────────────────────────────────────────────────────────────
echo "=== CREDENTIALS ==="
echo -n "tenant-a pod: "; kubectl exec -n tenant-a deploy/rag -- printenv API_KEYS
echo -n "tenant-b pod: "; kubectl exec -n tenant-b deploy/rag -- printenv API_KEYS


# ─────────────────────────────────────────────────────────────────────────────
# 7. Network: tenant-a cannot reach tenant-b's service.
#    WHY: third independent layer — holds even if the app and RBAC both fail.
#    EXPECT: a timeout. Requires k8s/50-networkpolicies.yaml to be applied, and
#            only enforced because the cluster was built with network_policy=calico.
# ─────────────────────────────────────────────────────────────────────────────
echo "=== NETWORK ==="
kubectl exec -n tenant-a deploy/rag -- \
  python -c 'import httpx
try:    httpx.get("http://rag.tenant-b:8080/healthz", timeout=5); print("REACHED — LEAK")
except Exception as e: print(type(e).__name__, "— blocked")'
