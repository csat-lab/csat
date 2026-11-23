#!/usr/bin/env python3
"""Parse one PDF with Upstage Document Parse (Enhanced, markdown).

Repository port of the verified Muse helper
(~/workspace/custom-connectors/custom.upstage/bin/upstage.py), but
self-contained and stdlib-only: the API key comes from the environment
(UPSTAGE_API_KEY), never from a file or an argument, so the same script
runs on a developer machine and in GitHub Actions (secret
UPSTAGE_API_KEY).

    python3 upstage_parse.py --file problem.pdf \
        --out-md work/2026_10_H3/upstage.md \
        --out-json work/2026_10_H3/upstage.response.json

Notes (verified 2026-11-23 against the live API):
- Send exactly one output format per call. Asking for html, markdown
  and text together returns only html with markdown and text empty.
  Markdown alone returns populated markdown.
- Enhanced (mode=enhanced) keeps underlines and extracts full chart
  tables; Standard drops underlines and most of a bar chart. Use
  Enhanced for exam pages.
- Pricing: Standard $0.01/page, Enhanced $0.03/page, excl. 10% VAT
  (official pricing page, checked 2026-11-23). An 8-page exam is
  $0.24 in Enhanced.
- The call is synchronous; the provider allows up to five minutes per
  request. Retries on 429/503 only.
"""

import argparse
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

API_URL = "https://api.upstage.ai/v1/document-ai/document-parse"
DEFAULT_MODE = "enhanced"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code,
                                     "redirect refused: %s" % newurl,
                                     headers, fp)


def encode_multipart(fields, files):
    """fields: {name: str}, files: {name: (filename, bytes, mime)}."""
    boundary = uuid.uuid4().hex
    body = bytearray()
    for name, value in fields.items():
        body += ("--%s\r\n" % boundary).encode()
        body += ('Content-Disposition: form-data; name="%s"\r\n\r\n' % name).encode()
        body += value.encode()
        body += b"\r\n"
    for name, (filename, data, mime) in files.items():
        body += ("--%s\r\n" % boundary).encode()
        body += ('Content-Disposition: form-data; name="%s"; filename="%s"\r\n'
                 % (name, filename)).encode()
        body += ("Content-Type: %s\r\n\r\n" % mime).encode()
        body += data
        body += b"\r\n"
    body += ("--%s--\r\n" % boundary).encode()
    return bytes(body), boundary


def parse_document(pdf_path, api_key, mode=DEFAULT_MODE):
    """POST the PDF; return the parsed response dict."""
    pdf_path = Path(pdf_path)
    mime = mimetypes.guess_type(pdf_path.name)[0] or "application/pdf"
    fields = {
        "output_formats": json.dumps(["markdown"]),
        "base64_encoding": json.dumps(["table", "figure", "chart"]),
        "mode": mode,
    }
    files = {"document": (pdf_path.name, pdf_path.read_bytes(), mime)}
    body, boundary = encode_multipart(fields, files)
    req = urllib.request.Request(API_URL, data=body, method="POST")
    req.add_header("Authorization", "Bearer %s" % api_key)
    req.add_header("Content-Type",
                   "multipart/form-data; boundary=%s" % boundary)
    opener = urllib.request.build_opener(NoRedirect)
    last_err = None
    for attempt in range(3):
        try:
            with opener.open(req, timeout=300) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in (429, 503) and attempt < 2:
                time.sleep(10 * (attempt + 1))
                continue
            detail = e.read().decode(errors="ignore")[:500] if e.fp else ""
            raise RuntimeError("Upstage API HTTP %s: %s" % (e.code, detail))
        except Exception as e:  # timeout / connection error: no retry
            raise RuntimeError("Upstage API request failed: %s" % e)
    raise RuntimeError("Upstage API request failed: %s" % last_err)


def main():
    ap = argparse.ArgumentParser(description="Parse one PDF with Upstage "
                                 "Document Parse (Enhanced, markdown).")
    ap.add_argument("--file", required=True, help="PDF to parse")
    ap.add_argument("--out-md", required=True,
                    help="where to write the extracted markdown")
    ap.add_argument("--out-json", default=None,
                    help="where to write the full response JSON")
    ap.add_argument("--mode", default=DEFAULT_MODE,
                    choices=["standard", "enhanced", "auto"])
    ap.add_argument("--api-key-env", default="UPSTAGE_API_KEY",
                    help="env var holding the API key")
    args = ap.parse_args()

    api_key = os.environ.get(args.api_key_env)
    if not api_key:
        print("error: %s is not set; set it to your Upstage API key "
              "(Upstage console > API keys) and retry." % args.api_key_env,
              file=sys.stderr)
        return 2

    try:
        resp = parse_document(args.file, api_key, mode=args.mode)
    except RuntimeError as e:
        print("error: %s" % e, file=sys.stderr)
        return 1

    content = resp.get("content") or {}
    md = content.get("markdown") or ""
    if not md:
        print("error: Upstage returned no markdown "
              "(model %s, usage %s); response saved to %s"
              % (resp.get("model"), resp.get("usage"), args.out_json),
              file=sys.stderr)
        if args.out_json:
            Path(args.out_json).write_text(
                json.dumps(resp, ensure_ascii=False, indent=2),
                encoding="utf-8")
        return 1

    out_md = Path(args.out_md)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(md, encoding="utf-8")
    if args.out_json:
        out_json = Path(args.out_json)
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(resp, ensure_ascii=False, indent=2),
                            encoding="utf-8")
    pages = (resp.get("usage") or {}).get("pages")
    print("upstage: %s pages=%s markdown=%d chars -> %s"
          % (Path(args.file).name, pages, len(md), out_md))
    return 0


if __name__ == "__main__":
    sys.exit(main())
