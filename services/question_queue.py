import heapq
import itertools
import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

import anyio

from core.config import env, env_int
from core.db import get_store
from services import question_gen, resumes

log = logging.getLogger("talentscope")

PENDING_KEY = "talentscope:qgen:pending"
WORKER_KEY = "talentscope:qgen:worker:"
LIVE_BOOST_S = 3600
HEARTBEAT_S = 30
SWEEP_S = 60
POLL_S = 1.0
FALLBACK_SETTLE_S = 6
MAX_GENERATION_WAIT_S = 300
CLAIM_PREFIX = "c:"
CLAIM_STALE_S = 60


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _age_s(value) -> float:
    if isinstance(value, datetime):
        moment = value
    else:
        try:
            moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return 0.0
    moment = moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - moment).total_seconds()


class Dispatcher:
    def __init__(self):
        self.heap: list = []
        self.cond = threading.Condition()
        self.counter = itertools.count()
        self.client = None
        self.redis_ok = False
        self.workers = 0
        self.lock = threading.Lock()

    def redis(self):
        url = env("REDIS_URL")
        if not url:
            return None
        with self.lock:
            if self.client is None:
                import redis

                self.client = redis.Redis.from_url(url, socket_timeout=3, socket_connect_timeout=3, decode_responses=True)
        return self.client

    def push(self, job: str, score: float) -> None:
        client = self.redis()
        if client is not None:
            try:
                client.zadd(PENDING_KEY, {job: score})
                self.redis_ok = True
                return
            except Exception as err:
                self.redis_ok = False
                log.warning("question queue redis unavailable, using local queue: %s", err)
        with self.cond:
            heapq.heappush(self.heap, (score, next(self.counter), job))
            self.cond.notify()

    def _pop_redis(self) -> Optional[str]:
        client = self.redis()
        if client is None:
            return None
        try:
            for job in client.zrangebyscore(PENDING_KEY, "-inf", time.time(), start=0, num=1):
                if client.zrem(PENDING_KEY, job):
                    self.redis_ok = True
                    return job
            self.redis_ok = True
        except Exception as err:
            self.redis_ok = False
            log.warning("question queue redis pop failed: %s", err)
        return None

    def _pop_local(self) -> Optional[str]:
        with self.cond:
            if self.heap and self.heap[0][0] <= time.time():
                return heapq.heappop(self.heap)[2]
        return None

    def pop(self, timeout: float) -> Optional[str]:
        deadline = time.monotonic() + timeout
        while True:
            job = self._pop_redis() or self._pop_local()
            if job or time.monotonic() >= deadline:
                return job
            with self.cond:
                self.cond.wait(min(0.25, max(0.0, deadline - time.monotonic())))

    def depth(self) -> int:
        local = len(self.heap)
        client = self.redis()
        if client is None:
            return local
        try:
            return local + int(client.zcard(PENDING_KEY))
        except Exception:
            return local

    def heartbeat(self, worker_id: str) -> None:
        client = self.redis()
        if client is not None:
            try:
                client.set(WORKER_KEY + worker_id, "1", ex=HEARTBEAT_S)
            except Exception:
                pass

    def remote_workers(self) -> int:
        client = self.redis()
        if client is None:
            return 0
        try:
            return sum(1 for _ in client.scan_iter(match=WORKER_KEY + "*", count=100))
        except Exception:
            return 0


dispatcher = Dispatcher()
_bank_attempts: dict[str, int] = {}
_stop = threading.Event()
_threads: list[threading.Thread] = []


def max_attempts() -> int:
    return max(1, env_int("QGEN_MAX_ATTEMPTS", 3))


def backoff_s(attempt: int) -> float:
    return env_int("QGEN_BACKOFF_S", 20) * (2 ** max(0, attempt - 1))


def start_wait_s() -> float:
    return float(max(120, env_int("QGEN_START_WAIT_S", 120)))


def workers_alive() -> bool:
    return dispatcher.workers > 0 or dispatcher.remote_workers() > 0


