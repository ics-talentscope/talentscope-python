import json
import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Optional

DATA_PATH = Path(__file__).resolve().parents[1] / "data" / "topic_sources.json"
log = logging.getLogger("talentscope")

ACRONYMS = {
    "ai": "AI",
    "ml": "ML",
    "ds": "Data Science",
    "dl": "Deep Learning",
    "nlp": "NLP",
    "cv": "Computer Vision",
    "genai": "Generative AI",
    "sql": "SQL",
    "llm": "LLM",
    "mlops": "MLOps",
    "bfs": "BFS",
    "dfs": "DFS",
    "a*": "A*",
    "eda": "EDA",
    "rag": "RAG",
    "rl": "Reinforcement Learning",
    "rlhf": "RLHF",
    "gan": "GAN",
    "vae": "VAE",
    "cnn": "CNN",
    "rnn": "RNN",
    "lstm": "LSTM",
    "bert": "BERT",
    "gpt": "GPT",
    "api": "API",
    "rest": "REST",
    "ci": "CI",
    "cd": "CD",
    "iam": "IAM",
    "aws": "AWS",
    "gcp": "GCP",
    "azure": "Azure",
}


@lru_cache(maxsize=1)
def _load_sources() -> dict:
    try:
        if DATA_PATH.exists():
            return json.loads(DATA_PATH.read_text(encoding="utf-8"))
    except Exception as err:
        log.error("Failed to load topic_sources.json: %s", err)
    return {}


def format_title_case(text: str) -> str:
    """Format any raw slug or title into grammatically proper Title Case with acronym preservation."""
    raw = (text or "").strip()
    if not raw:
        return "Core Concepts"
    # Replace dashes and underscores with spaces if this looks like a slug
    if "-" in raw or "_" in raw:
        raw = raw.replace("-", " ").replace("_", " ")

    words = raw.split()
    formatted = []
    lower_articles = {"a", "an", "the", "in", "on", "at", "for", "to", "by", "and", "or", "of", "vs"}

    for i, w in enumerate(words):
        clean_word = re.sub(r"[^\w*]", "", w).lower()
        if clean_word in ACRONYMS:
            # Preserve special acronym case
            formatted.append(ACRONYMS[clean_word])
        elif i > 0 and clean_word in lower_articles:
            formatted.append(w.lower())
        else:
            formatted.append(w.capitalize())

    return " ".join(formatted)


def _slugify(text: str) -> str:
    return "-".join("".join(c if c.isalnum() else " " for c in text.lower()).split())


def _fallback_source_for(topic_id: str, topic_title: str, subject_id: str, subject_title: str, difficulty: str) -> dict:
    # Always ensure clean, Title Case, human-readable titles — NEVER use raw slugs like 'ml-intermediate'
    clean_title = format_title_case(topic_title if topic_title and topic_title != topic_id else topic_id)
    clean_subject = format_title_case(subject_title if subject_title else "Software Engineering")
    slug = _slugify(clean_title)

    explanation = (
        f"{clean_title} is an indispensable discipline within {clean_subject}, providing both the foundational concepts "
        f"and engineering frameworks necessary to build resilient, production-ready solutions. "
        f"In real-world applications, understanding how to apply {clean_title} allows developers and researchers to decompose "
        f"intricate challenges into predictable, maintainable components.\n\n"
        f"To understand how this operates in practice, consider an enterprise software platform handling high volumes of user traffic. "
        f"Applying the principles of {clean_title} enables the architecture to balance computational throughput against memory overhead, "
        f"isolate potential failure modes before they propagate through the stack, and ensure that operational metrics—such as latency, "
        f"accuracy, and fault tolerance—consistently meet rigorous production standards.\n\n"
        f"Furthermore, mastery of {clean_title} requires balancing essential engineering trade-offs. "
        f"Practitioners must determine when to deploy lightweight heuristics versus computationally intensive algorithms, "
        f"how to structure data pipelines to avoid bottlenecks, and which automated validation suites to employ for long-term reliability.\n\n"
        f"For assessment and certification, candidates should focus on core mechanisms, defining formulas, and diagnostic best practices. "
        f"Understanding edge cases, failure recovery strategies, and industry design standards ensures that you can design, debug, "
        f"and optimize systems with technical rigor and confidence."
    )

    words = len(explanation.split())
    return {
        "topic_id": topic_id,
        "topic_title": clean_title,
        "subject_id": subject_id,
        "subject_title": clean_subject,
        "difficulty": difficulty or "intermediate",
        "estimated_minutes": 25,
        "word_count": words,
        "key_points": [
            f"Understand the fundamental definitions, theoretical principles, and operational goals of {clean_title}.",
            f"Analyze critical engineering trade-offs between computational throughput, memory complexity, and latency.",
            f"Evaluate architectural patterns, validation strategies, and data representations used across {clean_subject}.",
            f"Identify common failure modes, anomaly mitigation strategies, and industry best practices."
        ],
        "explanation": explanation,
        "sources": [
            {
                "name": "GeeksforGeeks",
                "title": f"{clean_title} - Tutorial & Concepts",
                "url": f"https://www.geeksforgeeks.org/{slug}/",
                "description": f"Educational guide covering core definitions, architectural patterns, and practical code examples for {clean_title}."
            },
            {
                "name": "Wikipedia",
                "title": f"{clean_title} - Overview",
                "url": f"https://en.wikipedia.org/wiki/{clean_title.replace(' ', '_')}",
                "description": f"Authoritative reference documenting theoretical foundations, historical context, and formal taxonomies of {clean_title}."
            }
        ]
    }


def get_topic_source(topic_id_or_title: str, subject_id: str = "", subject_title: str = "", difficulty: str = "intermediate") -> dict:
    data = _load_sources()
    raw = (topic_id_or_title or "").strip()
    needle = raw.lower()

    # Exact match on key
    if needle in data:
        return data[needle]

    # Partial / title match
    for k, v in data.items():
        if k.lower() == needle or v.get("topic_title", "").lower() == needle:
            return v

    # Check key without intro/prefix
    clean_needle = needle.replace("ai-intro-", "ai-").replace("-intro-", "-")
    if clean_needle in data:
        return data[clean_needle]

    # Fuzzy matches for specific topics
    if "history" in needle:
        if "ai-intro-history" in data:
            return data["ai-intro-history"]
    if "adversarial" in needle or "minimax" in needle:
        if "ai-intro-adversarial" in data:
            return data["ai-intro-adversarial"]
    if "search" in needle and ("agent" in needle or "bfs" in needle or "dfs" in needle or "a*" in needle):
        if "ai-intro-agents" in data:
            return data["ai-intro-agents"]
    if "logic" in needle or "inference" in needle or "knowledge representation" in needle:
        if "ai-intro-logic" in data:
            return data["ai-intro-logic"]
    if "expert" in needle:
        if "ai-intro-expert" in data:
            return data["ai-intro-expert"]

    return _fallback_source_for(
        topic_id=raw,
        topic_title=raw,
        subject_id=subject_id,
        subject_title=subject_title,
        difficulty=difficulty
    )
