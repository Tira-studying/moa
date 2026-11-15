"""동일 승인 식별자의 금액 차이를 격리하고 이후 조회가 원장을 확정하지 못하게 확인합니다.

실제 금융 자료나 CODEF 요청은 사용하지 않습니다. 가상 응답의 순서·취소 표현과 관계없이
모호한 묶음은 한 검토 건으로 남기고, 독립된 정상 거래와 다른 계정의 원장은 보존합니다.
"""

from datetime import datetime
from itertools import permutations
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet
from werkzeug.security import generate_password_hash

from app import app, connect
from auth import set_password
import codef_sync as sync


AMOUNT_REASON = "동일 승인 식별자의 이용금액 차이 확인 필요"
DECLINED_REASON = "동일 승인 식별자의 승인·거절 상태 확인 필요"
HEADERS = {"X-Requested-With": "CardDashboard"}
SETTINGS = dict(
    environment="demo",
    client_id="fixture-id",
    client_secret="fixture-secret",
    public_key="fixture-public",
)


def fixture_row(**changes):
    """가맹점·식별자·금액은 이 테스트에서만 쓰는 가상 값입니다."""
    row = dict(
        resUsedDate=datetime.now(sync.KST).strftime("%Y%m%d"),
        resUsedTime="120000",
        resUsedAmount="2400",
        resCancelYN="0",
        resAccountCurrency="KRW",
        resMemberStoreName="가상 문구점",
        resCardNo="****0000",
        resApprovalNo="fixture-approval",
    )
    row.update(changes)
    return row


