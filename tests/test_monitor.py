import json

from services import monitor



def test_vm_target_reads_collector_snapshot(store, tmp_path, monkeypatch):
    (tmp_path / "snapshot.json").write_text(json.dumps({"hostname": "vm1"}))
    (tmp_path / "history.json").write_text(json.dumps([{"t": 1}]))
    monkeypatch.setenv("MONITOR_DIR", str(tmp_path))
    monitor._cache.clear()
    body = monitor.collect("vm")
    assert body["collector"]["available"] and not body["collector"]["stale"]
    assert body["snapshot"] == {"hostname": "vm1"} and body["history"] == [{"t": 1}]
    assert {"llm", "traffic", "dbPool"} <= set(body["runtime"])


def test_vm_target_reports_missing_collector(store, tmp_path, monkeypatch):
    monkeypatch.setenv("MONITOR_DIR", str(tmp_path / "missing"))
    monitor._cache.clear()
    body = monitor.collect("vm")
    assert body["collector"]["available"] is False and body["collector"]["stale"] is True


def test_db_target_without_mssql_is_not_configured(store):
    monitor._cache.clear()
    assert monitor.collect("db")["configured"] is False


def test_db_sampler_skips_server_metrics_without_server_permissions(store, monkeypatch, caplog):
    seen = []

    def fake_rows(query, params=()):
        seen.append(query)
        if "HAS_PERMS_BY_NAME" in query:
            return [{"viewServerState": 0, "viewServerPerformanceState": 0}]
        if "SELECT 1 AS ok" in query:
            return [{"ok": 1}]
        if "sys.database_files" in query:
            return [{"dataMb": 100, "dataUsedMb": 50, "logMb": 20, "logUsedMb": 2}]
        raise AssertionError(f"Unexpected SQL query: {query}")

    monkeypatch.setattr(monitor, "rows", fake_rows)
    monkeypatch.setattr(monitor, "get_store", lambda: store)
    monkeypatch.setitem(monitor._sampler_permissions, "warned", False)

    point = monitor.db_point()

    assert point["down"] == 0
    assert point["dataMb"] == 100
    assert not any(query in (monitor.COUNTERS_SQL, monitor.CPU_HISTORY_SQL) for query in seen)
    assert "lacks VIEW SERVER STATE" in caplog.text


def test_monitor_requires_admin_and_valid_target(client):
    assert client.get("/api/admin/monitor?target=vm").status_code in (401, 403)
