"""True PDF redaction: PII text is removed from the content stream (not just covered)."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pymupdf

from .detector import PiiDetector

log = logging.getLogger(__name__)

Char = Tuple[str, Optional[pymupdf.Rect], int]   # (char, bbox, line_id)


def _page_chars(page: pymupdf.Page, textpage=None) -> List[Char]:
    """Characters in reading order with per-char bboxes; separators carry no bbox."""
    chars: List[Char] = []
    raw = page.get_text("rawdict", textpage=textpage, sort=True)
    line_id = 0
    for block in raw["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block["lines"]:
            line_id += 1
            for span in line["spans"]:
                for ch in span["chars"]:
                    chars.append((ch["c"], pymupdf.Rect(ch["bbox"]), line_id))
            chars.append(("\n", None, line_id))
        chars.append(("\n", None, line_id))
    return chars


def _rects_for_span(chars: List[Char], start: int, end: int) -> List[pymupdf.Rect]:
    by_line: Dict[int, pymupdf.Rect] = {}
    for c, bbox, lid in chars[start:end]:
        if bbox is None or c.isspace() and lid not in by_line:
            continue
        by_line[lid] = by_line[lid] | bbox if lid in by_line else pymupdf.Rect(bbox)
    return list(by_line.values())


def redact_pdf(src: Path, dst: Path, detector: PiiDetector, ocr: bool = False,
               ocr_language: str = "eng") -> dict:
    doc = pymupdf.open(src)
    report = {"file": str(src), "pages": doc.page_count, "entities": [], "warnings": []}

    page_data = []
    for page in doc:
        tp = None
        if not page.get_text("text").strip():
            if ocr:
                try:
                    tp = page.get_textpage_ocr(language=ocr_language, dpi=300, full=True)
                except Exception as exc:  # Tesseract missing etc.
                    report["warnings"].append(f"page {page.number + 1}: OCR failed: {exc}")
            elif page.get_images():
                report["warnings"].append(
                    f"page {page.number + 1}: no text layer (scanned?) - not analysed; use --ocr")
        chars = _page_chars(page, tp)
        page_data.append((page, chars, "".join(c for c, _, _ in chars)))

    all_entities = detector.detect_many([t for _, _, t in page_data])

    for (page, chars, _text), ents in zip(page_data, all_entities):
        redact_rects = []
        for e in ents:
            rects = _rects_for_span(chars, e.offset, e.end)
            if not rects:
                continue
            label = detector.label(e)
            for r in rects:
                page.add_redact_annot(
                    r, text=label, fontsize=min(6, max(r.height * 0.6, 3)),
                    fill=(0, 0, 0), text_color=(1, 1, 1), cross_out=False)
                redact_rects.append(r)
            report["entities"].append({
                "page": page.number + 1, "category": e.category,
                "subcategory": e.subcategory, "confidence": round(e.confidence, 3),
                "length": e.length, "_text": e.text})

        # Drop links whose hotspot or target (e.g. mailto:) leaks PII
        ent_texts = [e.text.lower() for e in ents]
        for link in page.get_links():
            uri = (link.get("uri") or "").lower()
            if any(link["from"].intersects(r) for r in redact_rects) or \
               any(t and t in uri for t in ent_texts):
                page.delete_link(link)

        # Strip annotations (comments/sticky notes) whose content contains PII
        for annot in list(page.annots() or []):
            if annot.type[0] == pymupdf.PDF_ANNOT_REDACT:
                continue
            content = (annot.info.get("content") or "").lower()
            if any(t and t in content for t in ent_texts):
                page.delete_annot(annot)

        if redact_rects:
            page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_PIXELS,
                                  graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)

    # Scrub document-level metadata that commonly carries names
    doc.set_metadata({})
    doc.del_xml_metadata()
    if doc.get_toc():
        report["warnings"].append("document has bookmarks/outline - review manually")
    if doc.is_form_pdf:
        report["warnings"].append("document has form fields - field values are not analysed")

    dst.parent.mkdir(parents=True, exist_ok=True)
    doc.save(dst, garbage=4, deflate=True, clean=True)
    doc.close()
    return report
