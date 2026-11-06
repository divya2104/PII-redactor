"""Filtering, merging and replacement-text logic shared by the PDF and DOCX redactors."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence, Set

from .client import Entity, LanguagePiiClient


@dataclass
class DetectorOptions:
    min_confidence: float = 0.5
    exclude_categories: Set[str] = field(default_factory=set)
    mode: str = "category"           # category | mask | redacted
    mask_char: str = "*"
    allow_list: Set[str] = field(default_factory=set)   # exact strings never redacted


class PiiDetector:
    def __init__(self, client: LanguagePiiClient, options: DetectorOptions):
        self.client = client
        self.opts = options
        self.stats: Counter = Counter()

    def detect_many(self, texts: Sequence[str]) -> List[List[Entity]]:
        raw = self.client.recognize(texts)
        return [self._clean(ents) for ents in raw]

    def detect(self, text: str) -> List[Entity]:
        return self.detect_many([text])[0]

    def _clean(self, ents: Iterable[Entity]) -> List[Entity]:
        keep = [
            e for e in ents
            if e.confidence >= self.opts.min_confidence
            and e.category not in self.opts.exclude_categories
            and e.text.strip() not in self.opts.allow_list
        ]
        keep.sort(key=lambda e: (e.offset, -e.length))
        merged: List[Entity] = []
        for e in keep:
            if merged and e.offset < merged[-1].end:
                last = merged[-1]
                if e.end > last.end:  # extend overlapping span, keep higher-confidence label
                    top = last if last.confidence >= e.confidence else e
                    full_text = last.text + e.text[last.end - e.offset:]
                    merged[-1] = Entity(last.offset, e.end - last.offset, top.category,
                                        top.subcategory, top.confidence, full_text)
                continue
            merged.append(e)
        for e in merged:
            self.stats[e.category] += 1
        return merged

    def replacement(self, e: Entity) -> str:
        if self.opts.mode == "mask":
            return "".join(c if c.isspace() else self.opts.mask_char for c in e.text)
        if self.opts.mode == "redacted":
            return "[REDACTED]"
        return f"[{e.category}]"

    def label(self, e: Entity) -> Optional[str]:
        """Text drawn on top of the black box in PDFs."""
        if self.opts.mode == "category":
            return e.category
        if self.opts.mode == "redacted":
            return "REDACTED"
        return None
