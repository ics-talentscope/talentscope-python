import pytest

from services import document_questions, employee_tests as et, assessment_attempts
from services import accounts


def seed_employee(store, employee_id="EMP1", email="emp1@infinite.com", full_name="Emp One"):
    row = {"id": "e1-uuid", "employee_id": employee_id, "email": email, "full_name": full_name,
           "department": "eng", "role": "employee"}
    store.seed("employees", [row])
    return row


def seed_test(store, test_id="t1", topic_id="topic-python", employee_uuid="e1-uuid",
              employee_code="EMP1", total_questions=2, pass_threshold=80):
    now = "2026-11-23T10:00:00+00:00"
    row = {
        "id": test_id, "employee_id": employee_uuid, "employee_code": employee_code,
        "topic_id": topic_id, "topic_title": "Python Assessment", "subject_id": "subj",
        "subject_title": "Python", "difficulty": "medium",
        "total_questions": total_questions, "time_limit_seconds": 1800,
        "status": "pending", "current_question_index": 0,
        "pass_threshold": pass_threshold, "created_at": now,
    }
    store.seed("tests", [row])
    return row


def seed_questions(store, test_id="t1"):
    now = "2026-11-23T10:00:00+00:00"
    rows = [
        {"id": "q1", "test_id": test_id, "question_index": 0, "question_text": "Q1 basic?",
         "options": ["A", "B", "C", "D"], "correct_option_index": 0,
         "explanation": "e1", "difficulty": "beginner", "marks": 1,
         "topic_id": "topic-python", "topic_title": "Python Assessment", "created_at": now},
        {"id": "q2", "test_id": test_id, "question_index": 1, "question_text": "Q2 complex?",
         "options": ["A", "B", "C", "D"], "correct_option_index": 2,
         "explanation": "e2", "difficulty": "advanced", "marks": 5,
         "topic_id": "topic-python", "topic_title": "Python Assessment", "created_at": now},
    ]
    store.seed("test_questions", rows)
    return rows


# ---------------------------------------------------------------------------
# Document parsing
# ---------------------------------------------------------------------------

SAMPLE = """
Section 1: Python Programming (2 questions)
1. What is the output of print(10 // 3)? (Single choice)
A. 3.33
B. 3
C. Error
D. 4
Answer: B
Explanation: // is floor division, so 10 // 3 = 3.

2. Which Python types are mutable? (Multiple choice – select all that apply)
A. list
B. tuple
C. dict
D. str
Answer: A, C
Explanation: list and dict are mutable; tuple and str are immutable.
"""


def test_document_extraction_single_and_multiple(store):
    questions = document_questions.extract_questions_from_text(SAMPLE)
    assert len(questions) == 2
    q1, q2 = questions
    assert q1.question_number == 1
    assert q1.question_type == "single"
    assert q1.options == ["3.33", "3", "Error", "4"]
    assert q1.correct_answers == [1]
    assert "floor division" in q1.explanation
    assert q1.is_valid()

    assert q2.question_number == 2
    assert q2.question_type == "multiple"
    assert q2.options == ["list", "tuple", "dict", "str"]
    assert q2.correct_answers == [0, 2]
    assert q2.is_valid()


def test_document_extraction_invalid_without_answer(store):
    text = "1. Something?\nA. x\nB. y\n"
    questions = document_questions.extract_questions_from_text(text)
    assert len(questions) == 1
    assert not questions[0].is_valid()
    assert any("No correct answer" in w for w in questions[0].warnings)


# ---------------------------------------------------------------------------
# Weighted scoring + pass/fail
# ---------------------------------------------------------------------------

