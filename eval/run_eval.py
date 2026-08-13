#!/usr/bin/env python3
"""Run the evaluation set. Two metrics, deliberately asymmetric.

    leakage_rate           GATE     — exit 1 if anything crossed a tenant boundary
    gold_article_hit_rate  REPORTED — sanity that retrieval works; never gates

Leakage is checked three ways, because one mechanism failing silently is the risk:
  1. every retrieved chunk's tenant_id equals the caller's
  2. no citation resolves to the other tenant's law
  3. no forbidden term (the other law's vocabulary) appears in the answer

    uv run python eval/run_eval.py --url http://localhost:8000
"""

import argparse
import sys
from pathlib import Path

import httpx
import yaml

KEYS = {"tenant-a": "key-a", "tenant-b": "key-b"}


def run(urls, questions):
    leaks, hits, gold_total = [], 0, 0

    for item in questions:
        tenant = item["tenant"]
        response = httpx.post(
            f"{urls[tenant]}/query",
            headers={"Authorization": f"Bearer {KEYS[tenant]}"},
            json={"question": item["question"], "k": 5},
            timeout=300,
        )
        response.raise_for_status()
        body = response.json()

        # 1. chunk-level tenant ownership
        foreign = [c for c in body["chunks"] if c["tenant_id"] != tenant]
        if foreign:
            leaks.append((item["id"], f"chunks from {sorted({c['tenant_id'] for c in foreign})}"))

        # 2. the other law's vocabulary in the answer
        answer = (body.get("answer") or "")
        for term in item.get("forbidden_terms", []):
            if term.lower() in answer.lower():
                leaks.append((item["id"], f"forbidden term in answer: {term!r}"))

        # 3. retrieval sanity — reported, not gated
        if item.get("gold_article") is not None:
            gold_total += 1
            if item["gold_article"] in [c["article"] for c in body["chunks"]]:
                hits += 1

        status = "REFUSED" if body["refused"] else f"{len(body['chunks'])} chunks"
        print(f"  {item['id']:<34} {tenant}  {status}")

    return leaks, hits, gold_total


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    # Locally one process serves both tenants. On the cluster each tenant has its own
    # Deployment holding only its own key, so the endpoints differ.
    parser.add_argument("--url", default="http://localhost:8000", help="serves all tenants")
    parser.add_argument("--url-tenant-a", help="overrides --url for tenant-a")
    parser.add_argument("--url-tenant-b", help="overrides --url for tenant-b")
    parser.add_argument("--questions", type=Path, default=Path("eval/questions.yaml"))
    args = parser.parse_args()

    urls = {
        "tenant-a": args.url_tenant_a or args.url,
        "tenant-b": args.url_tenant_b or args.url,
    }

    questions = yaml.safe_load(args.questions.read_text())["questions"]
    print(f"running {len(questions)} questions")
    for tenant, url in urls.items():
        print(f"  {tenant} -> {url}")
    print()
    leaks, hits, gold_total = run(urls, questions)

    print(f"\ngold_article_hit_rate  {hits}/{gold_total}   (reported, not gated)")
    print(f"leakage_rate           {len(leaks)}          (gate: must be 0)")

    if leaks:
        print("\nLEAKS:")
        for question_id, detail in leaks:
            print(f"  {question_id}: {detail}")
        sys.exit(1)

    print("\nPASS — no tenant boundary crossed.")


if __name__ == "__main__":
    main()
