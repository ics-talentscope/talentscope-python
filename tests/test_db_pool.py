import sys
import threading
import time
import types

import pytest

from core.db import MssqlStore


class FakeOdbcError(Exception):
    pass


fake_pyodbc = types.SimpleNamespace(Error=FakeOdbcError)


class FakeCursor:
    description = [("ok",)]

    def __init__(self, conn):
        self.conn = conn
        self.rowcount = 1

    def execute(self, sql, params):
        self.conn.owner.in_use += 1
        self.conn.owner.peak = max(self.conn.owner.peak, self.conn.owner.in_use)
        time.sleep(0.02)
        if self.conn.owner.fail_once:
            self.conn.owner.fail_once = False
            self.conn.owner.in_use -= 1
            raise FakeOdbcError("08S01", "link failure")
        self.conn.owner.in_use -= 1

    def fetchall(self):
        return [(1,)]

    def close(self):
        pass


class FakeConn:
    def __init__(self, owner):
        self.owner = owner
        self.closed = False

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        self.closed = True


class Harness:
    def __init__(self):
        self.created = 0
        self.in_use = 0
        self.peak = 0
        self.fail_once = False


@pytest.fixture
def pooled(monkeypatch):
    monkeypatch.setitem(sys.modules, "pyodbc", fake_pyodbc)
    harness = Harness()
    store = MssqlStore({"pool_max": 3, "pool_wait_s": 1, "server": "x", "port": 1, "database": "d", "user": "u", "password": "p",
                        "encrypt": True, "trust_cert": True, "driver": "d"})

    def connect():
        harness.created += 1
        return FakeConn(harness)

    monkeypatch.setattr(store, "_connect", connect)
    return store, harness


def test_pool_caps_concurrent_connections_and_reuses_them(pooled):
    store, harness = pooled
    threads = [threading.Thread(target=store.query, args=("SELECT 1",)) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert harness.created <= 3 and harness.peak <= 3
    assert store.pool_stats()["inUse"] == 0


def test_broken_connection_is_discarded_and_retried(pooled):
    store, harness = pooled
    harness.fail_once = True
    assert store.query("SELECT 1")
    assert harness.created == 2 and store.pool_stats()["inUse"] == 0


def test_exhausted_pool_raises_instead_of_hanging(pooled):
    store, _ = pooled
    for _ in range(3):
        store.slots.acquire()
    with pytest.raises(RuntimeError):
        store.query("SELECT 1")


def test_connection_uses_unicode_encoding_for_wide_parameters(monkeypatch):
    class EncodingConn:
        def __init__(self):
            self.encodings = []

        def add_output_converter(self, *_args):
            pass

        def setencoding(self, **kwargs):
            self.encodings.append(kwargs)

    conn = EncodingConn()
    fake = types.SimpleNamespace(
        SQL_CHAR=1, SQL_VARCHAR=2, SQL_LONGVARCHAR=3, SQL_WCHAR=4,
        connect=lambda *_args, **_kwargs: conn,
    )
    monkeypatch.setitem(sys.modules, "pyodbc", fake)
    store = MssqlStore({
        "server": "x", "port": 1, "database": "d", "user": "u", "password": "p",
        "encrypt": True, "trust_cert": True, "driver": "d",
    })

    assert store._connect() is conn
    assert conn.encodings == [
        {"encoding": "utf-8", "ctype": fake.SQL_CHAR},
        {"encoding": "utf-16le", "ctype": fake.SQL_WCHAR},
    ]
