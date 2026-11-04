import hashlib
import heapq
import itertools
import json
import math
import re
import threading
import time
import traceback
from collections import OrderedDict
from typing import Any, Optional

import httpx

from core.config import env
from core.logger import logger
from core.request_context import log_context
from services.system_logs import record_log

DEFAULT_BASE = "http://127.0.0.1:8080"
DEFAULT_MODEL = "Qwen3.5-4B-Q4_K_M"
DEFAULT_SYSTEM = (
    "You are a precise assistant for an HR screening app. Do not use chain-of-thought. "
    "Reply with the requested output only."
)
JSON_SYSTEM = "You are a precise HR assistant. Return ONLY valid JSON. No markdown. No chain-of-thought."


PRIORITY_LIVE = 0
PRIORITY_BATCH = 1
CHARS_PER_TOKEN = 3.0
MIN_OUTPUT_TOKENS = 192
CTX_SAFETY_TOKENS = 48
CACHE_TTL_S = 900
CACHE_MAX = 64
HEALTH_TTL_S = 5.0
BREAKER_THRESHOLD = 3
BREAKER_COOLDOWN_S = 30.0


class LlmError(Exception):
    pass


class LlmBusy(LlmError):
    pass


def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(env(name) or default))
    except (TypeError, ValueError):
        return default


class Gate:
    def __init__(self):
        self.cond = threading.Condition()
        self.waiters: list = []
        self.counter = itertools.count()
        self.active = 0
        self.stats = {"served": 0, "busy_rejected": 0, "oversize_trimmed": 0, "timeouts": 0, "cache_hits": 0,
                      "breaker_rejected": 0, "failures": 0, "latency_total_s": 0.0}
        self.fail_streak = 0
        self.open_until = 0.0

    def slots(self) -> int:
        return _int_env("LLM_CONCURRENCY", 1)

    def max_queue(self) -> int:
        return _int_env("LLM_MAX_QUEUE", 12)

    def acquire(self, priority: int, wait_s: float) -> None:
        with self.cond:
            if time.monotonic() < self.open_until:
                self.stats["breaker_rejected"] += 1
                raise LlmBusy("Local Qwen is recovering from repeated failures, using the built-in fallback for now.")
            if self.active < self.slots() and not self.waiters:
                self.active += 1
                return
            if len(self.waiters) >= self.max_queue():
                self.stats["busy_rejected"] += 1
                raise LlmBusy("Local Qwen queue is full, using the built-in fallback for now.")
            ticket = (priority, next(self.counter))
            heapq.heappush(self.waiters, ticket)
            deadline = time.monotonic() + wait_s
            try:
                while not (self.active < self.slots() and self.waiters[0] == ticket):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        self.stats["busy_rejected"] += 1
                        raise LlmBusy("Timed out waiting for the local Qwen queue.")
                    self.cond.wait(remaining)
                heapq.heappop(self.waiters)
                self.active += 1
            except BaseException:
                if ticket in self.waiters:
                    self.waiters.remove(ticket)
                    heapq.heapify(self.waiters)
                self.cond.notify_all()
                raise

    def release(self, ok: bool, elapsed: float) -> None:
        with self.cond:
            self.active = max(0, self.active - 1)
            if ok:
                self.fail_streak = 0
                self.stats["served"] += 1
                self.stats["latency_total_s"] += elapsed
            else:
                self.stats["failures"] += 1
                self.fail_streak += 1
                if self.fail_streak >= BREAKER_THRESHOLD:
                    self.open_until = time.monotonic() + BREAKER_COOLDOWN_S
                    self.fail_streak = 0
            self.cond.notify_all()

    def snapshot(self) -> dict:
        with self.cond:
            served = self.stats["served"]
            return {**self.stats, "active": self.active, "queued": len(self.waiters), "slots": self.slots(),
                    "maxQueue": self.max_queue(), "breakerOpen": time.monotonic() < self.open_until,
                    "avgLatencyS": round(self.stats["latency_total_s"] / served, 2) if served else None}


gate = Gate()
_cache: "OrderedDict[str, tuple[float, str]]" = OrderedDict()
_cache_lock = threading.Lock()
_ctx_cache = {"value": None, "at": 0.0}
_health_cache = {"value": False, "at": 0.0}


def base_url() -> str:
    return (env("LOCAL_LLM_URL") or DEFAULT_BASE).rstrip("/")


def model_name() -> str:
    return env("LOCAL_LLM_MODEL") or DEFAULT_MODEL


