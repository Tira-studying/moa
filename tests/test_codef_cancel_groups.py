"""가상 승인·취소 묶음을 순서와 무관하게 보류하고 확정된 원장 금액을 보존하는지 확인합니다.

실제 카드 응답·승인번호·가맹점·금액은 사용하지 않습니다. 같은 식별자의 취소 표현을
임의 합산하지 않고 다른 정상 지출은 저장하며, 재조회가 취소를 되돌리지 못하게 합니다.
"""

from datetime import datetime
from itertools import permutations
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet
from werkzeug.security import generate_password_hash

from app import app, connect
from auth import set_password
import codef_sync as sync


def fixture_row(**changes):
    """숫자와 가맹점은 이 테스트에서만 사용하는 가상 값입니다."""
    row = dict(
        resUsedDate=datetime.now(sync.KST).strftime("%Y%m%d"),
        resUsedTime="120000",
        resUsedAmount="2400",
        resCancelYN="0",
        resAccountCurrency="KRW",
        resMemberStoreName="가상 식당",
        resCardNo="****0000",
        resApprovalNo="fixture-approval",
    )
    row.update(changes)
    return row


class CodefCancelGroupTests(unittest.TestCase):
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
        db = connect()
        try:
            with db:
                db.execute(
                    "INSERT INTO users(id,username,password_hash,role) VALUES(2,'partner',?,'manager')",
                    (generate_password_hash("fixture-partner-password"),),
                )
        finally:
            db.close()

    def tearDown(self):
        self.environment.stop()
        app.config.update(self.previous)
        self.directory.cleanup()

    def normalized(self, rows):
        day = datetime.now(sync.KST).strftime("%Y%m%d")
        return sync.normalize(list(rows), "samsung", day, day)

    def persist(self, rows, owner=1):
        """운영 수집과 같이 목록 검증을 끝내고 하나의 쓰기 저장 단위에서 반영합니다."""
        items = self.normalized(rows)
        db = connect()
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                return sync.store_events(db, owner, "samsung", items)
        finally:
            db.close()

    def fetch(self, sql, parameters=()):
        db = connect()
        try:
            return db.execute(sql, parameters).fetchall()
        finally:
            db.close()

    def amount(self, owner=1):
        rows = self.fetch(
            "SELECT t.amount FROM transactions t JOIN imports i ON i.id=t.import_id "
            "WHERE i.owner_id=? ORDER BY t.id",
            (owner,),
        )
        self.assertEqual(len(rows), 1)
        return rows[0][0]

    def test_positive_or_negative_full_cancel_pair_is_one_order_independent_review_group(self):
        original = self.normalized([fixture_row()])[0]
        for amount in ("2400", "-2400"):
            expected = None
            for rows in permutations(
                [fixture_row(), fixture_row(resCancelYN="1", resUsedAmount=amount)]
            ):
                items = self.normalized(rows)
                self.assertEqual(len(items), 1)
                item = items[0]
                self.assertEqual(item["fingerprint"], original["fingerprint"])
                self.assertEqual(item["kind"], "mixed")
                self.assertIsNone(item["net"])
                self.assertEqual(item["reason"], "동일 승인·취소 묶음 확인 필요")
                if expected is not None:
                    self.assertEqual(item, expected)
                expected = item

    def test_three_or_more_conflicting_rows_remain_mixed_in_every_order(self):
        rows = [
            fixture_row(),
            fixture_row(resCancelYN="1", resUsedAmount="-2400"),
            fixture_row(resCancelYN="2", resCancelAmount="600"),
            fixture_row(resCancelYN="2"),
        ]
        expected = None
        for ordered in permutations(rows):
            items = self.normalized(ordered)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["kind"], "mixed")
            self.assertIsNone(items[0]["net"])
            if expected is not None:
                self.assertEqual(items[0], expected)
            expected = items[0]

    def test_multiple_partial_cancels_are_not_summed_or_selected_by_order(self):
        rows = [fixture_row(resCancelYN="2", resCancelAmount=value) for value in ("600", "1200")]
        for ordered in permutations(rows):
            item = self.normalized(ordered)[0]
            self.assertEqual(item["kind"], "mixed")
            self.assertIsNone(item["net"])

    def test_uncertain_currency_or_sign_is_never_changed_into_a_spending_amount(self):
        for second in (
            fixture_row(resCancelYN="1", resAccountCurrency="USD"),
            fixture_row(resCancelYN="0", resUsedAmount="-2400"),
            fixture_row(resCancelYN="2", resUsedAmount="-2400", resCancelAmount="600"),
        ):
            self.assertIsNone(self.normalized([fixture_row(), second])[0]["net"])

    def test_different_merchants_still_reject_whole_batch(self):
        for conflict in (
            fixture_row(resMemberStoreName="다른 가상 식당", resCancelYN="1"),
            fixture_row(resMemberStoreName="다른 가상 식당", resCancelYN="3"),
        ):
            for ordered in permutations([fixture_row(), conflict]):
                with self.assertRaises(sync.DataReviewError):
                    self.persist(ordered)
                self.assertEqual(len(self.fetch("SELECT * FROM transactions")), 0)
        for ordered in permutations(
            [
                fixture_row(),
                fixture_row(resCancelYN="1"),
                fixture_row(resMemberStoreName="다른 가상 식당"),
            ]
        ):
            with self.assertRaises(sync.DataReviewError):
                self.normalized(ordered)

    def test_malformed_row_still_prevents_unrelated_rows_from_being_saved(self):
        with self.assertRaises(sync.DataReviewError):
            self.persist(
                [fixture_row(), fixture_row(resApprovalNo="fixture-other", resUsedAmount="bad")]
            )
        self.assertEqual(len(self.fetch("SELECT * FROM transactions")), 0)

    def test_other_valid_group_saves_once_while_cancel_group_remains_review(self):
        rows = [
            fixture_row(),
            fixture_row(resCancelYN="1", resUsedAmount="-2400"),
            fixture_row(resApprovalNo="fixture-other", resUsedTime="120100"),
        ]
        self.assertTrue(self.persist(rows))
        self.assertEqual(self.amount(), 2400)
        self.assertEqual(len(self.fetch("SELECT * FROM codef_events WHERE state='review'")), 1)
        self.assertEqual(len(self.fetch("SELECT * FROM codef_events WHERE state='saved'")), 1)
        self.assertFalse(self.persist(list(reversed(rows))))
        self.assertEqual(self.amount(), 2400)
        self.assertEqual(len(self.fetch("SELECT * FROM codef_events")), 2)

    def test_existing_approval_and_cancel_pair_keep_original_amount_until_review(self):
        self.persist([fixture_row()])
        self.persist([fixture_row(), fixture_row(resCancelYN="1", resUsedAmount="-2400")])
        self.assertEqual(self.amount(), 2400)
        event = self.fetch("SELECT * FROM codef_events")[0]
        self.assertEqual(event["state"], "review")
        self.assertEqual(event["kind"], "mixed")
        self.assertIsNotNone(event["transaction_id"])
        self.assertIsNone(event["net"])

    def test_full_cancel_then_mixed_then_approval_cannot_reopen_spending(self):
        self.persist([fixture_row()])
        self.persist([fixture_row(resCancelYN="1")])
        self.assertEqual(self.amount(), 0)
        self.persist([fixture_row(), fixture_row(resCancelYN="1", resUsedAmount="-2400")])
        self.assertEqual(self.amount(), 0)
        self.persist([fixture_row()])
        self.assertEqual(self.amount(), 0)
        event = self.fetch("SELECT * FROM codef_events")[0]
        self.assertEqual(event["state"], "review")
        self.assertEqual(event["reason"], "기존 원장보다 금액이 증가해 확인 필요")
        self.assertFalse(self.persist([fixture_row()]))
        self.assertEqual(self.amount(), 0)

    def test_partial_cancel_never_increases_canonical_amount_after_incomplete_snapshot(self):
        self.persist([fixture_row()])
        self.persist([fixture_row(resCancelYN="2", resCancelAmount="600")])
        self.assertEqual(self.amount(), 1800)
        self.persist([fixture_row(), fixture_row(resCancelYN="2", resCancelAmount="600")])
        self.persist([fixture_row()])
        self.assertEqual(self.amount(), 1800)
        self.persist([fixture_row(resCancelYN="2", resCancelAmount="1200")])
        self.assertEqual(self.amount(), 1200)
        self.persist([fixture_row(resCancelYN="2", resCancelAmount="300")])
        self.assertEqual(self.amount(), 1200)
        self.assertEqual(self.fetch("SELECT state FROM codef_events")[0][0], "review")

    def test_guard_uses_canonical_ledger_amount_instead_of_event_previous_net(self):
        self.persist([fixture_row()])
        db = connect()
        try:
            with db:
                db.execute("UPDATE transactions SET amount=500")
        finally:
            db.close()
        self.persist([fixture_row(resCancelYN="2", resCancelAmount="600")])
        self.assertEqual(self.amount(), 500)
        self.assertEqual(self.fetch("SELECT state FROM codef_events")[0][0], "review")

    def test_each_owner_has_independent_ledger_and_review_groups(self):
        self.persist([fixture_row()])
        self.persist([fixture_row(resCancelYN="1")])
        self.persist([fixture_row()], owner=2)
        self.assertEqual(self.amount(1), 0)
        self.assertEqual(self.amount(2), 2400)
        self.persist([fixture_row()])
        self.assertEqual(self.amount(1), 0)
        self.assertEqual(self.amount(2), 2400)

    def test_wrong_owner_transaction_pointer_cannot_modify_other_owners_amount(self):
        self.persist([fixture_row()])
        self.persist([fixture_row()], owner=2)
        db = connect()
        try:
            with db:
                other = db.execute(
                    "SELECT transaction_id FROM codef_events WHERE owner_id=2"
                ).fetchone()[0]
                db.execute("UPDATE codef_events SET transaction_id=? WHERE owner_id=1", (other,))
        finally:
            db.close()
        self.persist([fixture_row(resCancelYN="2", resCancelAmount="600")])
        self.assertEqual(self.amount(2), 2400)
        event = self.fetch("SELECT * FROM codef_events WHERE owner_id=1")[0]
        self.assertEqual(event["state"], "review")
        self.assertIsNone(event["transaction_id"])

    def test_shared_codef_source_uses_neutral_label_and_retains_user_names(self):
        self.persist([fixture_row()])
        month = datetime.now(sync.KST).strftime("%Y-%m")
        self.assertEqual(self.fetch("SELECT filename FROM imports")[0][0], "CODEF 조회 · " + month)
        self.persist([fixture_row()], owner=2)
        db = connect()
        try:
            with db:
                db.execute(
                    "UPDATE imports SET filename=?",
                    ("CODEF 자동 수집 · " + month,),
                )
        finally:
            db.close()
        self.persist([fixture_row(resApprovalNo="fixture-other")])
        rows = self.fetch("SELECT owner_id,filename FROM imports ORDER BY owner_id")
        self.assertEqual(tuple(rows[0]), (1, "CODEF 조회 · " + month))
        self.assertEqual(tuple(rows[1]), (2, "CODEF 자동 수집 · " + month))
        db = connect()
        try:
            with db:
                db.execute("UPDATE imports SET filename='나의 가상 조회' WHERE owner_id=1")
        finally:
            db.close()
        self.persist([fixture_row(resApprovalNo="fixture-third")])
        self.assertEqual(
            self.fetch("SELECT filename FROM imports WHERE owner_id=1")[0][0], "나의 가상 조회"
        )

    def test_generic_registration_error_does_not_claim_bad_credentials(self):
        settings = dict(
            environment="demo",
            client_id="fixture-id",
            client_secret="fixture-secret",
            public_key="fixture-public",
        )
        sync.save_link(connect, 1, "samsung", "fixture-connected", settings, verifying=False)
        link = dict(self.fetch("SELECT * FROM codef_links")[0])
        client = Mock()
        client.credentials.return_value = (settings, "fixture-token")
        client.send.return_value = {"result": {"code": "CF-04000"}, "data": {}}
        sync.collect(connect, client, link, "fixture-registration-error")
        stored = self.fetch("SELECT state,error_code FROM codef_links")[0]
        self.assertEqual(tuple(stored), ("error", "CF-04000"))
        self.assertEqual(len(self.fetch("SELECT * FROM transactions")), 0)
        self.assertEqual(client.send.call_count, 1)


if __name__ == "__main__":
    unittest.main()
