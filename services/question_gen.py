import hashlib
import json
import logging
import random
import re
import time
from functools import lru_cache
from pathlib import Path
from typing import Optional

from core.db import get_store
from services import llm

log = logging.getLogger("talentscope")

DATA = Path(__file__).resolve().parents[1] / "data" / "interview_question_bank.json"
CODING_PREFIX = "Coding Challenge:"
SUPPORTED = ("python", "javascript", "typescript", "java", "cpp")
LABELS = {"python": "Python", "javascript": "JavaScript", "typescript": "TypeScript", "java": "Java", "cpp": "C++"}
DEFAULT_SECTIONS = {"overlapping": 8, "gap": 3, "projects": 4, "coding": 2}
NON_TECH = (("behavioral", "Behavioral"), ("leadership", "Leadership"), ("softskills", "Soft Skills"))
BANK_CACHE_S = 60
BANK_TARGET = 8

SKILL_PATTERNS = {
    "python": r"\bpython\b|\bdjango\b|\bflask\b|\bfastapi\b|\bpandas\b|\bnumpy\b|\bpytest\b|\bpyspark\b|\bcelery\b",
    "typescript": r"\btypescript\b|\bangular\b|\bnestjs\b|\bts\b",
    "javascript": r"\bjavascript\b|\bnode\.?js\b|\breact(?:\.?js)?\b|\bvue(?:\.?js)?\b|\bexpress(?:\.?js)?\b|\bnext\.?js\b|\bjquery\b|\bes6\b|\bjs\b",
    "java": r"\bjava\b|\bspring(?: boot)?\b|\bhibernate\b|\bmaven\b|\bgradle\b|\bj2ee\b|\bservlets?\b",
    "cpp": r"c\+\+|\bcpp\b|\bqt\b|\bstl\b|\bboost\b",
}
NAMED_PATTERNS = {
    "python": r"\bpython\b",
    "typescript": r"\btypescript\b",
    "javascript": r"\bjavascript\b|\bnode\.?js\b",
    "java": r"\bjava\b",
    "cpp": r"c\+\+|\bcpp\b",
}
FAMILY_PATTERNS = (
    ("java", r"c#|\.net\b|\bkotlin\b|\bscala\b|\bgroovy\b"),
    ("cpp", r"\bgolang\b|\brust\b|\bobjective-c\b|\bc language\b"),
    ("python", r"\bruby\b|\bphp\b|\bperl\b|\bmatlab\b|\bshell\b|\bbash\b|\bpowershell\b"),
)
DOMAIN_PATTERNS = {
    "sql": r"\bsql\b|\bmysql\b|\bpostgres(?:ql)?\b|\boracle\b|\bt-sql\b|\bpl/?sql\b|\bssis\b|\bsnowflake\b",
    "devops": r"\bdocker\b|\bkubernetes\b|\bjenkins\b|\bterraform\b|\bansible\b|\baws\b|\bazure\b|\bci/cd\b|\bdevops\b|\bgitlab\b",
    "testing": r"\bselenium\b|\bcypress\b|\bplaywright\b|\btestng\b|\bjunit\b|\bqa\b|\bautomation testing\b|\bmanual testing\b|\bsdet\b",
}


class QuestionGenError(Exception):
    pass


@lru_cache(maxsize=1)
def preset_bank() -> dict:
    return json.loads(DATA.read_text(encoding="utf-8"))


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def text_hash(text: str) -> str:
    return hashlib.sha1(_norm(text).encode("utf-8")).hexdigest()


def _hits(pattern: str, text: str) -> int:
    return len(re.findall(pattern, text, re.I))


def _skill_text(parsed: dict) -> str:
    skills = parsed.get("skills") or {}
    return " | ".join(str(s) for key in ("technical", "tools", "languages", "other") for s in (skills.get(key) or []))