def _headers() -> dict:
    headers = {"Content-Type": "application/json"}
    key = env("LOCAL_LLM_API_KEY")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def unavailable_message() -> str:
    return (
        f"Local Qwen ({model_name()}) is not running. Start it from python-ai/qwen with "
        f'.\\llamafile-0.10.5.exe -m ".\\Qwen3.5-4B-Q4_K_M.gguf" --server (server {base_url()}).'
    )


def strip_think(text: str) -> str:
    out = re.sub(r"<think>[\s\S]*?</think>", "", text or "", flags=re.I)
    return re.sub(r"</?think>", "", out, flags=re.I).strip()


def extract_json_payload(raw: str) -> str:
    cleaned = re.sub(r"```json", "", strip_think(raw), flags=re.I).replace("```", "").strip()
    arr, obj = cleaned.find("["), cleaned.find("{")
    if arr != -1 and (obj == -1 or arr < obj):
        start, end = arr, cleaned.rfind("]")
    elif obj != -1:
        start, end = obj, cleaned.rfind("}")
    else:
        return cleaned
    if end > start:
        return cleaned[start:end + 1]
    return cleaned[start:]


def repair_json(raw: str) -> str:
    s = extract_json_payload(raw).strip()
    if not s:
        return s
    if s.count('"') % 2 == 1:
        s += '"'
    braces = brackets = 0
    for ch in s:
        braces += (ch == "{") - (ch == "}")
        brackets += (ch == "[") - (ch == "]")
    return s + "]" * max(brackets, 0) + "}" * max(braces, 0)


def context_tokens() -> int:
    now = time.monotonic()
    if _ctx_cache["value"] and now - _ctx_cache["at"] < 300:
        return _ctx_cache["value"]
    detected = None
    try:
        res = httpx.get(f"{base_url()}/props", headers=_headers(), timeout=2.5)
        detected = int(((res.json().get("default_generation_settings") or {}).get("n_ctx")) or 0) or None
    except Exception:
        detected = None
    value = detected or _int_env("LLM_CTX_TOKENS", 2048)
    _ctx_cache.update(value=value, at=now)
    return value


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def fit_prompt(system: str, prompt: str, max_tokens: int) -> tuple[str, int]:
    ctx = context_tokens()
    out_floor = min(max_tokens, MIN_OUTPUT_TOKENS)
    budget = ctx - CTX_SAFETY_TOKENS - estimate_tokens(system) - out_floor
    if budget < 64:
        raise LlmError("Local Qwen context is too small for this request")
    if estimate_tokens(prompt) > budget:
        keep = int(budget * CHARS_PER_TOKEN)
        head, tail = int(keep * 0.45), int(keep * 0.55)
        prompt = prompt[:head] + "\n...\n" + prompt[-tail:]
        gate.stats["oversize_trimmed"] += 1
    room = ctx - CTX_SAFETY_TOKENS - estimate_tokens(system) - estimate_tokens(prompt)
    return prompt, max(out_floor, min(max_tokens, room))


def _cache_key(system: str, prompt: str, temperature: float, max_tokens: int) -> str:
    return hashlib.sha256(json.dumps([system, prompt, temperature, max_tokens]).encode()).hexdigest()


def _cache_get(key: str) -> Optional[str]:
    with _cache_lock:
        hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < CACHE_TTL_S:
            _cache.move_to_end(key)
            gate.stats["cache_hits"] += 1
            return hit[1]
        _cache.pop(key, None)
    return None


def _cache_put(key: str, text: str) -> None:
    with _cache_lock:
        _cache[key] = (time.monotonic(), text)
        while len(_cache) > CACHE_MAX:
            _cache.popitem(last=False)


def _post(body: dict, timeout: float) -> httpx.Response:
    return httpx.post(f"{base_url()}/v1/chat/completions", json=body, headers=_headers(), timeout=timeout)


def _request(system: str, prompt: str, temperature: float, max_tokens: int, timeout_s: float) -> str:
    messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
    slim = {"model": model_name(), "messages": messages, "temperature": temperature, "max_tokens": max_tokens,
            "stream": False, "cache_prompt": True}
    chat = {**slim, "chat_template_kwargs": {"enable_thinking": False}, "reasoning": "off"}
    try:
        res = _post(chat, timeout_s)
        if res.status_code == 400:
            res = _post(slim, timeout_s)
        if res.status_code in (401, 403):
            raise LlmError("Local Qwen rejected LOCAL_LLM_API_KEY. Make sure the key in .env.local matches the one Qwen was started with.")
        if res.status_code >= 400:
            raise LlmError(f"Local LLM HTTP {res.status_code}")
        choice = (res.json().get("choices") or [{}])[0]
        text = (choice.get("message") or {}).get("content") or choice.get("text") or ""
        if not str(text).strip():
            raise LlmError("Local LLM returned an empty response")
        return strip_think(text)
    except httpx.TimeoutException:
        gate.stats["timeouts"] += 1
        raise LlmError(f"Local Qwen ({model_name()}) timed out. The 4B model on CPU is slow, try again shortly.")
    except httpx.TransportError:
        raise LlmError(unavailable_message())