def test_weighted_scoring_pass_and_fail(store, monkeypatch):
    monkeypatch.setenv("SAML_ENABLED", "0")
    seed_employee(store)
    seed_test(store, total_questions=2, pass_threshold=80)
    seed_questions(store)

    # Q1 (1 mark) correct, Q2 (5 marks) wrong => 1/6 = 16% -> FAIL
    result = et.finalize(
        et.get_store().first("tests", {"id": "t1"}),
        "e1-uuid",
        [{"question_id": "q1", "selected_index": 0, "time_seconds": 5},
         {"question_id": "q2", "selected_index": 1, "time_seconds": 5}],
        auto=False,
    )
    assert result["passed"] is False
    assert result["accuracy"] == 17  # round(1/6*100)
    assert result["correct_marks"] == 1
    assert result["total_marks"] == 6

    test = et.get_store().first("tests", {"id": "t1"})
    assert test["passed"] is False
    assert test["pass_threshold"] == 80
    assert test["total_marks"] == 6
    assert test["correct_marks"] == 1

    # Now both correct => 6/6 = 100% -> PASS
    result2 = et.finalize(
        et.get_store().first("tests", {"id": "t1"}),
        "e1-uuid",
        [{"question_id": "q1", "selected_index": 0, "time_seconds": 5},
         {"question_id": "q2", "selected_index": 2, "time_seconds": 5}],
        auto=False,
    )
    assert result2["passed"] is True
    assert result2["accuracy"] == 100
    assert result2["correct_marks"] == 6


def test_multiple_correct_mcqs(store, monkeypatch):
    monkeypatch.setenv("SAML_ENABLED", "0")
    seed_employee(store)
    seed_test(store, total_questions=1, pass_threshold=80)
    now = "2026-11-23T10:00:00+00:00"
    store.seed("test_questions", [
        {"id": "qm", "test_id": "t1", "question_index": 0, "question_text": "MCQ?",
         "options": ["A", "B", "C", "D"], "correct_option_index": 0,
         "correct_answers": [0, 2], "explanation": "A and C are correct",
         "difficulty": "beginner", "marks": 2,
         "topic_id": "topic-python", "topic_title": "Python Assessment", "created_at": now},
    ])

    # Selecting one of the two correct answers counts as correct (2/2 = 100%).
    result = et.finalize(
        et.get_store().first("tests", {"id": "t1"}),
        "e1-uuid",
        [{"question_id": "qm", "selected_index": 2, "time_seconds": 5}],
        auto=False,
    )
    assert result["passed"] is True
    assert result["accuracy"] == 100
    assert result["correct_marks"] == 2

    # Selecting a wrong answer => 0/2 = 0% -> FAIL.
    result2 = et.finalize(
        et.get_store().first("tests", {"id": "t1"}),
        "e1-uuid",
        [{"question_id": "qm", "selected_index": 1, "time_seconds": 5}],
        auto=False,
    )
    assert result2["passed"] is False
    assert result2["accuracy"] == 0


# ---------------------------------------------------------------------------
# Attempt management
# ---------------------------------------------------------------------------

def test_attempt_retake_rule(store):
    seed_employee(store)
    emp_uuid = "e1-uuid"
    topic_id = "topic-python"

    # No attempt yet: can start.
    allowed, reason = assessment_attempts.can_start_assessment(emp_uuid, topic_id)
    assert allowed is True and reason is None

    # Start attempt #1.
    a1 = assessment_attempts.start_attempt(emp_uuid, "EMP1", topic_id, "t1")
    assert a1["attemptNumber"] == 1
    assert a1["status"] == "in_progress"
    assert a1["isRetakeAuthorized"] is False

    # While in progress, can resume (still allowed).
    allowed, _ = assessment_attempts.can_start_assessment(emp_uuid, topic_id)
    assert allowed is True

    # Complete attempt #1 with a score.
    assessment_attempts.complete_attempt("t1", 50.0, "completed")

    # Now completed and not authorized: cannot start again.
    allowed, reason = assessment_attempts.can_start_assessment(emp_uuid, topic_id)
    assert allowed is False
    assert "retake requires administrator" in reason

    # Authorize retake.
    assert assessment_attempts.authorize_retake(a1["id"], "admin@infinite.com") is True

    # Now can start attempt #2.
    allowed, _ = assessment_attempts.can_start_assessment(emp_uuid, topic_id)
    assert allowed is True
    a2 = assessment_attempts.start_attempt(emp_uuid, "EMP1", topic_id, "t1")
    assert a2["attemptNumber"] == 2

    attempts = assessment_attempts.list_attempts(emp_uuid, topic_id)
    assert len(attempts) == 2
    assert attempts[0]["attemptNumber"] == 2
