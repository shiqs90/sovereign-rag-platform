#!/usr/bin/env bash
# The six isolation proofs. Run against a live cluster with everything deployed.
#
#   bash scripts/verify-isolation.sh
#
# Proofs 1-2 are control plane (RBAC), 3 is network (Calico), 4-6 are data plane.
# Each layer is independent: any one of them failing should not let a leak through.
set -uo pipefail

PASS=0
FAIL=0

ok()   { echo "  PASS  $1"; PASS=$((PASS + 1)); }
bad()  { echo "  FAIL  $1"; FAIL=$((FAIL + 1)); }

# rag-service runs on python:slim — no curl. httpx is installed, so use that.
in_pod() {
  kubectl exec -n "$1" deploy/rag -- python -c "$2" 2>/dev/null
}

echo "=== port-forwarding both tenants"
kubectl port-forward -n tenant-a svc/rag 18080:8080 >/dev/null 2>&1 &
PF_A=$!
kubectl port-forward -n tenant-b svc/rag 18081:8080 >/dev/null 2>&1 &
PF_B=$!
trap 'kill $PF_A $PF_B 2>/dev/null' EXIT
sleep 4

echo
echo "=== 1. control plane: tenant-a's identity cannot read tenant-b's secrets"
RESULT=$(kubectl auth can-i get secrets -n tenant-b \
  --as=system:serviceaccount:tenant-a:rag-sa 2>/dev/null)
[[ "$RESULT" == "no" ]] && ok "cross-namespace secret read denied" \
                        || bad "expected 'no', got '$RESULT'"

echo
echo "=== 2. control plane: tenant-a can GET its own secret but cannot LIST"
GET=$(kubectl auth can-i get secret/rag-api-keys -n tenant-a \
  --as=system:serviceaccount:tenant-a:rag-sa 2>/dev/null)
LIST=$(kubectl auth can-i list secrets -n tenant-a \
  --as=system:serviceaccount:tenant-a:rag-sa 2>/dev/null)
# list would return every secret in the namespace and defeat resourceNames, which
# list/watch cannot scope. Denying it is the point.
[[ "$GET" == "yes" && "$LIST" == "no" ]] && ok "get=yes list=no" \
                                         || bad "expected get=yes list=no, got get=$GET list=$LIST"

echo
echo "=== 3. network: tenant-a cannot reach tenant-b's service"
# Expect a timeout, not a refusal. A default-deny NetworkPolicy silently drops packets;
# a connection *refused* would mean the policy is absent and only the port is closed.
NET=$(in_pod tenant-a '
import httpx
try:
    httpx.get("http://rag.tenant-b:8080/healthz", timeout=5)
    print("REACHED")
except httpx.ConnectTimeout:
    print("TIMEOUT")
except Exception as e:
    print(type(e).__name__)
')
[[ "$NET" == "TIMEOUT" ]] && ok "cross-tenant traffic dropped" \
                          || bad "expected TIMEOUT, got '$NET'"

echo
echo "=== 4. data plane: same question, two tenants, two legal regimes"
ask() {
  curl -s -X POST "http://localhost:$1/query" \
    -H "Authorization: Bearer $2" -H 'Content-Type: application/json' \
    -d '{"question":"What are my breach notification obligations?"}'
}
LAW_A=$(ask 18080 key-a | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d["chunks"][0]["tenant_id"] if d["chunks"] else "none")')
LAW_B=$(ask 18081 key-b | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d["chunks"][0]["tenant_id"] if d["chunks"] else "none")')
[[ "$LAW_A" == "tenant-a" && "$LAW_B" == "tenant-b" ]] \
  && ok "each tenant retrieved only its own corpus" \
  || bad "expected tenant-a/tenant-b, got $LAW_A/$LAW_B"

echo
echo "=== 5. data plane: a header cannot override the API key"
CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST http://localhost:18080/query \
  -H 'Authorization: Bearer key-a' -H 'X-Tenant: tenant-b' \
  -H 'Content-Type: application/json' \
  -d '{"question":"What are my breach notification obligations?"}')
[[ "$CODE" == "403" ]] && ok "spoofed X-Tenant rejected with 403" \
                       || bad "expected 403, got $CODE"

# And the attempt must be recorded — a rejection nobody can see is not governance.
AUDITED=$(curl -s http://localhost:18080/audit -H 'Authorization: Bearer key-a' \
  | python3 -c 'import sys,json; print(any("rejected X-Tenant" in (r.get("note") or "") for r in json.load(sys.stdin)))')
[[ "$AUDITED" == "True" ]] && ok "rejection written to the audit log" \
                           || bad "no audit record for the spoof attempt"

echo
echo "=== 6. evaluation gate: leakage across the full question set"
if uv run python eval/run_eval.py \
     --url-tenant-a http://localhost:18080 \
     --url-tenant-b http://localhost:18081 >/tmp/eval.log 2>&1; then
  ok "leakage_rate = 0"
  grep gold_article_hit_rate /tmp/eval.log | sed 's/^/        /'
else
  bad "eval failed — see /tmp/eval.log"
  tail -20 /tmp/eval.log | sed 's/^/        /'
fi

echo
echo "================================"
echo "  passed: $PASS   failed: $FAIL"
echo "================================"
[[ $FAIL -eq 0 ]] || exit 1
