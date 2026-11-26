import json
import logging
import random
from functools import lru_cache
from pathlib import Path

from services import llm

DATA = Path(__file__).resolve().parents[1] / "data" / "learning_fallback.json"
log = logging.getLogger("talentscope")

STEMS = [
    "What is the primary objective of {topic} in the context of {subject}?",
    "Which of the following is a key advantage of utilizing {topic} over traditional methods?",
    "What is a potential risk or bottleneck when applying {topic} to large-scale systems?",
    "In {subject}, how does {topic} handle unexpected input anomalies?",
    "Which tool or framework is commonly associated with implementing {topic}?",
    "What is the theoretical foundation behind {topic} design principles?",
    "How is the efficiency of {topic} typically measured in production?",
    "Which best practice is recommended when configuring {topic} parameters?",
    "What type of data representation is optimal for {topic} processing?",
    "Under what condition does {topic} perform most optimally?",
    "What is the impact of dimensionality or scale on {topic} execution?",
    "How does {topic} interact with other components in a {subject} pipeline?",
    "Which algorithm or technique is a core component of {topic}?",
    "What is the primary difference between a simple and advanced implementation of {topic}?",
    "How does {topic} ensure model generalization or robustness?",
    "Which metric is most suitable for validating the output of {topic}?",
    "What is a common misconception about {topic} in the industry?",
    "How does resource allocation affect the performance of {topic}?",
    "Which mathematical concept is fundamental to {topic}?",
    "What is the future trend or research focus in the field of {topic}?",
]
CORRECT = [
    "To establish structured patterns, reduce bias, and ensure execution consistency.",
    "Enhanced adaptability to complex patterns and improved overall efficiency.",
    "Increased latency or memory footprint during peak workload conditions.",
    "By applying filtering mechanisms, normalization, or default fallbacks.",
    "Industry-standard packages and integrated development frameworks.",
    "Rigorous mathematical logic and empirical validation models.",
    "Through latency metrics, precision scores, and throughput optimization.",
    "Conducting hyperparameter search and validating on isolated test sets.",
    "Normalized vectors, relational schemas, or structured feature maps.",
    "When the input distribution closely aligns with the training assumptions.",
    "It can cause sparseness or increased complexity, requiring dimensionality reduction.",
    "It acts as a feature extractor or decision node in the data pipeline.",
    "Iterative optimization, gradient-based search, or rule-based matching.",
    "The inclusion of non-linear mappings and deeper parameter spaces.",
    "By using cross-validation, regularization, and dropout techniques.",
    "Mean squared error, classification accuracy, or custom utility functions.",
    "That it guarantees perfect accuracy without requiring high-quality input data.",
    "Insufficient hardware capacity can cause execution timeouts or failures.",
    "Linear algebra, probability theory, or multi-variable calculus.",
    "Improving explainability, reducing latency, and enhancing zero-shot capabilities.",
]
DISTRACTORS = [
    ["To write temporary log files that are automatically deleted after 24 hours.",
     "To customize the visual color themes of the administrator dashboard.",
     "To run background network speed tests during peak system traffic."],
    ["It eliminates the need for any programming language or compiler.",
     "It reduces the physical power consumption of the server to zero.",
     "It allows users to bypass authorization rules completely."],
    ["The system UI automatically reverting to default browser settings.",
     "A lack of documentation in the developer community repositories.",
     "The server requiring physical hardware reboot after each run."],
    ["By shutting down the active execution process immediately.",
     "By emailing the system administrator a notification check.",
     "By converting the data into unformatted binary strings."],
    ["Basic word processors and spreadsheet document applications.",
     "Legacy file transfer protocols and storage tape devices.",
     "Static web browsers running on outdated mobile hardware."],
]


def map_difficulty(diff) -> str:
    d = str(diff).lower().strip()
    if d in ("basic", "easy", "beginner"):
        return "beginner"
    if d in ("medium", "intermediate"):
        return "intermediate"
    if d in ("high", "hard", "advanced"):
        return "advanced"
    if d == "expert":
        return "expert"
    return "intermediate"


@lru_cache(maxsize=1)
def _banks() -> dict:
    return json.loads(DATA.read_text(encoding="utf-8"))


def fallback_questions(topic_title: str) -> list[dict]:
    title = (topic_title or "").lower()
    banks = _banks()
    if "history" in title:
        key = "history"
    elif "search" in title or "bfs" in title or "dfs" in title or "a*" in title or "agent" in title:
        key = "search_algorithm"
    elif "adversarial" in title or "minimax" in title:
        key = "adversarial"
    elif "logical" in title or "knowledge representation" in title or "inference" in title:
        key = "logical"
    elif "expert" in title:
        key = "expert_system"
    else:
        key = "generic"
    return [dict(q) for q in banks[key]]