def _experience_text(parsed: dict) -> str:
    parts = []
    for entry in (parsed.get("experience") or []) + (parsed.get("projects") or []):
        if not isinstance(entry, dict):
            continue
        parts.append(" ".join(str(entry.get(k) or "") for k in ("position", "name", "description")))
        parts.extend(str(b) for b in (entry.get("bulletPoints") or []))
        parts.extend(str(t) for t in (entry.get("technologies") or []))
    return "\n".join(parts)


def resolve_language(parsed: dict, jd: str) -> dict:
    skills, history = _skill_text(parsed or {}), _experience_text(parsed or {})
    candidate, wanted = {}, {}
    for lang, pattern in SKILL_PATTERNS.items():
        score = 4 * _hits(pattern, skills) + min(_hits(pattern, history), 6) * 2
        if score:
            candidate[lang] = score
        if _hits(pattern, jd or ""):
            wanted[lang] = _hits(pattern, jd or "")
    if "typescript" in candidate and "javascript" in candidate and candidate["typescript"] >= candidate["javascript"]:
        candidate["javascript"] = max(0, candidate["javascript"] - candidate["typescript"] // 2)
    if candidate:
        ranked = sorted(candidate, key=lambda lang: candidate[lang] + (6 if lang in wanted else 0), reverse=True)
        return {"language": ranked[0], "basis": "resume", "candidate": candidate, "jd": wanted}
    if wanted:
        return {"language": max(wanted, key=wanted.get), "basis": "jd", "candidate": candidate, "jd": wanted}
    blob = f"{skills}\n{history}\n{jd or ''}".lower()
    for lang, pattern in FAMILY_PATTERNS:
        if re.search(pattern, blob):
            return {"language": lang, "basis": "family", "candidate": candidate, "jd": wanted}
    return {"language": "python", "basis": "default", "candidate": candidate, "jd": wanted}


def stack_keys(parsed: dict, jd: str, language: str) -> list[str]:
    blob = f"{_skill_text(parsed or {})}\n{_experience_text(parsed or {})}"
    scored = {key: _hits(p, blob) * 2 + _hits(p, jd or "") for key, p in DOMAIN_PATTERNS.items()}
    domains = [key for key, score in sorted(scored.items(), key=lambda kv: kv[1], reverse=True) if score >= 2]
    return [language, *domains[:2], "general"]


def jd_text(jd_id: Optional[str]) -> str:
    if not jd_id:
        return ""
    try:
        row = get_store().first("job_descriptions", {"id": jd_id}, ["jd_text"])
        return (row or {}).get("jd_text") or ""
    except Exception as err:
        log.error("Error fetching JD: %s", err)
        return ""


def jd_excerpt(jd: str, limit: int = 1800) -> str:
    text = re.sub(r"\s+", " ", jd or "").strip()
    return text[:limit]


def candidate_brief(resume: dict, limit: int = 1500) -> str:
    parsed = resume.get("parsed") or {}
    skills = _skill_text(parsed)
    roles = "; ".join(
        f"{e.get('position') or ''} @ {e.get('company') or ''} ({e.get('startDate') or ''}-{e.get('endDate') or ''})"
        for e in (parsed.get("experience") or [])[:4] if isinstance(e, dict))
    projects = "; ".join(
        f"{p.get('name') or ''}: {str(p.get('description') or '')[:140]}"
        for p in (parsed.get("projects") or [])[:3] if isinstance(p, dict))
    targets = ", ".join((resume.get("report") or {}).get("targetRoles") or [])
    return f"Skills: {skills}\nRoles: {roles}\nProjects: {projects}\nTarget roles: {targets}"[:limit]


def _section(config: dict, key: str, default: int) -> int:
    value = (config.get("sections") or {}).get(key)
    try:
        return max(0, int(value)) if value is not None else default
    except (TypeError, ValueError):
        return default


def sections(config: dict) -> dict:
    return {key: _section(config, key, default) for key, default in {**DEFAULT_SECTIONS, "behavioral": 5, "leadership": 5, "softskills": 5}.items()}


_bank_cache: dict = {"at": 0.0, "rows": []}


def bank_rows() -> list[dict]:
    if time.monotonic() - _bank_cache["at"] < BANK_CACHE_S:
        return _bank_cache["rows"]
    try:
        rows = get_store().select("interview_question_bank", columns=["bank_key", "kind", "language", "difficulty", "question_text"])
    except Exception as err:
        log.warning("question bank unavailable: %s", err)
        rows = []
    _bank_cache.update(at=time.monotonic(), rows=rows)
    return rows


def reset_bank_cache() -> None:
    _bank_cache.update(at=0.0, rows=[])


def _pool(kind: str, keys: list[str], language: Optional[str] = None) -> list[str]:
    seen, out = set(), []
    preset = preset_bank().get(kind, {})
    ordered = []
    for key in keys:
        ordered.extend(item["q"] for item in preset.get(key, []))
        ordered.extend(r["question_text"] for r in bank_rows()
                       if r["kind"] == kind and r["bank_key"] == key and (not language or r.get("language") in (None, language)))
    for text in ordered:
        h = _norm(text)
        if h not in seen:
            seen.add(h)
            out.append(text)
    return out


def _take(pool: list[str], count: int, rng: random.Random, taken: set) -> list[str]:
    candidates = [t for t in pool if _norm(t) not in taken]
    rng.shuffle(candidates)
    chosen = candidates[:count]
    taken.update(_norm(t) for t in chosen)
    return chosen


def preset_verbal(keys: list[str], count: int, rng: random.Random, taken: set) -> list[str]:
    if count <= 0:
        return []
    primary = _take(_pool("verbal", keys[:1]), (count + 1) // 2, rng, taken)
    rest = _take(_pool("verbal", keys[1:]), count - len(primary), rng, taken)
    return primary + rest + _take(_pool("verbal", keys), count - len(primary) - len(rest), rng, taken)


def preset_coding(language: str, count: int, rng: random.Random, taken: set) -> list[str]:
    return _take(_pool("coding", [language], language), count, rng, taken)


TEMPLATE_VERBAL = (
    "Can you explain a challenging scenario you faced while working on {project}?",
    "What are the most common performance bottlenecks you've encountered with {primary} and how did you resolve them?",
    "How do you ensure code quality, unit testing, and scalability in the features you build with {primary}?",
    "Can you explain the architecture of a complex system you built or contributed to using {secondary}?",
    "What design patterns do you most frequently use with {primary} and why?",
    "How do you approach debugging a critical production issue involving {secondary} where logging is minimal?",
    "What security vulnerabilities do you proactively look for when writing or reviewing {primary} code?",
    "Describe how you would approach migrating a legacy feature built with {tertiary} to a modern stack.",
)
PREFERRED_NON_TECH = {
    "behavioral": (
        "Describe a time when you had to work with a difficult team member or client. How did you resolve the situation?",
        "Explain a time when you made a mistake on a project. How did you address it and what did you learn?",
        "Describe a situation where you had to quickly adapt to a major change at work.",
        "How do you prioritize your tasks when handling multiple deadlines or high-pressure projects?",
        "What motivates you in your professional career, and how do you align it with company goals?",
    ),
    "leadership": (
        "Can you share an experience where you led a project or initiative? What was the outcome?",
        "How do you handle constructive feedback or disagreement with a supervisor's decision?",
        "Describe a time when you went above and beyond your standard job responsibilities to deliver a project.",
        "How do you mentor junior team members or share knowledge across your team?",
        "Describe a time you had to persuade stakeholders to adopt your proposed design or plan.",
    ),
    "softskills": (
        "Describe a time you solved a complex problem. What steps did you take to find the solution?",
        "How do you explain technical or complex concepts to non-technical stakeholders or team members?",
        "How do you handle ambiguous requirements when starting a new task or project?",
        "Describe how you build trust and maintain strong relationships with your cross-functional partners.",
        "What strategies do you use to manage your time and avoid burnout during busy quarters?",
    ),
}


def template_personal(resume: dict, jd: str, count: int, rng: random.Random, taken: set) -> list[str]:
    parsed = resume.get("parsed") or {}
    skills = [s for s in re.split(r"\s*\|\s*", _skill_text(parsed)) if s]
    overlap = [s for s in skills if jd and s.lower() in jd.lower()]
    picks = overlap or skills or ["your primary technology"]
    primary, secondary, tertiary = (picks + picks + picks)[0], (picks + picks + picks)[1], (picks + picks + picks)[2]
    projects = [p for p in (parsed.get("projects") or []) if isinstance(p, dict) and p.get("name")]
    project = projects[0]["name"] if projects else "a recent project"
    filled = [t.format(project=project, primary=primary, secondary=secondary, tertiary=tertiary) for t in TEMPLATE_VERBAL]
    return _take(filled, count, rng, taken)


def non_tech_presets(config_sections: dict, rng: random.Random, taken: set) -> list[dict]:
    items = []
    for key, label in NON_TECH:
        for text in _take(list(PREFERRED_NON_TECH[key]), config_sections[key], rng, taken):
            items.append({"text": text, "kind": key, "source": "preset"})
    return items


def _item_text(raw) -> Optional[str]:
    if isinstance(raw, str):
        return raw.strip() or None
    if isinstance(raw, dict) and isinstance(raw.get("question"), str):
        return raw["question"].strip() or None
    return None


def _json_list(raw, expected: int, label: str) -> list[str]:
    if not isinstance(raw, list):
        raise QuestionGenError(f"{label}: unexpected response shape")
    texts = [t for t in (_item_text(i) for i in raw) if t]
    if len(texts) < expected:
        raise QuestionGenError(f"{label}: expected {expected}, got {len(texts)}")
    return texts[:expected]


def _ask(prompt: str, max_tokens: int, priority: int = llm.PRIORITY_BATCH) -> object:
    try:
        return llm.complete_json(prompt, temperature=0.2, max_tokens=max_tokens, timeout_s=300, priority=priority)
    except llm.LlmError as err:
        raise QuestionGenError(str(err)) from err


def personal_verbal_prompt(overlap: int, gap: int, projects: int, brief: str, jd: str) -> str:
    count = overlap + gap + projects
    return f"""You are an elite technical interviewer. Write {count} highly specific technical verbal interview questions for this candidate.

Job description / business requirement:
{jd_excerpt(jd) or "Not specified. Focus on the candidate's strongest skills."}

Candidate:
{brief}

RULES:
1. Use only facts and skills explicitly present in the candidate profile or job description. Never claim the candidate has experience or skills not shown in the profile.
2. Match depth to the candidate's seniority and use scenarios, troubleshooting and trade-offs rather than definitions.
3. Generate exactly {overlap} questions about skills present in both the profile and job description, exactly {gap} transferability questions about job requirements not evidenced in the profile, and exactly {projects} questions about the candidate's own projects and experience.
4. Return ONLY a JSON array of {count} objects: [{{"question": "...", "difficulty": "Easy|Medium|Hard", "source": "JD Skill|CV Skill"}}]"""


def personal_coding_prompt(count: int, language: str, brief: str, jd: str) -> str:
    name = LABELS[language]
    return f"""You are an elite technical interviewer. Write {count} hands-on coding challenges for this candidate.

Job description / business requirement:
{jd_excerpt(jd, 900) or "Not specified."}

Candidate:
{brief}

RULES:
1. Every challenge MUST be in {name}, the candidate's strongest language. Name {name} explicitly and never mention or use any other programming language.
2. Match difficulty to seniority and make the problem relevant to the candidate's domain where possible.
3. Each question starts with "{CODING_PREFIX} ({name}) " and contains a problem statement, input/output, constraints, at least 2 examples and a starter signature in {name}.
4. Return ONLY a JSON array of {count} objects: [{{"question": "{CODING_PREFIX} ({name}) ...", "difficulty": "Easy|Medium|Hard"}}]"""


def personal_non_tech_prompt(counts: dict, brief: str, jd: str) -> str:
    total = sum(counts.values())
    return f"""You are an elite HR interviewer. Write {total} professional non-technical interview questions for this candidate.

Job description / business requirement:
{jd_excerpt(jd, 900) or "Not specified."}

Candidate:
{brief}

RULES:
1. Adjust to the candidate's seniority.
2. Distribution (exactly {total}): {counts['behavioral']} Behavioral, {counts['leadership']} Leadership & Collaboration, {counts['softskills']} Problem Solving & Soft Skills, in that order.
3. Frame as situational prompts ("Tell me about a time when...").
4. Return ONLY a JSON array of {total} objects: [{{"question": "...", "difficulty": "Easy|Medium|Hard"}}]"""


def valid_coding(text: str, language: str) -> bool:
    if not text.startswith(CODING_PREFIX):
        return False
    low = text.lower()
    if not re.search(NAMED_PATTERNS[language], low):
        return False
    return not any(re.search(NAMED_PATTERNS[other], low) for other in SUPPORTED if other != language)


def ai_verbal(overlap: int, gap: int, projects: int, brief: str, jd: str, taken: set) -> list[str]:
    count = overlap + gap + projects
    texts = _json_list(_ask(personal_verbal_prompt(overlap, gap, projects, brief, jd), 1400, llm.PRIORITY_LIVE), count, "verbal")
    return [t for t in texts if _norm(t) not in taken]


def ai_coding(count: int, language: str, brief: str, jd: str) -> list[str]:
    raw = _ask(personal_coding_prompt(count, language, brief, jd), 1600, llm.PRIORITY_LIVE)
    texts = [t for t in (_item_text(i) for i in (raw if isinstance(raw, list) else [])) if t]
    return [t for t in texts if valid_coding(t, language)][:count]


def ai_non_tech(counts: dict, brief: str, jd: str) -> list[str]:
    return _json_list(_ask(personal_non_tech_prompt(counts, brief, jd), 1200, llm.PRIORITY_LIVE), sum(counts.values()), "non-technical")


def _mark(texts: list[str], kind: str, source: str, language: Optional[str] = None) -> list[dict]:
    return [{"text": t, "kind": kind, "source": source, **({"language": language} if language else {})} for t in texts]


def build_set(resume: dict, config: dict, jd: str, use_ai: bool, seed: str) -> dict:
    rng = random.Random(seed)
    counts = sections(config)
    kind = config.get("interviewType") or "technical"
    include_tech, include_non_tech = kind in ("technical", "both"), kind in ("non-technical", "both")
    parsed = resume.get("parsed") or {}
    resolved = resolve_language(parsed, jd)
    language = resolved["language"]
    keys = stack_keys(parsed, jd, language)
    brief = candidate_brief(resume)
    taken: set = set()
    items: list[dict] = []
    if include_tech:
        overlap, personal, coding = counts["overlapping"], counts["gap"] + counts["projects"], counts["coding"]
        verbal_texts: list[str] = []
        if use_ai and overlap + personal:
            verbal_texts = ai_verbal(overlap, counts["gap"], counts["projects"], brief, jd, taken)
            taken.update(_norm(t) for t in verbal_texts)
            items += _mark(verbal_texts, "verbal", "ai")
        missing_verbal = overlap + personal - len(verbal_texts)
        if missing_verbal > 0:
            template_count = min(personal, missing_verbal)
            items += _mark(template_personal(resume, jd, template_count, rng, taken), "verbal", "template")
            missing_verbal -= template_count
            items += _mark(preset_verbal(keys, missing_verbal, rng, taken), "verbal", "preset")
        coding_texts: list[str] = []
        if use_ai and coding:
            coding_texts = ai_coding(coding, language, brief, jd)
            taken.update(_norm(t) for t in coding_texts)
            items += _mark(coding_texts, "coding", "ai", language)
        missing = coding - len(coding_texts)
        if missing > 0:
            items += _mark(preset_coding(language, missing, rng, taken), "coding", "preset", language)
    if include_non_tech:
        extra = {key: counts[key] for key, _ in NON_TECH}
        texts = ai_non_tech(extra, brief, jd) if use_ai and sum(extra.values()) else []
        if texts:
            cursor = 0
            for key, _ in NON_TECH:
                items += _mark(texts[cursor:cursor + extra[key]], key, "ai")
                cursor += extra[key]
        else:
            items += non_tech_presets(counts, rng, taken)
    if not items:
        raise QuestionGenError("No questions generated for the selected assessment focus.")
    return {"items": items, "language": language, "basis": resolved["basis"], "stack": keys}


BANK_VERBAL_PROMPT = """You are an elite technical interviewer. Write {count} widely applicable interview questions for the role area "{key}". Mix Easy, Medium and Hard. Use scenarios, troubleshooting and trade-offs rather than definitions.
Return ONLY a JSON array of {count} objects: [{{"question": "...", "difficulty": "Easy|Medium|Hard"}}]"""

BANK_CODING_PROMPT = """You are an elite technical interviewer. Write {count} distinct hands-on coding challenges in {name} only. Never mention any other programming language.
Each question starts with "{prefix} ({name}) " and contains a problem statement, input/output, constraints, at least 2 examples and a starter signature in {name}.
Return ONLY a JSON array of {count} objects: [{{"question": "{prefix} ({name}) ...", "difficulty": "Easy|Medium|Hard"}}]"""


def _difficulty(raw) -> Optional[str]:
    value = str(raw.get("difficulty") if isinstance(raw, dict) else "").strip().lower()
    return value if value in ("easy", "medium", "hard") else None


def generate_bank(bank_key: str, verbal_count: int = 8, coding_count: int = 4) -> int:
    rows = []
    raw = _ask(BANK_VERBAL_PROMPT.format(count=verbal_count, key=bank_key), 1400)
    for item in raw if isinstance(raw, list) else []:
        text = _item_text(item)
        if text:
            rows.append({"kind": "verbal", "language": None, "difficulty": _difficulty(item), "question_text": text})
    if bank_key in SUPPORTED:
        raw = _ask(BANK_CODING_PROMPT.format(count=coding_count, name=LABELS[bank_key], prefix=CODING_PREFIX), 1600)
        for item in raw if isinstance(raw, list) else []:
            text = _item_text(item)
            if text and valid_coding(text, bank_key):
                rows.append({"kind": "coding", "language": bank_key, "difficulty": _difficulty(item), "question_text": text})
    store = get_store()
    saved = 0
    for row in rows:
        digest = text_hash(row["question_text"])
        if store.first("interview_question_bank", {"bank_key": bank_key, "kind": row["kind"], "text_hash": digest}, ["id"]):
            continue
        store.insert("interview_question_bank", {**row, "bank_key": bank_key, "text_hash": digest})
        saved += 1
    reset_bank_cache()
    if not saved and not rows:
        raise QuestionGenError("No bank questions were generated")
    return saved


def bank_size(bank_key: str) -> int:
    return sum(1 for r in bank_rows() if r["bank_key"] == bank_key and r["kind"] == "verbal")


def bank_keys_for(jd: str) -> list[str]:
    resolved = resolve_language({}, jd)
    keys = []
    if resolved["basis"] in ("jd", "family"):
        keys.append(resolved["language"])
    keys += [key for key, pattern in DOMAIN_PATTERNS.items() if _hits(pattern, jd or "") >= 1]
    return list(dict.fromkeys(keys or ["general"]))
