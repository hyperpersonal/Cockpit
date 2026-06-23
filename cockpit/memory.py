"""Reflection memory (BM25). Ported from TradingAgents memory.py (read in full):
BM25Okapi over situations, normalized similarity. Lessons persist in state/reflection_memory.json
and grow at every postmortem."""
from __future__ import annotations
import json, re, os
from rank_bm25 import BM25Okapi

def _tok(t: str): return re.findall(r"\b\w+\b", t.lower())

class ReflectionMemory:
    def __init__(self, path: str):
        self.path = path
        self.lessons = []
        if os.path.exists(path):
            self.lessons = json.load(open(path, encoding="utf-8")).get("lessons", [])
        self._index()

    def _index(self):
        docs = [l["situation"] for l in self.lessons]
        self.bm25 = BM25Okapi([_tok(d) for d in docs]) if docs else None

    def retrieve(self, situation: str, n: int = 2) -> list[dict]:
        if not self.bm25: return []
        scores = self.bm25.get_scores(_tok(situation))
        order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:n]
        mx = max(scores) or 1.0
        return [{**self.lessons[i], "sim": round(float(scores[i] / mx), 2)}
                for i in order if scores[i] > 0]

    def add(self, situation: str, lesson: str, source: str, tags: list[str] | None = None):
        nid = "L%d" % (len(self.lessons) + 1)
        self.lessons.append(dict(id=nid, situation=situation, lesson=lesson,
                                 source=source, tags=tags or []))
        self._index()
        return nid

    def save(self):
        json.dump({"version": 1, "lessons": self.lessons}, open(self.path, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
