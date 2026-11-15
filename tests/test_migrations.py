"""검증 범위: DB 구조 버전(user_version) 관리와 저장 전 미리보기 보관 규칙.

임시 DB와 가상 자료만 사용합니다.
"""

import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

import migrations
from app import app, connect
from preview_store import PreviewStore


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.previous = dict(app.config)
        self.directory = tempfile.TemporaryDirectory()
        self.path = str(Path(self.directory.name) / "db.sqlite3")
        app.config.update(TESTING=True, DATABASE=self.path)

    def tearDown(self):
        app.config.clear()
        app.config.update(self.previous)
        self.directory.cleanup()

    def test_new_database_reaches_latest_version_with_all_tables(self):
        db = connect()
        try:
            self.assertEqual(migrations.current_version(db), migrations.LATEST_VERSION)
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master")}
            self.assertTrue({"imports", "transactions", "users", "transactions_import"} <= tables)
        finally:
            db.close()

    def test_pre_versioning_database_keeps_rows_and_is_upgraded(self):
        # 버전 관리 도입 전처럼 기본 테이블만 있고 user_version이 0인 DB를 만듭니다.
        old = sqlite3.connect(self.path)
        old.executescript("""
            CREATE TABLE imports (id INTEGER PRIMARY KEY, card TEXT NOT NULL, filename TEXT NOT NULL,
                digest TEXT NOT NULL, created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(card, digest));
            CREATE TABLE transactions (id INTEGER PRIMARY KEY, import_id INTEGER NOT NULL
                REFERENCES imports(id) ON DELETE CASCADE, used_on TEXT NOT NULL,
                merchant TEXT NOT NULL, amount INTEGER NOT NULL);
            INSERT INTO imports(id, card, filename, digest) VALUES (1, '신한', 'fixture.csv', 'x');
            INSERT INTO transactions VALUES (1, 1, '2026-01-02', '가상상점', 1000);
        """)
        old.close()
        db = connect()
        try:
            self.assertEqual(migrations.current_version(db), migrations.LATEST_VERSION)
            self.assertEqual(db.execute("SELECT owner_id FROM imports").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM transactions").fetchone()[0], 1)
        finally:
            db.close()

    def test_each_step_runs_once_and_new_steps_run_on_next_connection(self):
        calls = []
        steps = [("첫 단계", lambda db: calls.append(1))]
        db = sqlite3.connect(self.path)
        try:
            self.assertEqual(migrations.migrate(db, steps), 1)
            self.assertEqual(migrations.migrate(db, steps), 1)
            self.assertEqual(calls, [1])
            steps.append(("새 단계", lambda db: calls.append(2)))
            self.assertEqual(migrations.migrate(db, steps), 2)
            self.assertEqual(calls, [1, 2])
        finally:
            db.close()

    def test_failed_step_keeps_earlier_version_and_retries_later(self):
        state = {"fail": True}

        def flaky(db):
            if state["fail"]:
                raise RuntimeError("가상 실패")

        steps = [("첫 단계", lambda db: None), ("실패 단계", flaky)]
        db = sqlite3.connect(self.path)
        try:
            with self.assertRaises(RuntimeError):
                migrations.migrate(db, steps)
            self.assertEqual(migrations.current_version(db), 1)
            state["fail"] = False
            self.assertEqual(migrations.migrate(db, steps), 2)
        finally:
            db.close()

    def test_add_column_is_safe_to_repeat(self):
        db = sqlite3.connect(self.path)
        try:
            db.execute("CREATE TABLE fixture (id INTEGER PRIMARY KEY)")
            migrations.add_column(db, "fixture", "note", "TEXT NOT NULL DEFAULT ''")
            migrations.add_column(db, "fixture", "note", "TEXT NOT NULL DEFAULT ''")
            columns = [row[1] for row in db.execute("PRAGMA table_info(fixture)")]
            self.assertEqual(columns, ["id", "note"])
        finally:
            db.close()


class PreviewStoreTests(unittest.TestCase):
    def test_only_owner_can_read_and_expired_items_disappear(self):
        store = PreviewStore(ttl=0.05, per_owner=3, total=10)
        token = store.put(1, {"rows": [["가상"]]})
        self.assertIsNone(store.get_for(token, 2))
        self.assertEqual(store.get_for(token, 1)["rows"], [["가상"]])
        time.sleep(0.1)
        self.assertIsNone(store.get_for(token, 1))
        self.assertEqual(len(store), 0)

    def test_one_user_cannot_push_out_another_users_preview(self):
        store = PreviewStore(ttl=60, per_owner=2, total=10)
        partner = store.put(2, {})
        owner_tokens = [store.put(1, {}) for _ in range(5)]
        self.assertIsNotNone(store.get_for(partner, 2))
        self.assertEqual([t for t in owner_tokens if t in store], owner_tokens[-2:])

    def test_total_limit_drops_oldest_first(self):
        store = PreviewStore(ttl=60, per_owner=5, total=2)
        first, second, third = (store.put(n, {}) for n in (1, 2, 3))
        self.assertEqual(list(store), [second, third])


if __name__ == "__main__":
    unittest.main()