class CodefAmountConflictTests(unittest.TestCase):
    def setUp(self):
        self.previous = dict(app.config)
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        app.config.update(TESTING=True, PUBLIC_ORIGIN="", DATABASE=str(root / "db.sqlite3"))
        key = root / "fixture-key"
        key.write_bytes(Fernet.generate_key())
        self.environment = patch.dict(os.environ, {"MOA_CODEF_STORAGE_KEY_FILE": str(key)})
        self.environment.start()
        set_password(connect, "fixture-owner-password")
        self.execute(
            "INSERT INTO users(id,username,password_hash,role) VALUES(2,'partner',?,'manager')",
            (generate_password_hash("fixture-partner-password"),),
        )

    def tearDown(self):
        self.environment.stop()
        app.config.update(self.previous)
        self.directory.cleanup()

    def execute(self, sql, parameters=()):
        db = connect()
        try:
            with db:
                db.execute(sql, parameters)
        finally:
            db.close()

    def fetch(self, sql, parameters=()):
        db = connect()
        try:
            return db.execute(sql, parameters).fetchall()
        finally:
            db.close()

    def normalized(self, rows):
        day = datetime.now(sync.KST).strftime("%Y%m%d")
        return sync.normalize(list(rows), "samsung", day, day)

    def persist(self, rows, owner=1):
        """검증 완료 뒤 운영 코드와 같이 한 트랜잭션으로 원장과 출처를 저장합니다."""
        items = self.normalized(rows)
        db = connect()
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                return sync.store_events(db, owner, "samsung", items)
        finally:
            db.close()

    def event(self, owner=1):
        rows = self.fetch("SELECT * FROM codef_events WHERE owner_id=?", (owner,))
        self.assertEqual(len(rows), 1)
        return dict(rows[0])

    def ledger(self, owner=1):
        return [
            dict(row)
            for row in self.fetch(
                "SELECT t.* FROM transactions t JOIN imports i ON i.id=t.import_id "
                "WHERE i.owner_id=? ORDER BY t.id",
                (owner,),
            )
        ]

    def assert_quarantined(self, item, reason=AMOUNT_REASON):
        self.assertEqual(item["quarantined"], 1)
        self.assertEqual(item["kind"], "mixed")
        self.assertIsNone(item["net"])
        self.assertEqual(item["reason"], reason)

    def amount_conflict(self):
        return [fixture_row(), fixture_row(resUsedAmount="4800", resCancelYN="1")]

    def test_amount_variations_and_known_statuses_are_order_independent_unknown_amount(self):
        self.assertEqual(sync.NORMALIZATION_VERSION, 3)
        original = self.normalized([fixture_row()])[0]
        for kind in ("0", "1", "2", "3"):
            for amount in ("4800", "-4800", "0"):
                expected = None
                for ordered in permutations(
                    [fixture_row(), fixture_row(resUsedAmount=amount, resCancelYN=kind)]
                ):
                    with self.subTest(kind=kind, amount=amount, order=ordered[0]["resUsedAmount"]):
                        items = self.normalized(ordered)
                        self.assertEqual(len(items), 1)
                        self.assert_quarantined(items[0])
                        self.assertEqual(items[0]["original_amount"], 0)
                        self.assertEqual(items[0]["fingerprint"], original["fingerprint"])
                        if expected is not None:
                            self.assertEqual(items[0], expected)
                        expected = items[0]

    def test_three_or_more_rows_cannot_replace_unknown_amount_or_choose_decline_reason_by_order(
        self,
    ):
        rows = [
            fixture_row(),
            fixture_row(resUsedAmount="-4800", resCancelYN="1"),
            fixture_row(resUsedAmount="0", resCancelYN="2"),
            fixture_row(resCancelYN="3"),
        ]
        expected = None
        for ordered in permutations(rows):
            item = self.normalized(ordered)[0]
            self.assert_quarantined(item)
            self.assertEqual(item["original_amount"], 0)
            if expected is not None:
                self.assertEqual(item, expected)
            expected = item

    def test_same_amount_declined_conflict_is_quarantined_and_same_amount_cancel_behavior_remains(
        self,
    ):
        for kind in ("0", "1", "2"):
            for ordered in permutations(
                [fixture_row(resCancelYN=kind), fixture_row(resCancelYN="3")]
            ):
                self.assert_quarantined(self.normalized(ordered)[0], DECLINED_REASON)
        for ordered in permutations(
            [fixture_row(), fixture_row(resCancelYN="1", resUsedAmount="-2400")]
        ):
            item = self.normalized(ordered)[0]
            self.assertEqual(item["kind"], "mixed")
            self.assertIsNone(item["net"])
            self.assertEqual(item["reason"], "동일 승인·취소 묶음 확인 필요")
            self.assertEqual(item["quarantined"], 0)

    def test_quarantine_stays_review_through_all_later_statuses_without_creating_ledger(self):
        self.persist(self.amount_conflict())
        initial = self.event()
        for later in (
            fixture_row(),
            fixture_row(resUsedAmount="4800"),
            fixture_row(resCancelYN="1"),
            fixture_row(resCancelYN="2", resCancelAmount="600"),
            fixture_row(resCancelYN="3"),
            fixture_row(resCancelYN="1", resUsedAmount="-4800"),
        ):
            with self.subTest(later=later["resCancelYN"], amount=later["resUsedAmount"]):
                self.assertFalse(self.persist([later]))
                event = self.event()
                self.assert_quarantined(event)
                self.assertEqual(event["state"], "review")
                self.assertEqual(event["id"], initial["id"])
                self.assertIsNone(event["transaction_id"])
                self.assertEqual(self.ledger(), [])

    def test_existing_ledger_amount_and_pointer_are_preserved_after_amount_quarantine(self):
        self.persist([fixture_row()])
        self.persist([fixture_row(resCancelYN="2", resCancelAmount="600")])
        previous = self.ledger()
        pointer = self.event()["transaction_id"]
        self.assertEqual(previous[0]["amount"], 1800)
        self.persist(self.amount_conflict())
        for rows in (
            [fixture_row()],
            [fixture_row(resCancelYN="1")],
            [fixture_row(resCancelYN="2", resUsedAmount="4800", resCancelAmount="600")],
            [fixture_row(resCancelYN="3")],
        ):
            self.persist(rows)
            self.assert_quarantined(self.event())
            self.assertEqual(self.event()["transaction_id"], pointer)
            self.assertEqual(self.ledger(), previous)

    def test_later_single_different_amount_is_sticky_not_automatically_reconciled(self):
        self.persist([fixture_row()])
        previous = self.ledger()
        self.assertTrue(self.persist([fixture_row(resUsedAmount="4800")]))
        self.assert_quarantined(self.event())
        for amount in ("2400", "4800", "0", "7200"):
            self.persist([fixture_row(resUsedAmount=amount)])
            self.assert_quarantined(self.event())
            self.assertEqual(self.ledger(), previous)

    def test_approval_decline_quarantine_remains_unknown_through_later_responses(self):
        self.persist([fixture_row()])
        previous = self.ledger()
        self.persist([fixture_row(), fixture_row(resCancelYN="3")])
        self.assert_quarantined(self.event(), DECLINED_REASON)
        for later in (
            fixture_row(),
            fixture_row(resCancelYN="1"),
            fixture_row(resCancelYN="2", resCancelAmount="600"),
            fixture_row(resCancelYN="3"),
            fixture_row(resUsedAmount="4800"),
        ):
            self.assertFalse(self.persist([later]))
            self.assert_quarantined(self.event(), DECLINED_REASON)
            self.assertEqual(self.event()["state"], "review")
            self.assertEqual(self.ledger(), previous)

    def test_unrelated_normal_group_saves_once_while_quarantined_group_is_idempotent(self):
        rows = self.amount_conflict() + [
            fixture_row(
                resApprovalNo="fixture-independent", resUsedTime="120100", resUsedAmount="3600"
            )
        ]
        self.assertTrue(self.persist(rows))
        self.assertEqual(len(self.ledger()), 1)
        self.assertEqual(self.ledger()[0]["amount"], 3600)
        self.assertEqual(
            len(self.fetch("SELECT * FROM codef_events WHERE state='review' AND quarantined=1")), 1
        )
        self.assertFalse(self.persist(list(reversed(rows))))
        self.assertEqual(len(self.ledger()), 1)
        self.assertEqual(len(self.fetch("SELECT * FROM codef_events")), 2)

    def test_automatic_decline_ignore_can_become_review_but_manual_ignore_stays_ignored(self):
        self.persist([fixture_row(resCancelYN="3")])
        old = self.event()
        self.assertEqual(
            (old["state"], old["kind"], old["reason"], old["transaction_id"]),
            ("ignored", "3", "", None),
        )
        self.persist(self.amount_conflict())
        self.assert_quarantined(self.event())
        self.assertEqual(self.event()["state"], "review")
        self.execute("UPDATE codef_events SET state='ignored' WHERE owner_id=1")
        before = self.event()
        self.assertFalse(self.persist([fixture_row(resCancelYN="3")]))
        self.assertFalse(self.persist([fixture_row(resUsedAmount="7200")]))
        self.assertEqual(self.event(), before)
        self.assertEqual(self.ledger(), [])

    def test_automatic_decline_then_single_approval_is_conflict_review_without_spending(self):
        self.persist([fixture_row(resCancelYN="3")])
        self.persist([fixture_row()])
        self.assert_quarantined(self.event(), DECLINED_REASON)
        self.assertEqual(self.event()["state"], "review")
        self.assertEqual(self.ledger(), [])
        self.assertFalse(self.persist([fixture_row()]))

    def test_deleted_canonical_ledger_is_not_restored_from_quarantined_snapshots(self):
        self.persist([fixture_row()])
        self.persist(self.amount_conflict())
        self.execute("DELETE FROM imports WHERE owner_id=1")
        self.assertIsNone(self.event()["transaction_id"])
        for later in (
            fixture_row(),
            fixture_row(resCancelYN="1"),
            fixture_row(resUsedAmount="4800"),
        ):
            self.persist([later])
            self.assert_quarantined(self.event())
            self.assertEqual(self.ledger(), [])

    def test_owner_quarantine_does_not_change_other_owners_same_identity(self):
        self.persist([fixture_row()], owner=2)
        partner_ledger = self.ledger(2)
        self.persist(self.amount_conflict())
        self.persist([fixture_row(resUsedAmount="4800", resCancelYN="1")])
        self.assert_quarantined(self.event())
        self.assertEqual(self.ledger(), [])
        self.assertEqual(self.ledger(2), partner_ledger)
        self.assertEqual(self.event(2)["quarantined"], 0)
        self.assertEqual(self.event(2)["state"], "saved")

    def test_malformed_rows_or_different_merchant_still_abort_whole_batch(self):
        for bad in (
            fixture_row(resUsedAmount="not-money"),
            fixture_row(resCancelYN="4"),
            fixture_row(resUsedTime="250000"),
            fixture_row(resMemberStoreName="다른 가상 문구점", resUsedAmount="4800"),
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(sync.DataReviewError):
                    self.persist(
                        self.amount_conflict()
                        + [bad, fixture_row(resApprovalNo="fixture-independent")]
                    )
                self.assertEqual(self.ledger(), [])
                self.assertEqual(len(self.fetch("SELECT * FROM codef_events")), 0)

    def test_api_review_never_saves_unknown_amount_and_ignore_is_stable_and_private(self):
        sync.save_link(connect, 1, "samsung", "fixture-connected-id", SETTINGS, verifying=False)
        self.persist(self.amount_conflict())
        owner, partner = app.test_client(), app.test_client()
        owner.post("/api/login", json={"password": "fixture-owner-password"}, headers=HEADERS)
        partner.post(
            "/api/login",
            json={"username": "partner", "password": "fixture-partner-password"},
            headers=HEADERS,
        )
        result = owner.get("/api/codef-sync")
        self.assertEqual(result.json["links"][0]["review_count"], 1)
        self.assertEqual(len(result.json["review"]), 1)
        self.assertIsNone(result.json["review"][0]["net"])
        self.assertEqual(result.json["review"][0]["kind"], "mixed")
        event_id = result.json["review"][0]["id"]
        other = partner.get("/api/codef-sync")
        self.assertEqual(other.json["review"], [])
        self.assertNotIn("가상 문구점", other.get_data(as_text=True))
        self.assertEqual(
            partner.post(
                f"/api/codef-sync/review/{event_id}",
                json={"action": "ignore"},
                headers={**HEADERS, "If-Match": other.headers["X-Moa-Revision"]},
            ).status_code,
            404,
        )
        revision = owner.get("/api/codef-sync").headers["X-Moa-Revision"]
        self.assertEqual(
            owner.post(
                f"/api/codef-sync/review/{event_id}",
                json={"action": "save"},
                headers={**HEADERS, "If-Match": revision},
            ).status_code,
            400,
        )
        self.assertEqual(self.ledger(), [])
        revision = owner.get("/api/codef-sync").headers["X-Moa-Revision"]
        self.assertEqual(
            owner.post(
                f"/api/codef-sync/review/{event_id}",
                json={"action": "ignore"},
                headers={**HEADERS, "If-Match": revision},
            ).status_code,
            200,
        )
        before = self.event()
        self.persist([fixture_row()])
        self.assertEqual(self.event(), before)
        self.assertEqual(owner.get("/api/codef-sync").json["review"], [])

    def test_schema_adds_quarantine_flag_without_changing_legacy_event_and_is_repeatable(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        try:
            db.executescript(
                "CREATE TABLE users(id INTEGER PRIMARY KEY);"
                "CREATE TABLE transactions(id INTEGER PRIMARY KEY);"
                "CREATE TABLE codef_events("
                "id INTEGER PRIMARY KEY,owner_id INTEGER NOT NULL,card TEXT NOT NULL,"
                "fingerprint TEXT NOT NULL,used_on TEXT NOT NULL,merchant TEXT NOT NULL,"
                "original_amount INTEGER NOT NULL,net INTEGER,kind TEXT NOT NULL,"
                "state TEXT NOT NULL,reason TEXT NOT NULL DEFAULT '',transaction_id INTEGER,"
                "UNIQUE(owner_id,card,fingerprint));"
                "INSERT INTO codef_events VALUES(1,1,'samsung','fixture-hmac','2026-01-01',"
                "'가상 이전 가맹점',2400,NULL,'2','review','가상 이전 사유',NULL);"
            )
            before = dict(db.execute("SELECT * FROM codef_events").fetchone())
            sync.schema(db)
            sync.schema(db)
            after = dict(db.execute("SELECT * FROM codef_events").fetchone())
            self.assertEqual(after.pop("quarantined"), 0)
            self.assertEqual(after, before)
            column = next(
                row
                for row in db.execute("PRAGMA table_info(codef_events)")
                if row[1] == "quarantined"
            )
            self.assertEqual(str(column[4]), "0")
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
