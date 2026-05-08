#!/usr/bin/env python3
"""
scripts/check_dhan_api.py

Quick test harness to validate DhanHQ credentials and basic endpoints.
Tries multiple header styles (Bearer, access-token, api-key+secret, Basic) and
reports responses so you can debug whether the provided keys/tokens are accepted.

Usage:
  python scripts/check_dhan_api.py [--base BASE_URL] [--client-id CID] [--access-token AT] [--api-key KEY] [--api-secret SECRET]

Run inside the container (recommended):
  docker exec -i -e PYTHONPATH=/app <CID> python3 scripts/check_dhan_api.py

"""
from __future__ import annotations

import argparse
import base64
import json
import os
import textwrap
import requests
from typing import Dict, List, Tuple


def make_req(url: str, headers: Dict[str, str]) -> Tuple[int | None, str]:
    try:
        r = requests.get(url, headers=headers, timeout=10)
        body = r.text
        return r.status_code, body
    except Exception as exc:
        return None, str(exc)


def build_header_variants(client_id: str | None, access_token: str | None) -> List[Dict[str, str]]:
    variants: List[Dict[str, str]] = []

    if access_token:
        variants.append({'Authorization': f'Bearer {access_token}'})
        variants.append({'access-token': access_token})
        variants.append({'x-access-token': access_token})
        if client_id:
            variants.append({'client-id': client_id, 'access-token': access_token})

    # Add an empty headers attempt to see if public endpoints exist
    variants.append({})

    # Deduplicate while preserving order
    seen = set()
    out: List[Dict[str, str]] = []
    for h in variants:
        key = tuple(sorted(h.items()))
        if key in seen:
            continue
        seen.add(key)
        out.append(h)
    return out


def summarise_headers(h: Dict[str, str]) -> str:
    if not h:
        return '(none)'
    return ', '.join(f'{k}={v if len(v)<20 else v[:17]+"..."}' for k, v in h.items())


def run_tests(base: str, client_id: str | None, access_token: str | None, endpoints: List[str]) -> None:
    headers_variants = build_header_variants(client_id, access_token)
    print('Base URL:', base)
    print('Client ID present:', bool(client_id))
    print('Access token present:', bool(access_token))
    print()

    results = []
    for ep in endpoints:
        # join base and endpoint robustly to avoid duplicated segments like /v2/v2
        url = base.rstrip('/') + '/' + ep.lstrip('/')
        for h in headers_variants:
            code, body = make_req(url, h)
            results.append((ep, h, code, body))

    # Print a concise table
    for ep, h, code, body in results:
        print('---')
        print('Endpoint:', ep)
        print('Headers :', summarise_headers(h))
        print('Status  :', code)
        if body:
            snippet = body if len(body) < 2000 else body[:2000] + '\n...'
            print('Body    :')
            print(textwrap.indent(snippet, '  '))
        else:
            print('Body    : (empty)')


def parse_args():
    p = argparse.ArgumentParser(description='Check DhanHQ API auth & profile endpoints with multiple header variants')
    p.add_argument('--base', help='Base Dhan API URL', default=os.environ.get('DHAN_BASE_URL', 'https://api.dhan.co/v2'))
    p.add_argument('--client-id', help='Dhan client id', default=os.environ.get('DHAN_CLIENT_ID'))
    p.add_argument('--access-token', help='Dhan access token', default=os.environ.get('DHAN_ACCESS_TOKEN'))
    p.add_argument('--endpoints', help='Comma separated endpoints to test (prefix with /)', default='/v2/profile,/profile,/v2/fundlimit,/v2/holdings')
    return p.parse_args()


def main():
    args = parse_args()
    endpoints = [e.strip() for e in args.endpoints.split(',') if e.strip()]
    run_tests(args.base, args.client_id, args.access_token, endpoints)


if __name__ == '__main__':
    main()
