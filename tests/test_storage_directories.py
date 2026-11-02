from services import docs_storage, structured_log, test_video


def test_read_only_storage_lookups_do_not_create_empty_directories(tmp_path, monkeypatch):
    monkeypatch.setattr(docs_storage, "UPLOAD_DIR", tmp_path)
    monkeypatch.setattr(structured_log, "UPLOAD_DIR", tmp_path)
    monkeypatch.setattr("services.storage.UPLOAD_DIR", tmp_path)

    assert docs_storage.list_files("BR") == []
    assert docs_storage.read_file("BR", "missing.txt") is None
    assert docs_storage.delete_file("BR", "missing.txt") is False
    assert not (tmp_path / "docs").exists()

    assert structured_log.read_logs() == []
    assert structured_log.raw_log("employee") is None
    structured_log.clear_all()
    assert not (tmp_path / "logs").exists()

    assert test_video._disk_list_ids() == set()
    assert not (tmp_path / test_video.BUCKET).exists()


def test_writing_document_creates_its_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(docs_storage, "UPLOAD_DIR", tmp_path)

    assert docs_storage.write_file("BR", "sample.txt", b"document") == "sample.txt"
    assert (tmp_path / "docs" / "BR" / "sample.txt").read_bytes() == b"document"
