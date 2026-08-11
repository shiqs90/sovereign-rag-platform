#!/usr/bin/env python3
"""Stage 1 of ingestion: PDF -> structured markdown.

Run once, commit the output. Ingest reads processed/*.md and never touches a PDF —
parsing is slow and non-deterministic, so the extracted form is the contract.

    python3 extract.py raw/uae-pdpl.pdf processed/uae-pdpl.md

A PDF stores positioned glyphs, not a document tree. The whole job here is recovering
`Article N`, because chunk boundaries, citations and the article filter all depend on it.
"""

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

# NOT `import fitz` — an unrelated squatter package on PyPI owns that name and shadows
# PyMuPDF's own `fitz` module. PyMuPDF >= 1.24 exposes `pymupdf` directly.
import pymupdf

# Two source documents, two heading conventions — so the pattern is per-document, chosen
# explicitly with --format. No auto-detection: a silently mismatched pattern yields missing
# articles, and failing loudly beats being clever.
#
#   article   UAE PDPL      "Article (9)" / "Article 9 —"
#   numbered  DIFC DP 2020  bare "6." alone on a line, title on the next line
#                           (sub-clauses are parenthesised "(1)" / "(a)", so they don't collide)
FORMATS = {
    "article": re.compile(r"^\s*Article\s*\(?(\d+)\)?\s*[-–—.:]?\s*(.*)$", re.IGNORECASE),
    "numbered": re.compile(r"^\s*(\d{1,3})\.\s*()$"),
}

# Page furniture: bare page numbers, and lines that are only punctuation/whitespace.
NOISE_RE = re.compile(r"^\s*(\d{1,3}|[-–—_.\s]*)\s*$")


def read_lines(pdf_path):
    """Flatten the PDF to a list of text lines, dropping page furniture.

    Two kinds of furniture: bare page numbers (NOISE_RE), and running headers/footers —
    e.g. "DATA PROTECTION LAW" printed on every page. The latter is detected rather than
    hardcoded: a line appearing on most pages is furniture, not content. Left in, it lands
    in every chunk and adds the same constant component to every embedding.
    """
    doc = pymupdf.open(pdf_path)
    pages = [
        [ln.strip() for ln in page.get_text().splitlines() if ln.strip() and not NOISE_RE.match(ln)]
        for page in doc
    ]

    # Length guard is load-bearing: sub-clause markers like "(a)" sit alone on a line and
    # recur on most pages, so frequency alone would classify them as furniture and silently
    # delete real content. Running headers are substantive strings; markers are not.
    repeats = Counter(line for page in pages for line in set(page))
    furniture = (
        {line for line, n in repeats.items() if n > len(pages) * 0.5 and len(line) > 10}
        if len(pages) > 5
        else set()
    )

    return [line for page in pages for line in page if line not in furniture]


def split_articles(lines, heading_re):
    """-> [(number, title, [body lines])], in document order."""
    articles = []
    current = None
    title_consumed = False

    for i, line in enumerate(lines):
        if title_consumed:  # this line was taken as the previous heading's title
            title_consumed = False
            continue

        match = heading_re.match(line)
        if match:
            number = int(match.group(1))
            title = match.group(2).strip()
            # Title commonly sits on the next line in legal PDFs.
            if not title and i + 1 < len(lines) and not heading_re.match(lines[i + 1]):
                title = lines[i + 1].strip()
                title_consumed = True
            current = (number, title, [])
            articles.append(current)
        elif current is not None:
            current[2].append(line)

    return articles


def dedupe(articles):
    """Drop table-of-contents entries.

    A TOC lists every article number with no body (the trailing page number is stripped as
    noise); the real article follows later with paragraphs under it. So for each number, keep
    the occurrence carrying the most body — and return them in numeric order.
    """
    best = {}
    for number, title, body in articles:
        if number not in best or len(body) > len(best[number][2]):
            best[number] = (number, title, body)
    return [best[number] for number in sorted(best)]


def report_gaps(articles, out=sys.stderr):
    """Missing numbers mean silent extraction failures — the check that matters."""
    numbers = sorted({n for n, _, _ in articles})
    if not numbers:
        print("ERROR: no articles matched. Check the heading format.", file=out)
        return False

    expected = set(range(min(numbers), max(numbers) + 1))
    missing = sorted(expected - set(numbers))
    duplicates = sorted({n for n in numbers if [a[0] for a in articles].count(n) > 1})

    print(f"articles extracted: {len(articles)}  range: {min(numbers)}-{max(numbers)}", file=out)
    if missing:
        print(f"WARNING missing article numbers: {missing}", file=out)
    if duplicates:
        print(f"WARNING duplicate article numbers: {duplicates}", file=out)
    return not missing


def to_markdown(articles, source_url):
    out = []
    if source_url:
        out.append(f"<!-- source: {source_url} -->\n")
    for number, title, body in articles:
        heading = f"## Article {number}"
        if title:
            heading += f" — {title}"
        out.append(heading)
        out.append("")
        out.extend(body)
        out.append("")
    return "\n".join(out)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("markdown", type=Path)
    parser.add_argument(
        "--format",
        choices=sorted(FORMATS),
        required=True,
        help="heading convention of THIS document (article=UAE PDPL, numbered=DIFC)",
    )
    parser.add_argument("--source-url", default="", help="recorded as a comment for citations")
    args = parser.parse_args()

    lines = read_lines(args.pdf)
    articles = dedupe(split_articles(lines, FORMATS[args.format]))
    complete = report_gaps(articles)

    args.markdown.parent.mkdir(parents=True, exist_ok=True)
    args.markdown.write_text(to_markdown(articles, args.source_url))
    print(f"wrote {args.markdown}", file=sys.stderr)

    # Non-zero on gaps so this is usable as a pipeline step, not just a script.
    sys.exit(0 if complete else 1)


if __name__ == "__main__":
    main()
