"""A scripted, deliberately gullible stand-in for a model (tests and the offline demo).

It answers reverie's own prompts from a script:

* **extract**: returns the script's candidates for the session in the prompt, including
  ones a careless model would propose (injected instructions, directory listings,
  fabricated claims). The engine's own checks must remove those.
* **verify**: supported when most content words of the claim appear in the cited
  evidence (so it is fooled by injected text, on purpose).
* **reconcile**: relation rules from the script, matched by substrings.
* **organize / promote**: topics by substring from the script; everything is "general".

This is never real learning; runs using it are labeled ``provider: mock``.
"""

from __future__ import annotations

import json
import re
from typing import Any

from reverie.dream.reconcile import tokens

_SESSION_RE = re.compile(r'<<<TRANSCRIPT session="([^"]+)"')
_PART_RE = re.compile(r'part="events (\d+)-(\d+)"')
_CAND_RE = re.compile(r'Candidate note:\n"(.*?)"\n\nCited evidence:\n\n(.*)', re.S)
_NEW_RE = re.compile(r'New note \(newest evidence [^)]*\):\n"(.*?)"')
_EXISTING_RE = re.compile(r'- id=(\S+) status=\S+ newest evidence \S+: "(.*)"')
_NOTE_LINE_RE = re.compile(r'- id=(\S+) kind=\S+: "(.*)"')


class ScriptedOracle:
    def __init__(self, script: dict[str, Any]) -> None:
        self.script = script

    def __call__(self, model: str, system: str, messages: list[dict[str, Any]]) -> str | None:
        user = str(messages[0].get("content", "")) if messages else ""
        tag = system.split("\n", 1)[0]
        if "extract@" in tag:
            return self._extract(user)
        if "verify@" in tag:
            return self._verify(user)
        if "reconcile@" in tag:
            return self._reconcile(user)
        if "organize@" in tag:
            return self._organize(user)
        if "promote@" in tag:
            ids = [m.group(1) for m in _NOTE_LINE_RE.finditer(user)]
            return json.dumps({"general": ids})
        return None

    def _extract(self, user: str) -> str:
        m = _SESSION_RE.search(user)
        sid = m.group(1) if m else ""
        lo, hi = 0, 10**9
        part = _PART_RE.search(user)
        if part:
            lo, hi = int(part.group(1)), int(part.group(2))
        cands = []
        for cand in self.script.get("extract", {}).get(sid, []):
            idxs = [int(e.split("#")[1]) for e in cand.get("evidence", []) if "#" in e]
            if idxs and all(lo <= i <= hi for i in idxs):
                cands.append(cand)
        return json.dumps({"candidates": cands})

    def _verify(self, user: str) -> str:
        m = _CAND_RE.search(user)
        if not m:
            return json.dumps({"verdict": "unsupported", "reason": "could not parse"})
        claim, evidence = m.group(1), m.group(2)
        ctoks = tokens(claim)
        etoks = tokens(evidence)
        frac = len(ctoks & etoks) / len(ctoks) if ctoks else 0.0
        verdict = "supported" if frac >= 0.34 else "unsupported"
        return json.dumps({"verdict": verdict, "reason": f"{frac:.0%} of the claim's content words are in evidence"})

    def _reconcile(self, user: str) -> str:
        m = _NEW_RE.search(user)
        new_text = m.group(1) if m else ""
        existing = _EXISTING_RE.findall(user)
        for rule in self.script.get("relations", []):
            if rule["new"] not in new_text:
                continue
            for nid, text in existing:
                if rule["old"] in text:
                    return json.dumps({"relation": rule["relation"], "target_id": nid,
                                       "merged_text": rule.get("merged_text"),
                                       "stronger": rule.get("stronger", "unclear"), "reason": "scripted"})
        return json.dumps({"relation": "unrelated", "target_id": None, "merged_text": None,
                           "stronger": "unclear", "reason": "scripted: no rule"})

    def _organize(self, user: str) -> str:
        out = []
        for nid, text in _NOTE_LINE_RE.findall(user):
            for key, topic in self.script.get("topics", {}).items():
                if key.lower() in text.lower():
                    out.append({"id": nid, "topic": topic})
                    break
        return json.dumps({"assignments": out})