def complete(prompt: str, temperature: float = 0.2, max_tokens: int = 512, system: Optional[str] = None,
             timeout_s: float = 180.0, priority: int = PRIORITY_BATCH) -> str:
    system = system or DEFAULT_SYSTEM
    prompt, max_tokens = fit_prompt(system, prompt, max_tokens)
    cacheable = temperature <= 0.3
    key = _cache_key(system, prompt, temperature, max_tokens)
    if cacheable:
        cached = _cache_get(key)
        if cached is not None:
            elapsed_ms = 0.0
            logger.bind(module="ai", action="llm_cache_hit", model=model_name()).info("AI response served from cache")
            record_log({
                "module": "ai",
                "action": "llm_cache_hit",
                "log_level": "INFO",
                "status": "success",
                "message": "AI response served from cache",
                "execution_time": elapsed_ms,
                "input_tokens": estimate_tokens(system) + estimate_tokens(prompt),
                "output_tokens": estimate_tokens(cached),
                "model": model_name(),
                **log_context(),
            })
            return cached
    wait_s = _int_env("LLM_LIVE_WAIT_S", 25) if priority == PRIORITY_LIVE else min(timeout_s, _int_env("LLM_BATCH_WAIT_S", 600))
    started = time.monotonic()
    ok = False
    acquired = False
    try:
        gate.acquire(priority, wait_s)
        acquired = True
        text = _request(system, prompt, temperature, max_tokens, timeout_s)
        ok = True
    except Exception as err:
        elapsed_ms = round((time.monotonic() - started) * 1000, 2)
        logger.bind(module="ai", action="llm_completion", model=model_name(), execution_time_ms=elapsed_ms).exception(
            "AI completion failed"
        )
        record_log({
            "module": "ai",
            "action": "llm_completion",
            "log_level": "ERROR",
            "status": "failed",
            "message": "AI completion failed",
            "error_message": f"{type(err).__name__}: {err}",
            "stack_trace": "".join(traceback.format_exception(type(err), err, err.__traceback__)),
            "execution_time": elapsed_ms,
            "input_tokens": estimate_tokens(system) + estimate_tokens(prompt),
            "model": model_name(),
            **log_context(),
        })
        raise
    finally:
        if acquired:
            gate.release(ok, time.monotonic() - started)
    elapsed_ms = round((time.monotonic() - started) * 1000, 2)
    logger.bind(module="ai", action="llm_completion", model=model_name(), execution_time_ms=elapsed_ms).info(
        "AI completion succeeded"
    )
    record_log({
        "module": "ai",
        "action": "llm_completion",
        "log_level": "INFO",
        "status": "success",
        "message": "AI completion succeeded",
        "execution_time": elapsed_ms,
        "input_tokens": estimate_tokens(system) + estimate_tokens(prompt),
        "output_tokens": estimate_tokens(text),
        "model": model_name(),
        **log_context(),
    })
    if cacheable:
        _cache_put(key, text)
    return text


def complete_json(prompt: str, temperature: float = 0.2, max_tokens: int = 512, timeout_s: float = 180.0,
                  priority: int = PRIORITY_BATCH) -> Any:
    raw = complete(prompt, temperature, max_tokens, JSON_SYSTEM, timeout_s, priority)
    payload = extract_json_payload(raw)
    try:
        return json.loads(payload)
    except ValueError:
        try:
            return json.loads(repair_json(raw))
        except ValueError:
            raise LlmError(f"Local Qwen returned malformed JSON: {payload[:240]}")


def is_up() -> bool:
    now = time.monotonic()
    if now - _health_cache["at"] < HEALTH_TTL_S:
        return _health_cache["value"]
    up = False
    try:
        res = httpx.get(f"{base_url()}/health", headers=_headers(), timeout=2.5)
        up = res.status_code < 400 or httpx.get(f"{base_url()}/v1/models", headers=_headers(), timeout=2.5).status_code < 400
    except Exception:
        up = False
    _health_cache.update(value=up, at=now)
    return up


def stats() -> dict:
    return {**gate.snapshot(), "contextTokens": _ctx_cache["value"] or _int_env("LLM_CTX_TOKENS", 2048),
            "cacheEntries": len(_cache)}
