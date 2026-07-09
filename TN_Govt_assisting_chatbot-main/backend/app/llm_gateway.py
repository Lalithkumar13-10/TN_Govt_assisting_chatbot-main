"""LLM gateway.

Calls go through LiteLLM to Gemini, but we add rate-aware rotation on TOP of
LiteLLM because Gemini's hard limits include a PER-DAY cap (RPD) that LiteLLM's
Router does not track natively — only per-minute (rpm/tpm). So this pool:

  * picks the highest-priority model in a group that is under ALL of its
    rpm / tpm / rpd limits right now,
  * calls it via litellm.completion(...) with a Pydantic response_format,
  * on 429 / error, cools that model down and falls through to the next,
  * if every model is exhausted, raises GatewayExhausted so the caller can
    degrade to a plain guided form (a citizen is never turned away).

With no GEMINI_API_KEY, it runs in deterministic MOCK mode so the whole app is
demoable offline.
"""
from __future__ import annotations
import time, json, datetime as dt
from collections import deque, defaultdict
from typing import Type
from pydantic import BaseModel

from . import config
from .schemas import IntakeResult
from .mock_llm import mock_intake


class GatewayExhausted(RuntimeError):
    pass


class _ModelState:
    def __init__(self, cfg: config.ModelCfg):
        self.cfg = cfg
        self.minute_reqs: deque[float] = deque()      # timestamps in the last 60s
        self.minute_tokens: deque[tuple[float, int]] = deque()
        self.day = _today()
        self.day_reqs = 0
        self.cooldown_until = 0.0

    def _roll(self):
        cutoff = time.time() - 60
        while self.minute_reqs and self.minute_reqs[0] < cutoff:
            self.minute_reqs.popleft()
        while self.minute_tokens and self.minute_tokens[0][0] < cutoff:
            self.minute_tokens.popleft()
        if self.day != _today():
            self.day, self.day_reqs = _today(), 0

    def available(self, est_tokens: int) -> bool:
        self._roll()
        if time.time() < self.cooldown_until:
            return False
        if len(self.minute_reqs) >= self.cfg.rpm:
            return False
        if sum(t for _, t in self.minute_tokens) + est_tokens > self.cfg.tpm:
            return False
        if self.day_reqs >= self.cfg.rpd:
            return False
        return True

    def record(self, tokens: int):
        n = time.time()
        self.minute_reqs.append(n)
        self.minute_tokens.append((n, tokens))
        self.day_reqs += 1

    def cooldown(self, seconds: int = 30):
        self.cooldown_until = time.time() + seconds


def _today() -> str:
    return dt.date.today().isoformat()


class GeminiPool:
    def __init__(self):
        self.states = [_ModelState(c) for c in config.GEMINI_POOL]

    def status(self) -> list[dict]:
        out = []
        for s in self.states:
            s._roll()
            out.append({
                "model": s.cfg.name, "group": s.cfg.group,
                "rpm": f"{len(s.minute_reqs)}/{s.cfg.rpm}",
                "rpd": f"{s.day_reqs}/{s.cfg.rpd}",
                "cooling_down": time.time() < s.cooldown_until,
                "available": s.available(500),
            })
        return out

    def _candidates(self, group: str, est_tokens: int) -> list[_ModelState]:
        cands = [s for s in self.states if s.cfg.group == group and s.available(est_tokens)]
        return sorted(cands, key=lambda s: s.cfg.order)

    def generate(self, *, group: str, system: str, user: str,
                 schema: Type[BaseModel] = IntakeResult) -> BaseModel:
        est = (len(system) + len(user)) // 4 + 512
        if not config.llm_is_live():
            return mock_intake(user)  # offline deterministic path

        import litellm  # lazy import so the app runs without litellm installed
        last_err = None
        for state in self._candidates(group, est):
            try:
                resp = litellm.completion(
                    model=state.cfg.name,
                    api_key=config.GEMINI_API_KEY,
                    messages=[{"role": "system", "content": system},
                              {"role": "user", "content": user}],
                    response_format=schema,      # LiteLLM -> Gemini response_schema
                    timeout=20,
                )
                content = resp.choices[0].message.content
                used = getattr(getattr(resp, "usage", None), "total_tokens", est)
                state.record(used)
                return schema.model_validate_json(_strip(content))
            except Exception as e:                      # noqa: BLE001
                last_err = e
                if "429" in str(e) or "rate" in str(e).lower():
                    state.cooldown(30)
                continue
        raise GatewayExhausted(f"All '{group}' models unavailable. last={last_err}")


def _strip(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.strip("`")
        t = t[t.find("{"):]
    return t


POOL = GeminiPool()