def snapshot() -> dict:
    counts: dict[str, int] = {}
    try:
        for row in get_store().select("question_sets", columns=["status"]):
            counts[row["status"]] = counts.get(row["status"], 0) + 1
    except Exception as err:
        log.warning("question queue snapshot failed: %s", err)
    return {"backend": "redis" if env("REDIS_URL") else "local", "redisUp": dispatcher.redis_ok if env("REDIS_URL") else None,
            "pending": dispatcher.depth(), "localWorkers": dispatcher.workers, "remoteWorkers": dispatcher.remote_workers(),
            "sets": counts, "maxAttempts": max_attempts()}


def _row(resume_id: str) -> Optional[dict]:
    return get_store().first("question_sets", {"resume_id": resume_id})


def enqueue(resume_id: str, live: bool = False, force: bool = False) -> dict:
    store = get_store()
    row = _row(resume_id)
    if row is None:
        try:
            store.insert("question_sets", {"resume_id": resume_id, "status": "queued", "attempts": 0, "queued_at": _now(), "updated_at": _now()})
        except Exception as err:
            row = _row(resume_id)
            if row is None:
                raise RuntimeError(f"Could not register question generation for {resume_id}") from err
            log.info("question set already registered: %s", err)
        else:
            row = _row(resume_id)
        if row is None:
            raise RuntimeError(f"Question generation state missing after registration for {resume_id}")
    elif row["status"] == "failed" or (force and row["status"] != "running"):
        store.update("question_sets", {"status": "queued", "attempts": 0, "last_error": None, "updated_at": _now()}, {"resume_id": resume_id})
        row = {**row, "status": "queued"}
    if row["status"] == "queued":
        dispatcher.push(f"q:{resume_id}", time.time() - (LIVE_BOOST_S if live else 0))
    return row


def enqueue_bank(bank_key: str) -> None:
    dispatcher.push(f"b:{bank_key}", time.time())


def reset(resume_id: str) -> None:
    get_store().delete("question_sets", {"resume_id": resume_id})


def existing_questions(resume_id: str) -> list[str]:
    rows = get_store().select("interview_questions", {"resume_id": resume_id}, ["question_text"], "question_index asc")
    return [r["question_text"] for r in rows]


def load_context(resume_id: str) -> tuple[dict, dict, str]:
    resume = resumes.get_resume(resume_id)
    if not resume:
        raise LookupError("Resume not found")
    report = resume.get("report") or {}
    config = report.get("interviewConfig") or {"interviewType": "technical", "sections": dict(question_gen.DEFAULT_SECTIONS)}
    return resume, config, question_gen.jd_text(report.get("jdId"))


def _claim_served(resume_id: str, previous: Optional[str]) -> Optional[str]:
    claim = f"{CLAIM_PREFIX}{uuid.uuid4().hex[:14]}"
    store = get_store()
    store.update("question_sets", {"served": claim, "updated_at": _now()}, {"resume_id": resume_id, "served": previous})
    row = _row(resume_id)
    return claim if row and row.get("served") == claim else None


def _write_questions(resume_id: str, items: list[dict]) -> None:
    store = get_store()
    store.delete("interview_questions", {"resume_id": resume_id})
    store.insert("interview_questions", [{"resume_id": resume_id, "question_index": i, "question_text": it["text"]} for i, it in enumerate(items)])


def _commit(resume_id: str, items: list[dict], target: str, previous: Optional[str]) -> bool:
    claim = _claim_served(resume_id, previous)
    if not claim:
        return False
    try:
        _write_questions(resume_id, items)
    except Exception:
        get_store().update("question_sets", {"served": previous}, {"resume_id": resume_id, "served": claim})
        raise
    get_store().update(
        "question_sets",
        {"status": "ready", "served": target, "updated_at": _now()},
        {"resume_id": resume_id, "served": claim},
    )
    return True


def serve_ai(resume_id: str, items: list[dict]) -> bool:
    return _commit(resume_id, items, "ai", None)