def template_question(subject: str, topic: str, index: int, difficulty: str) -> dict:
    stem_index = index % len(STEMS)
    fill = lambda text: text.replace("{topic}", topic).replace("{subject}", subject)
    options = [fill(CORRECT[stem_index]), *[fill(d) for d in DISTRACTORS[index % len(DISTRACTORS)]]]
    correct_index = (index * 7) % 4
    if correct_index:
        options[0], options[correct_index] = options[correct_index], options[0]
    return {
        "question": f"{fill(STEMS[stem_index])} (Ref: {index + 1})",
        "options": options,
        "correctIndex": correct_index,
        "explanation": (f"This question (Reference ID: {index + 1}) tests your understanding of {topic} concepts within {subject}. "
                        "The correct answer was chosen because it correctly describes the core architectural, performance, or theoretical characteristics under standard conditions."),
        "difficulty": map_difficulty(difficulty),
    }


def build_databank(subject: str, topic: str, difficulty: str) -> list[dict]:
    bank = [{**q, "difficulty": map_difficulty(q.get("difficulty"))} for q in fallback_questions(topic)]
    while len(bank) < 100:
        n = len(bank)
        bank.append(template_question(subject, topic, n, "easy" if n % 3 == 0 else "medium" if n % 3 == 1 else "hard"))
    return bank


def fetch_questions(subject: str, topic: str, difficulty: str, count: int = 10) -> list[dict]:
    bank = build_databank(subject, topic, difficulty)
    random.shuffle(bank)
    return bank[:count]


def generate_ai_questions(subject: str, topic: str, difficulty: str, count: int = 10) -> list[dict]:
    source_context_block = ""
    try:
        from services import topic_sources
        src = topic_sources.get_topic_source(topic, "", subject, difficulty)
        explanation = src.get("explanation", "").strip()
        if explanation:
            source_context_block = f"""
Reference Source Material (What must be tested):
\"\"\"
{explanation}
\"\"\"
IMPORTANT: Every generated question must be directly related to the facts, milestones, and concepts covered in the Reference Source Material above.
"""
    except Exception as err:
        log.warning("Could not attach topic source material to prompt: %s", err)

    prompt = f"""Create exactly {count} original multiple-choice assessment questions for an employee learning quiz.
Subject: {subject}
Topic: {topic}
Difficulty: {difficulty}
{source_context_block}
Requirements:
- Assess understanding of the named topic and the provided source material at the requested difficulty.
- Each question must have exactly four distinct, plausible answer options.
- Exactly one option must be correct; do not make the answer obvious from wording.
- Include a concise explanation of why the correct option is right.
- Return only a JSON array. Each item must have this shape:
  {{"question":"...","options":["...","...","...","..."],"correctIndex":0,"explanation":"...","difficulty":"{difficulty}"}}
"""
    try:
        generated = llm.complete_json(
            prompt,
            temperature=0.3,
            max_tokens=3072,
            timeout_s=180,
            priority=llm.PRIORITY_LIVE,
        )
        if isinstance(generated, dict):
            generated = generated.get("questions")
        if not isinstance(generated, list) or len(generated) < count:
            raise llm.LlmError(f"Local LLM returned fewer than {count} complete questions.")

        questions = []
        for item in generated[:count]:
            if not isinstance(item, dict):
                raise llm.LlmError("Local LLM returned a question with an invalid shape.")
            question = str(item.get("question") or item.get("question_text") or "").strip()
            options = item.get("options")
            correct_index = item.get("correctIndex", item.get("correct_option_index"))
            if (
                not question
                or not isinstance(options, list)
                or len(options) != 4
                or any(not isinstance(option, str) or not option.strip() for option in options)
                or len({option.strip().casefold() for option in options}) != 4
                or isinstance(correct_index, bool)
                or not isinstance(correct_index, int)
                or not 0 <= correct_index < 4
            ):
                raise llm.LlmError("Local LLM returned an invalid multiple-choice question.")
            questions.append({
                "question": question,
                "options": [option.strip() for option in options],
                "correctIndex": correct_index,
                "explanation": str(item.get("explanation") or "").strip(),
                "difficulty": map_difficulty(item.get("difficulty") or difficulty),
            })
        return questions
    except llm.LlmError as err:
        log.warning("AI question generation unavailable; using the built-in topic question fallback: %s", err)
        return fetch_questions(subject, topic, difficulty, count)


_DIFFICULTY_DEFAULT_MARKS = {"beginner": 1, "intermediate": 2, "advanced": 3, "expert": 5}


def _default_marks(difficulty: str) -> int:
    return _DIFFICULTY_DEFAULT_MARKS.get((difficulty or "intermediate").strip().lower(), 2)


def to_rows(test_id: str, generated: list[dict], topic_id: str, topic_title: str, default_difficulty: str) -> list[dict]:
    rows = []
    for i, q in enumerate(generated):
        correct = q.get("correctIndex", q.get("correct_option_index", 0))
        difficulty = map_difficulty(q.get("difficulty") or default_difficulty)
        rows.append({
            "test_id": test_id, "question_index": i, "question_text": q["question"], "options": q["options"],
            "correct_option_index": correct, "explanation": q.get("explanation") or "",
            "difficulty": difficulty, "marks": int(q.get("marks") or _default_marks(difficulty)),
            "topic_id": topic_id, "topic_title": topic_title or topic_id,
        })
    return rows
