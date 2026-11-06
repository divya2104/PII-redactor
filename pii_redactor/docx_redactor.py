"""Word (.docx) redaction that preserves formatting.

Text is replaced inside the existing <w:t> runs, so fonts/bold/colours survive.
Covered: body, tables, text boxes, headers, footers, footnotes, endnotes,
comments, tracked deletions, field codes, external hyperlink targets and
core document properties.
"""
from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
from typing import List, Optional, Tuple

import docx
from lxml import etree

from .detector import PiiDetector

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W = "{%s}" % W_NS
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"

TEXT_TAGS = (W + "t", W + "delText")
SEP_TAGS = (W + "tab", W + "br", W + "cr")
TEXT_PART_SUFFIXES = (
    "document.main+xml", "header+xml", "footer+xml", "footnotes+xml",
    "endnotes+xml", "comments+xml",
)

Segment = Tuple[Optional[etree._Element], str]   # (editable element or None, text)


def _nearest_paragraph(el):
    p = el.getparent()
    while p is not None and p.tag != W + "p":
        p = p.getparent()
    return p


def _collect_units(root) -> List[List[Segment]]:
    """Group run text by its *own* paragraph (text boxes nest paragraphs)."""
    groups: "OrderedDict[int, Tuple[object, List[Segment]]]" = OrderedDict()
    for el in root.iter(*TEXT_TAGS, *SEP_TAGS):
        p = _nearest_paragraph(el)
        if p is None:
            continue
        segs = groups.setdefault(id(p), (p, []))[1]
        if el.tag in TEXT_TAGS:
            segs.append((el, el.text or ""))
        else:
            segs.append((None, "\t" if el.tag == W + "tab" else "\n"))
    units = [segs for _, segs in groups.values()]
    # Field codes (e.g. HYPERLINK "mailto:jane@x.com") are analysed individually
    for el in root.iter(W + "instrText"):
        units.append([(el, el.text or "")])
    return units


def _apply(segments: List[Segment], spans: List[Tuple[int, int, str]]) -> None:
    starts, pos = [], 0
    for _, t in segments:
        starts.append(pos); pos += len(t)
    texts = [t for _, t in segments]
    for s, e, repl in sorted(spans, reverse=True):
        placed = False
        for i, (el, _) in enumerate(segments):
            a, b = starts[i], starts[i] + len(segments[i][1])
            if b <= s or a >= e or el is None:
                continue
            ls, le = max(s, a) - a, min(e, b) - a
            texts[i] = texts[i][:ls] + ("" if placed else repl) + texts[i][le:]
            placed = True
    for (el, orig), new in zip(segments, texts):
        if el is not None and new != orig:
            el.text = new
            el.set(XML_SPACE, "preserve")


def _redact_root(root, detector: PiiDetector, report: dict, part_name: str) -> None:
    units = [u for u in _collect_units(root) if any(t.strip() for _, t in u)]
    texts = ["".join(t for _, t in u) for u in units]
    for unit, ents in zip(units, detector.detect_many(texts)):
        if not ents:
            continue
        _apply(unit, [(e.offset, e.end, detector.replacement(e)) for e in ents])
        for e in ents:
            report["entities"].append({
                "part": part_name, "category": e.category, "subcategory": e.subcategory,
                "confidence": round(e.confidence, 3), "length": e.length, "_text": e.text})
    # Author names on comments / tracked changes
    for el in root.iter():
        if el.get(W + "author") is not None:
            el.set(W + "author", "Redacted")
        if el.get(W + "initials") is not None:
            el.set(W + "initials", "R")


def redact_docx(src: Path, dst: Path, detector: PiiDetector) -> dict:
    document = docx.Document(str(src))
    report = {"file": str(src), "entities": [], "warnings": []}

    for part in document.part.package.iter_parts():
        if not part.content_type.endswith(TEXT_PART_SUFFIXES):
            continue
        element = getattr(part, "_element", None)
        if element is not None:
            _redact_root(element, detector, report, str(part.partname))
        else:  # generic Part (e.g. footnotes in some python-docx versions)
            root = etree.fromstring(part.blob)
            _redact_root(root, detector, report, str(part.partname))
            part._blob = etree.tostring(root, xml_declaration=True,
                                        encoding="UTF-8", standalone=True)

        # External hyperlink targets (mailto:, URLs with names/ids)
        ext = [r for r in part.rels.values() if r.is_external]
        if ext:
            hits = detector.detect_many([r.target_ref for r in ext])
            for rel, ents in zip(ext, hits):
                if ents or rel.target_ref.lower().startswith("mailto:"):
                    rel._target = "about:blank"

    cp = document.core_properties
    for attr in ("author", "last_modified_by", "title", "subject",
                 "keywords", "comments", "category", "content_status"):
        try:
            setattr(cp, attr, "")
        except Exception:
            pass

    if any(p.content_type.startswith("image/") for p in document.part.package.iter_parts()):
        report["warnings"].append("document contains images - text inside images is not analysed")

    dst.parent.mkdir(parents=True, exist_ok=True)
    document.save(str(dst))
    return report