def serve_fallback(resume_id: str) -> bool:
    if _row(resume_id) is None:
        log.warning("question set missing for %s; generating fallback questions directly", resume_id)
        resume, config, jd = load_context(resume_id)
        result = question_gen.build_set(resume, config, jd, False, resume_id)
        _write_questions(resume_id, result["items"])
        return True
    resume, config, jd = load_context(resume_id)
    result = question_gen.build_set(resume, config, jd, False, resume_id)
    claim = _claim_served(resume_id, None)
    if not claim:
        return False
    try:
        _write_questions(resume_id, result["items"])
        get_store().update(
            "question_sets",
            {"status": "ready", "served": "fallback", "language": result["language"], "items": result["items"], "updated_at": _now()},
            {"resume_id": resume_id, "served": claim},
        )
        return True
    except Exception:
        get_store().update("question_sets", {"served": None}, {"resume_id": resume_id, "served": claim})
        raise


def _poll(resume_id: str) -> Optional[list[str]]:
    return existing_questions(resume_id) or None


def _begin(resume_id: str) -> Optional[list[str]]:
    questions = existing_questions(resume_id)
    if questions:
        return questions
    row = _row(resume_id)
    if row and row.get("served"):
        served = row["served"]
        if served.startswith(CLAIM_PREFIX):
            if _age_s(row.get("updated_at")) <= CLAIM_STALE_S:
                return None
        reset(resume_id)
        row = None
    if row and row["status"] == "failed":
        return _finalize(resume_id)
    try:
        enqueue(resume_id, live=True)
    except Exception as err:
        log.error("question queue registration failed for %s; using fallback: %s", resume_id, err)
        return _finalize(resume_id)
    if not workers_alive():
        return _finalize(resume_id)
    return None


def _finalize(resume_id: str) -> list[str]:
    if serve_fallback(resume_id):
        return existing_questions(resume_id)
    deadline = time.monotonic() + FALLBACK_SETTLE_S
    while time.monotonic() < deadline:
        questions = existing_questions(resume_id)
        if questions:
            return questions
        time.sleep(0.3)
    return existing_questions(resume_id)


def ensure_sync(resume_id: str, wait_s: Optional[float] = None) -> list[str]:
    ready = _begin(resume_id)
    if ready is not None:
        return ready
    requested_wait = MAX_GENERATION_WAIT_S if wait_s is None else wait_s
    wait_budget = min(MAX_GENERATION_WAIT_S, max(start_wait_s(), requested_wait))
    deadline = time.monotonic() + wait_budget
    while True:
        ready = _poll(resume_id)
        if ready:
            return ready
        row = _row(resume_id)
        if not row or row.get("status") in ("failed", "ready") or not workers_alive():
            return _finalize(resume_id)
        if time.monotonic() >= deadline:
            log.warning("question generation timed out for %s; using fallback", resume_id)
            return _finalize(resume_id)
        time.sleep(POLL_S)


async def ensure_async(resume_id: str, wait_s: Optional[float] = None) -> list[str]:
    ready = await anyio.to_thread.run_sync(_begin, resume_id)
    if ready is not None:
        return ready
    requested_wait = MAX_GENERATION_WAIT_S if wait_s is None else wait_s
    wait_budget = min(MAX_GENERATION_WAIT_S, max(start_wait_s(), requested_wait))
    deadline = time.monotonic() + wait_budget
    while True:
        ready = await anyio.to_thread.run_sync(_poll, resume_id)
        if ready:
            return ready
        row = await anyio.to_thread.run_sync(_row, resume_id)
        workers = await anyio.to_thread.run_sync(workers_alive)
        if not row or row.get("status") in ("failed", "ready") or not workers:
            return await anyio.to_thread.run_sync(_finalize, resume_id)
        if time.monotonic() >= deadline:
            log.warning("question generation timed out for %s; using fallback", resume_id)
            return await anyio.to_thread.run_sync(_finalize, resume_id)
        await anyio.sleep(POLL_S)


def _retry_or_fail(resume_id: str, attempts: int, err: Exception) -> None:
    store = get_store()
    message = str(err)[:480]
    if attempts < max_attempts():
        store.update("question_sets", {"status": "queued", "last_error": message, "updated_at": _now()}, {"resume_id": resume_id, "status": "running"})
        dispatcher.push(f"q:{resume_id}", time.time() + backoff_s(attempts))
        return
    store.update("question_sets", {"status": "failed", "last_error": message, "updated_at": _now()}, {"resume_id": resume_id, "status": "running"})


