#!/usr/bin/env python3
"""Smoke-test CLI for the sarathi copilot API.

Usage:
    python scripts/ask.py "which robots are low on battery?"
    python scripts/ask.py "history for R-004 over the last 15 minutes" \
        --url http://localhost:8001

Sends POST {url}/ask and prints the answer, tier, latency and evidence.
Exit codes: 0 ok, 1 HTTP/connection error, 2 bad usage.
"""
from __future__ import annotations

import argparse
import json
import sys

import httpx

DEFAULT_URL = "http://localhost:8001"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Ask the sarathi fleet copilot a question."
    )
    parser.add_argument("question", help="natural-language question to ask")
    parser.add_argument(
        "--url", default=DEFAULT_URL,
        help=f"base URL of the sarathi service (default {DEFAULT_URL})",
    )
    parser.add_argument(
        "--timeout", type=float, default=60.0,
        help="request timeout in seconds (default 60)",
    )
    parser.add_argument(
        "--json", action="store_true", dest="raw_json",
        help="print the raw JSON response instead of formatted text",
    )
    args = parser.parse_args(argv)

    try:
        resp = httpx.post(
            f"{args.url.rstrip('/')}/ask",
            json={"question": args.question},
            timeout=args.timeout,
        )
    except httpx.HTTPError as exc:
        print(f"error: could not reach {args.url}: {exc}", file=sys.stderr)
        return 1
    if resp.status_code >= 400:
        print(f"error: HTTP {resp.status_code}: {resp.text[:500]}", file=sys.stderr)
        return 1

    body = resp.json()
    if args.raw_json:
        print(json.dumps(body, indent=2))
        return 0

    print(body.get("answer", ""))
    print()
    print(f"tier: {body.get('tier')}   latency: {body.get('latency_ms')} ms")
    evidence = body.get("evidence") or []
    if evidence:
        print("evidence:")
        for ev in evidence:
            print(f"  - {ev.get('label')}  [{ev.get('ref')}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
