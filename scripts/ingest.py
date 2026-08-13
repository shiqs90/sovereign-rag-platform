#!/usr/bin/env python3
"""Stage 2: push processed markdown into a tenant's collection.

Reads the artifact `corpus/extract.py` produced — never a PDF. Re-running is safe: point
IDs are derived from tenant + article number, so ingest upserts rather than duplicates.

    uv run python scripts/ingest.py corpus/processed/uae-pdpl.md \
        --law "UAE PDPL" --key key-a
"""

import argparse
import re
import sys
from pathlib import Path

import httpx

HEADING = re.compile(r"^## Article (\d+)(?:\s+—\s+(.*))?$")


def parse(markdown: Path):
    articles, current = [], None
    for line in markdown.read_text().splitlines():
        match = HEADING.match(line)
        if match:
            current = {"article": int(match.group(1)), "title": (match.group(2) or "").strip(), "text": []}
            articles.append(current)
        elif current is not None:
            current["text"].append(line)

    for article in articles:
        article["text"] = "\n".join(article["text"]).strip()
    return [a for a in articles if a["text"]]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("markdown", type=Path)
    parser.add_argument("--law", required=True, help='e.g. "UAE PDPL"')
    parser.add_argument("--key", required=True, help="tenant API key; the tenant is derived from it")
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--source-url", default="")
    args = parser.parse_args()

    articles = parse(args.markdown)
    if not articles:
        sys.exit(f"no articles parsed from {args.markdown} — check the '## Article N' headings")

    response = httpx.post(
        f"{args.url}/ingest",
        headers={"Authorization": f"Bearer {args.key}"},
        json={"law": args.law, "source_url": args.source_url, "articles": articles},
        timeout=600,
    )
    response.raise_for_status()
    print(response.json())


if __name__ == "__main__":
    main()