def process_question_job(resume_id: str) -> None:
    row = _row(resume_id)
    if not row or row["status"] != "queued":
        return
    attempts = (row.get("attempts") or 0) + 1
    claimed = get_store().update("question_sets", {"status": "running", "attempts": attempts, "updated_at": _now()},
                                 {"resume_id": resume_id, "status": "queued"})
    if not claimed:
        return
    try:
        resume, config, jd = load_context(resume_id)
        result = question_gen.build_set(resume, config, jd, True, resume_id)
    except LookupError:
        reset(resume_id)
        return
    except Exception as err:
        log.warning("question generation attempt %s failed for %s: %s", attempts, resume_id, err)
        _retry_or_fail(resume_id, attempts, err)
        return
    get_store().update("question_sets", {"language": result["language"], "items": result["items"], "last_error": None, "updated_at": _now()},
                       {"resume_id": resume_id, "status": "running"})
    try:
        serve_ai(resume_id, result["items"])
    except Exception as err:
        log.error("serving generated questions failed for %s: %s", resume_id, err)


def process_bank_job(bank_key: str) -> None:
    try:
        saved = question_gen.generate_bank(bank_key)
        _bank_attempts.pop(bank_key, None)
        log.info("question bank %s: %s new questions", bank_key, saved)
    except Exception as err:
        attempts = _bank_attempts.get(bank_key, 0) + 1
        _bank_attempts[bank_key] = attempts
        log.warning("question bank %s attempt %s failed: %s", bank_key, attempts, err)
        if attempts < max_attempts():
            dispatcher.push(f"b:{bank_key}", time.time() + backoff_s(attempts))


def process(job: str) -> None:
    kind, _, ref = job.partition(":")
    if kind == "q":
        process_question_job(ref)
    elif kind == "b":
        process_bank_job(ref)


def recover() -> int:
    store = get_store()
    lease = env_int("QGEN_LEASE_S", 900)
    revived = 0
    try:
        for row in store.select("question_sets", {"status": ["queued", "running"]}, ["resume_id", "status", "updated_at"]):
            if row["status"] == "running":
                if _age_s(row.get("updated_at")) < lease:
                    continue
                store.update("question_sets", {"status": "queued", "updated_at": _now()}, {"resume_id": row["resume_id"], "status": "running"})
            elif _age_s(row.get("updated_at")) < SWEEP_S:
                continue
            dispatcher.push(f"q:{row['resume_id']}", time.time())
            revived += 1
    except Exception as err:
        log.warning("question queue recovery failed: %s", err)
    return revived


def _worker_loop(worker_id: str) -> None:
    beat = 0.0
    while not _stop.is_set():
        if time.monotonic() - beat > HEARTBEAT_S / 3:
            dispatcher.heartbeat(worker_id)
            beat = time.monotonic()
        job = dispatcher.pop(2.0)
        if not job:
            continue
        try:
            process(job)
        except Exception as err:
            log.exception("question job %s crashed: %s", job, err)


def _sweeper_loop() -> None:
    while not _stop.wait(SWEEP_S):
        recover()


def start_workers(count: Optional[int] = None) -> int:
    count = env_int("QGEN_WORKERS", 2) if count is None else count
    if count <= 0 or _threads:
        return 0
    _stop.clear()
    base = uuid.uuid4().hex[:8]
    for i in range(count):
        thread = threading.Thread(target=_worker_loop, args=(f"{base}-{i}",), daemon=True, name=f"qgen-{i}")
        thread.start()
        _threads.append(thread)
    sweeper = threading.Thread(target=_sweeper_loop, daemon=True, name="qgen-sweeper")
    sweeper.start()
    _threads.append(sweeper)
    dispatcher.workers = count
    recover()
    return count


def stop_workers() -> None:
    _stop.set()
    for thread in _threads:
        thread.join(timeout=3)
    _threads.clear()
    dispatcher.workers = 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    started = start_workers(env_int("QGEN_WORKERS", 2) or 2)
    log.info("question workers started: %s (backend=%s)", started, "redis" if env("REDIS_URL") else "local")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        stop_workers()


if __name__ == "__main__":
    main()
