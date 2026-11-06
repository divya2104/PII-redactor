"""Command line entry point: pii-redact <files/dirs> -o <out_dir>"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from .client import LanguagePiiClient, OfflineViolation
from .detector import DetectorOptions, PiiDetector
from .docx_redactor import redact_docx
from .pdf_redactor import redact_pdf

SUPPORTED = {".pdf", ".docx"}


def load_dotenv(path: Path = Path(".env")) -> None:
    """Minimal .env loader (KEY=VALUE lines); existing env vars win."""
    for candidate in (path, Path(__file__).resolve().parent.parent / ".env"):
        if candidate.is_file():
            for line in candidate.read_text(encoding="utf-8-sig").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
            return


def _iter_inputs(paths):
    for p in map(Path, paths):
        if p.is_dir():
            yield from sorted(f for f in p.rglob("*") if f.suffix.lower() in SUPPORTED
                              and not f.name.startswith("~$"))
        elif p.suffix.lower() in SUPPORTED:
            yield p
        elif p.suffix.lower() == ".doc":
            logging.warning("%s: legacy .doc not supported - convert to .docx first "
                            "(e.g. soffice --headless --convert-to docx)", p)
        else:
            logging.warning("skipping unsupported file %s", p)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="pii-redact",
        description="Redact PII from PDF/DOCX using a local Azure AI Language PII container.")
    ap.add_argument("inputs", nargs="+", help="files or folders (.pdf, .docx)")
    ap.add_argument("-o", "--output", default="redacted", help="output folder")
    ap.add_argument("--cloud", action="store_true",
                    help="no container: call the Azure Language cloud API directly using "
                         "AZURE_LANGUAGE_ENDPOINT / AZURE_LANGUAGE_KEY (text is sent to Azure)")
    ap.add_argument("--endpoint", default=None,
                    help="default: PII_ENDPOINT or http://localhost:5000 (cloud: AZURE_LANGUAGE_ENDPOINT)")
    ap.add_argument("--api-key", default=None,
                    help="default: PII_API_KEY (cloud: AZURE_LANGUAGE_KEY)")
    ap.add_argument("--api-version", default=os.getenv("PII_API_VERSION", "2022-05-01"))
    ap.add_argument("--language", default="en")
    ap.add_argument("--domain", choices=["none", "phi"], default="none",
                    help="'phi' adds health-information entities")
    ap.add_argument("--categories", help="comma list of PII categories to detect (default: all)")
    ap.add_argument("--exclude", default="", help="comma list of categories to keep (not redact)")
    ap.add_argument("--allow", default="", help="comma list of exact strings never to redact")
    ap.add_argument("--min-confidence", type=float, default=0.5)
    ap.add_argument("--mode", choices=["category", "mask", "redacted"], default="category",
                    help="DOCX replacement / PDF box label: [Person], *****, or [REDACTED]")
    ap.add_argument("--ocr", action="store_true", help="OCR scanned PDF pages (needs Tesseract)")
    ap.add_argument("--report", choices=["none", "summary", "full"], default="summary",
                    help="'full' writes the detected PII values into the report (sensitive!)")
    ap.add_argument("--allow-remote", action="store_true",
                    help="permit a non-local endpoint (breaks the offline guarantee)")
    ap.add_argument("-v", "--verbose", action="store_true")
    return ap


def main(argv=None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s")
    if args.cloud:
        args.endpoint = args.endpoint or os.getenv("AZURE_LANGUAGE_ENDPOINT")
        args.api_key = args.api_key or os.getenv("AZURE_LANGUAGE_KEY")
        args.allow_remote = True
        if not args.endpoint or not args.api_key or "<" in args.endpoint + args.api_key:
            logging.error("--cloud needs AZURE_LANGUAGE_ENDPOINT and AZURE_LANGUAGE_KEY in .env")
            return 2
        logging.warning("CLOUD MODE: document text is sent to %s for analysis", args.endpoint)
    else:
        args.endpoint = args.endpoint or os.getenv("PII_ENDPOINT", "http://localhost:5000")
        args.api_key = args.api_key or os.getenv("PII_API_KEY")
    split = lambda s: {x.strip() for x in s.split(",") if x.strip()}
    try:
        client = LanguagePiiClient(
            endpoint=args.endpoint, api_key=args.api_key, api_version=args.api_version,
            language=args.language, domain=None if args.domain == "none" else args.domain,
            categories=sorted(split(args.categories)) if args.categories else None,
            allow_remote=args.allow_remote)
    except OfflineViolation as exc:
        logging.error(str(exc)); return 2
    ok, msg = client.health()
    if not ok:
        hint = ("check AZURE_LANGUAGE_ENDPOINT / AZURE_LANGUAGE_KEY in .env" if client.remote
                else "start the container (docker compose up -d) or use --cloud")
        logging.error("PII service not reachable at %s: %s (%s)", args.endpoint, msg, hint)
        return 2

    detector = PiiDetector(client, DetectorOptions(
        min_confidence=args.min_confidence, exclude_categories=split(args.exclude),
        mode=args.mode, allow_list=split(args.allow)))

    out_dir = Path(args.output)
    files = list(_iter_inputs(args.inputs))
    if not files:
        logging.error("no .pdf/.docx files found"); return 1
    failures = 0
    for f in files:
        dst = out_dir / f"{f.stem}.redacted{f.suffix.lower()}"
        try:
            rep = (redact_pdf(f, dst, detector, ocr=args.ocr) if f.suffix.lower() == ".pdf"
                   else redact_docx(f, dst, detector))
        except Exception as exc:
            failures += 1
            logging.error("%s: FAILED - %s", f, exc, exc_info=args.verbose)
            continue
        counts = {}
        for e in rep["entities"]:
            counts[e["category"]] = counts.get(e["category"], 0) + 1
            if args.report != "full":
                e.pop("_text", None)
            else:
                e["text"] = e.pop("_text")
        rep["summary"] = counts
        rep["output"] = str(dst)
        for w in rep["warnings"]:
            logging.warning("%s: %s", f.name, w)
        logging.info("%s -> %s  (%d redactions: %s)", f, dst, len(rep["entities"]),
                     ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "none")
        if args.report != "none":
            if args.report == "summary":
                rep.pop("entities")
            (out_dir / f"{f.stem}.report.json").write_text(json.dumps(rep, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
