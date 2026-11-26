"""Document-based question extraction for assessments.

Parses DOCX and PDF question documents (e.g. the Bootcamp pre-assessment
sample) into a structured, admin-previewable question list.

Supported document shape (see Bootcamp_PreAssessment_Test_v3.docx):

    1. What is the output ... ? (Single choice)
    x = [1, 2, 3]
    A. [4]
    B. [1, 2, 3]
    C. Error
    D. [1, 2, 3, 4]
    Answer: D
    Explanation: ...

    2. Which ... ? (Multiple choice – select all that apply)
    A. ...
    B. ...
    Answer: A, B
    Explanation: ...

Extraction is intentionally conservative: a question is only emitted when it
has at least two options and an answer. The admin preview step (not automatic
publishing) lets a human review and correct extracted questions before they
are attached to an assessment.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("talentscope")

MAX_DOCUMENT_CHARS = 200_000
MAX_OPTIONS = 10

# A line that begins a numbered question: "1.", "2.", etc.
QUESTION_START_RE = re.compile(r"^(\d{1,4})\.\s+(.*)$", re.MULTILINE)
# An option line: "A. text", "B) text". Option labels are uppercase letters;
# lowercase (e.g. code such as ``y.append(4)``) must not be mistaken for one.
OPTION_RE = re.compile(r"^([A-Z])[\.)\-]\s*(.*)$")
ANSWER_RE = re.compile(r"answer\s*[:=]\s*(.+)", re.IGNORECASE)
EXPLANATION_RE = re.compile(r"explanation\s*[:=]\s*(.*)", re.IGNORECASE)
TYPE_MARKER_RE = re.compile(
    r"\((single choice|multiple choice[^)]*)\)", re.IGNORECASE
)
# Trailing section headers like "Section 1: Python Programming (17 questions)"
SECTION_HEADER_RE = re.compile(r"^\s*section\s+\d+[:\-]", re.IGNORECASE)


@dataclass
class ExtractedQuestion:
    question_number: int
    question_text: str
    question_type: str  # "single" | "multiple"
    options: list[str] = field(default_factory=list)
    correct_answers: list[int] = field(default_factory=list)  # option indices
    explanation: str = ""
    raw_answer: str = ""
    warnings: list[str] = field(default_factory=list)

    def is_valid(self) -> bool:
        return bool(self.question_text and len(self.options) >= 2 and self.correct_answers)

    def to_admin_row(self) -> dict:
        return {
            "questionNumber": self.question_number,
            "questionText": self.question_text,
            "questionType": self.question_type,
            "options": self.options,
            "correctAnswers": self.correct_answers,
            "explanation": self.explanation,
            "valid": self.is_valid(),
            "warnings": self.warnings,
        }


def _classify_type(raw: str, option_count: int) -> str:
    t = (raw or "").strip().lower()
    if "multiple" in t or "select all" in t:
        return "multiple"
    if "single" in t:
        return "single"
    # Default: single choice unless more than one correct answer was given.
    return "multiple" if len(option_count) > 1 else "single" if option_count else "single"


def _parse_answer_line(value: str, option_count: int) -> tuple[list[int], str]:
    """Parse "Answer: D" or "Answer: A, B, E" into zero-based indices."""
    indices: list[int] = []
    tokens = re.split(r"[,;/]", value)
    for token in tokens:
        token = token.strip().upper()
        if not token:
            continue
        if len(token) == 1 and "A" <= token <= "Z":
            idx = ord(token) - 65
            if 0 <= idx < option_count and idx not in indices:
                indices.append(idx)
        else:
            # Try a leading letter inside longer tokens (defensive).
            m = re.match(r"\s*([A-Z])\b", token)
            if m:
                idx = ord(m.group(1)) - 65
                if 0 <= idx < option_count and idx not in indices:
                    indices.append(idx)
    return indices, value


def _split_into_question_blocks(text: str) -> list[str]:
    """Split the document into per-question blocks using numbered question starts."""
    matches = list(QUESTION_START_RE.finditer(text))
    blocks = []
    for i, match in enumerate(matches):
        start = match.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        block = text[start:end].strip()
        if block:
            blocks.append(block)
    return blocks


def _parse_block(block: str) -> Optional[ExtractedQuestion]:
    lines = [ln.rstrip() for ln in block.splitlines()]
    if not lines:
        return None

    first = lines[0]
    m = QUESTION_START_RE.match(first)
    if not m:
        return None
    try:
        question_number = int(m.group(1))
    except ValueError:
        return None

    # Drop any section header that leaked into the first line.
    question_text = m.group(2).strip()
    if SECTION_HEADER_RE.match(question_text):
        question_text = ""

    question_type = "single"
    options: list[str] = []
    raw_answer = ""
    explanation = ""

    # Collect everything after the first line.
    rest = "\n".join(lines[1:])

    # Detect question type from a marker anywhere in the block.
    type_match = TYPE_MARKER_RE.search(block)
    if type_match:
        question_type = "multiple" if "multiple" in type_match.group(1).lower() else "single"

    # Walk remaining lines: accumulate question text until options begin.
    option_started = False
    for ln in lines[1:]:
        stripped = ln.strip()
        if not stripped:
            continue
        ans_m = ANSWER_RE.match(stripped)
        if ans_m:
            raw_answer = ans_m.group(1).strip()
            option_started = True
            continue
        expl_m = EXPLANATION_RE.match(stripped)
        if expl_m:
            explanation = expl_m.group(1).strip()
            option_started = True
            continue
        opt_m = OPTION_RE.match(stripped)
        if opt_m:
            option_started = True
            options.append(opt_m.group(2).strip())
            continue
        # Not an option/answer/explanation line: treat as question text (supports
        # multi-line questions and code snippets) until options have started.
        if not option_started:
            if question_text:
                question_text += " " + stripped
            else:
                question_text = stripped

    # Determine final type if not explicitly marked.
    if not type_match:
        question_type = "multiple" if len(options) and _looks_multi_answer(raw_answer, len(options)) else "single"

    correct_indices, _ = _parse_answer_line(raw_answer, len(options))

    warnings: list[str] = []
    if not question_text:
        warnings.append("No question text detected.")
    if len(options) < 2:
        warnings.append(f"Only {len(options)} option(s) detected; need at least 2.")
    if not correct_indices:
        warnings.append("No correct answer detected.")
    elif question_type == "single" and len(correct_indices) > 1:
        warnings.append("Multiple answers marked but type is single choice.")

    return ExtractedQuestion(
        question_number=question_number,
        question_text=question_text,
        question_type=question_type,
        options=options[:MAX_OPTIONS],
        correct_answers=correct_indices,
        explanation=explanation,
        raw_answer=raw_answer,
        warnings=warnings,
    )


def _looks_multi_answer(raw: str, option_count: int) -> bool:
    if not raw:
        return False
    parts = [p.strip().upper() for p in re.split(r"[,;/]", raw) if p.strip()]
    return len(parts) > 1


def _clean_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Collapse excessive blank lines but keep intra-question line structure.
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_questions_from_text(text: str) -> list[ExtractedQuestion]:
    """Extract questions from plain text (already decoded from DOCX/PDF)."""
    text = _clean_text(text)
    if not text:
        return []
    blocks = _split_into_question_blocks(text)
    questions: list[ExtractedQuestion] = []
    for block in blocks:
        q = _parse_block(block)
        if q is not None:
            questions.append(q)
    return questions


def parse_docx(data: bytes) -> str:
    """Extract plain text from a DOCX file."""
    try:
        from docx import Document
    except ImportError as err:  # pragma: no cover - dependency missing
        raise RuntimeError("python-docx is not installed") from err
    import io

    document = Document(io.BytesIO(data))
    parts: list[str] = []
    for para in document.paragraphs:
        if para.text:
            parts.append(para.text)
    # Tables: read cell text row by row (useful for question documents laid out in tables).
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def parse_pdf(data: bytes) -> str:
    """Extract plain text from a PDF file."""
    try:
        import pypdf
    except ImportError as err:  # pragma: no cover - dependency missing
        raise RuntimeError("pypdf is not installed") from err
    import io

    reader = pypdf.PdfReader(io.BytesIO(data))
    parts: list[str] = []
    for page in reader.pages:
        try:
            page_text = page.extract_text() or ""
        except Exception as err:  # pragma: no cover - per-page robustness
            log.warning("Failed to extract a PDF page: %s", err)
            page_text = ""
        if page_text:
            parts.append(page_text)
    return "\n".join(parts)


def parse_document(filename: str, data: bytes) -> str:
    """Dispatch to the right parser by file extension."""
    lower = (filename or "").lower()
    if lower.endswith(".docx"):
        return parse_docx(data)
    if lower.endswith(".doc"):
        raise ValueError(".doc files are not supported; please upload .docx instead.")
    if lower.endswith(".pdf"):
        return parse_pdf(data)
    raise ValueError(f"Unsupported file type: {filename}. Upload a .docx or .pdf.")


def extract_document_questions(filename: str, data: bytes) -> dict:
    """Full pipeline: parse document -> extract questions -> admin preview payload."""
    text = parse_document(filename, data)
    if len(text) > MAX_DOCUMENT_CHARS:
        text = text[:MAX_DOCUMENT_CHARS]
    questions = extract_questions_from_text(text)
    valid = [q for q in questions if q.is_valid()]
    return {
        "fileName": filename,
        "characters": len(text),
        "questionCount": len(questions),
        "validCount": len(valid),
        "questions": [q.to_admin_row() for q in questions],
    }


def questions_to_test_rows(
    test_id: str,
    questions: list[dict],
    topic_id: str,
    topic_title: str,
    default_difficulty: str = "medium",
    default_marks: Optional[int] = None,
) -> list[dict]:
    """Convert admin-approved extracted questions into test_questions rows."""
    rows = []
    difficulty_marks = {"beginner": 1, "intermediate": 2, "advanced": 3, "expert": 5}
    for i, q in enumerate(questions):
        difficulty = str(q.get("difficulty") or default_difficulty or "medium").strip().lower()
        marks = q.get("marks")
        if marks is None:
            marks = default_marks if default_marks is not None else difficulty_marks.get(difficulty, 2)
        correct = q.get("correctAnswers") or q.get("correct_option_index")
        if isinstance(correct, list):
            # Multiple choice: store the first correct index as primary plus the set.
            primary = correct[0] if correct else 0
        else:
            primary = correct if isinstance(correct, int) else 0
        rows.append({
            "test_id": test_id,
            "question_index": i,
            "question_text": str(q.get("questionText") or q.get("question_text") or ""),
            "options": q.get("options") or [],
            "correct_option_index": primary,
            "correct_answers": correct if isinstance(correct, list) else [primary] if primary is not None else [],
            "explanation": str(q.get("explanation") or ""),
            "difficulty": difficulty,
            "marks": int(marks),
            "topic_id": topic_id,
            "topic_title": topic_title or topic_id,
            "source": "document_upload",
        })
    return rows
