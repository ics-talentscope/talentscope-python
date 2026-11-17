from scripts import bulk_tpm_upskilling
from core.db import Store


class RecordingStore(Store):
    def __init__(self, existing_test, questions=None, attempts=0):
        self.existing_test = existing_test
        self.questions = questions or []
        self.attempts = attempts
        self.inserts = []
        self.updates = []

    def first(self, table, where=None, columns=None, order=None):
        assert table == "tests"
        return self.existing_test

    def count(self, table, where=None) -> int:
        assert table == "test_attempts"
        return self.attempts

    def select(self, table, where=None, columns=None, order=None, limit=None, offset=None) -> list[dict]:
        assert table == "test_questions"
        return self.questions

    def insert(self, table, rows: dict | list[dict]) -> list[dict]:
        self.inserts.append((table, rows))
        return [rows] if isinstance(rows, dict) else rows

    def update(self, table, values, where) -> int:
        self.updates.append((table, values, where))
        return 1


def _account_uuid(monkeypatch):
    monkeypatch.setattr(bulk_tpm_upskilling.accounts, "employee_uuid", lambda _employee_id: "employee-uuid")


def _question(text):
    return {
        "question_text": text,
        "options": ["A", "B", "C", "D"],
        "correct_option_index": 0,
    }


def test_existing_completed_assessment_and_attempts_are_left_untouched(monkeypatch):
    _account_uuid(monkeypatch)
    store = RecordingStore({"id": "test-1", "status": "completed"}, attempts=1)

    result = bulk_tpm_upskilling.assign_assessment(
        store,
        {"employee_id": "E1"},
        [_question("Replacement question")],
    )

    assert result == "existing:test-1"
    assert store.inserts == []
    assert store.updates == []


def test_pending_assessment_only_appends_new_questions(monkeypatch):
    _account_uuid(monkeypatch)
    store = RecordingStore(
        {"id": "test-2", "status": "pending", "total_questions": 1},
        questions=[{"question_index": 0, "question_text": "Existing question"}],
    )

    result = bulk_tpm_upskilling.assign_assessment(
        store,
        {"employee_id": "E2"},
        [
            _question(" Existing   question "),
            _question("New question"),
            _question("New   question"),
        ],
    )

    assert result == "supplemented:test-2"
    assert len(store.inserts) == 1
    table, row = store.inserts[0]
    assert table == "test_questions"
    assert row["question_text"] == "New question"
    assert row["question_index"] == 1
    assert store.updates == [("tests", {"total_questions": 2}, {"id": "test-2"})]
