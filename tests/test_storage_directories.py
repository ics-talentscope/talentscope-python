from services import docs_storage, structured_log, test_video


def test_read_only_storage_lookups_do_not_create_empty_directories(tmp_path, monkeypatch):
    monkeypatch.setattr(structured_log, "UPLOAD_DIR", tmp_path)
    monkeypatch.setattr("services.storage.UPLOAD_DIR", tmp_path)

    assert docs_storage.list_files("BR") == []
    assert docs_storage.read_file("BR", "missing.txt") is None
    assert docs_storage.delete_file("BR", "missing.txt") is False
    assert list(tmp_path.iterdir()) == []

    assert structured_log.read_logs() == []
    assert structured_log.raw_log("employee") is None
    structured_log.clear_all()
    assert not (tmp_path / "logs").exists()

    assert test_video._disk_list_ids() == set()
    assert list(tmp_path.iterdir()) == []


def test_writing_document_creates_its_directory(tmp_path, monkeypatch):
    monkeypatch.setattr("services.storage.UPLOAD_DIR", tmp_path)

    assert docs_storage.write_file("BR", "sample.txt", b"document") == "sample.txt"
    assert (tmp_path / "doc-br-sample.txt").read_bytes() == b"document"
    assert docs_storage.list_files("BR") == ["sample.txt"]


def test_storage_is_flat_with_descriptive_names(tmp_path, monkeypatch):
    from core import tenancy
    from services import storage

    monkeypatch.setattr(storage, "UPLOAD_DIR", tmp_path)
    monkeypatch.setattr(storage, "_sql_ok", lambda: False)
    storage.save("recordings", "abc.webm", b"v")
    storage.save("verifications", "abc_id.jpg", b"i")
    storage.save("employee_test_recordings", "t1.webm", b"t")
    with tenancy.client_context("slalom"):
        storage.save("resumes", "abc-cv.pdf", b"r")

    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "interview-recording-abc.webm", "slalom-resume-abc-cv.pdf",
        "test-recording-t1.webm", "verification-abc_id.jpg",
    ]
    assert [row["bucket"] for row in storage.usage_summary()] == ["recordings", "employee_test_recordings", "verifications"] or len(storage.usage_summary()) == 3
    assert storage.names("recordings") == ["abc.webm"]
    assert storage.names("resumes") == []


def test_backup_keeps_only_latest_and_submit(tmp_path, monkeypatch, store):
    from services import storage, test_backup

    monkeypatch.setattr(storage, "UPLOAD_DIR", tmp_path)
    monkeypatch.setattr(storage, "_sql_ok", lambda: False)
    store.insert("tests", {"id": "11111111-1111-4111-8111-111111111111", "status": "in_progress"})
    for event in ("proctor", "proctor", "video", "submit", "proctor"):
        test_backup.snapshot("11111111-1111-4111-8111-111111111111", event)

    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "backup-11111111-1111-4111-8111-111111111111-latest.json",
        "backup-11111111-1111-4111-8111-111111111111-submit.json",
    ]


def test_stale_video_chunks_are_dropped(tmp_path, monkeypatch):
    import os
    import time

    from services import storage

    monkeypatch.setattr(storage, "UPLOAD_DIR", tmp_path)
    old = storage.disk_path("video_upload_chunks/t1", "0.part")
    fresh = storage.disk_path("video_upload_chunks/t2", "0.part")
    old.write_bytes(b"x")
    fresh.write_bytes(b"x")
    os.utime(old, (time.time() - 48 * 3600,) * 2)

    assert storage.drop_stale("video_upload_chunks", 24) == 1
    assert not old.exists() and fresh.exists()
