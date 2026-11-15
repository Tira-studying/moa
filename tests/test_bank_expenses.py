"""가상 은행·카드 내역으로 명시적 반영, 늦은 중복 보류, 개인 권한과 복원을 검증합니다."""

from datetime import date, datetime, timedelta
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
import uuid
from unittest.mock import patch
from zoneinfo import ZoneInfo

from werkzeug.security import generate_password_hash
from app import app, connect
from auth import set_password
import bank_expenses as expenses
import codef_bank
import codef_sync
import finance

HEADERS = {"X-Requested-With": "CardDashboard"}


class BankExpenseTests(unittest.TestCase):
    def setUp(self):
        self.old = dict(app.config)
        self.root = tempfile.TemporaryDirectory()
        app.config.update(
            TESTING=True, PUBLIC_ORIGIN="", DATABASE=str(Path(self.root.name) / "data.sqlite3")
        )
        self.day = datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat()
        self.month = self.day[:7]
        set_password(connect, "fixture-owner-password")
        db = connect()
        with db:
            db.execute(
                "INSERT INTO users(id,username,password_hash,role) VALUES(2,'partner',?,'manager')",
                (generate_password_hash("fixture-partner-password"),),
            )
            for owner in (1, 2):
                db.execute(
                    "INSERT INTO bank_links(id,owner_id,account_tag,cipher,app_digest) VALUES(?,?,?,?,?)",
                    (
                        f"fixture-link-{owner}",
                        owner,
                        "fixture-tag",
                        "fixture-cipher",
                        "fixture-digest",
                    ),
                )
        db.close()
        self.identifier = self.bank_row()
        self.other_identifier = self.bank_row(owner=2)
        self.owner = self.login()
        self.partner = self.login(partner=True)

    def tearDown(self):
        app.config.update(self.old)
        self.root.cleanup()

    def login(self, partner=False):
        client = app.test_client()
        response = client.post(
            "/api/login",
            json={
                "username": "partner" if partner else "owner",
                "password": "fixture-partner-password" if partner else "fixture-owner-password",
            },
            headers=HEADERS,
        )
        self.assertEqual(response.status_code, 200)
        return client

    def bank_row(
        self, owner=1, outgoing=1200, incoming=0, used_on=None, description="가상 은행 설명"
    ):
        db = connect()
        with db:
            identifier = db.execute(
                "INSERT INTO bank_transactions(link_id,fingerprint,used_on,used_time,incoming,outgoing,balance,description) VALUES(?,?,?,'120000',?,?,5000,?)",
                (
                    f"fixture-link-{owner}",
                    uuid.uuid4().hex,
                    used_on or self.day,
                    incoming,
                    outgoing,
                    description,
                ),
            ).lastrowid
        db.close()
        return identifier

    def card(
        self,
        owner=1,
        amount=1200,
        used_on=None,
        merchant="가상 카드 가맹점",
        excluded=False,
        background=False,
    ):
        db = connect()
        with db:
            source = db.execute(
                "INSERT INTO imports(card,filename,digest,owner_id) VALUES('신한','가상 명세서',?,?)",
                (uuid.uuid4().hex, owner),
            ).lastrowid
            identifier = db.execute(
                "INSERT INTO transactions(import_id,used_on,merchant,amount) VALUES(?,?,?,?)",
                (source, used_on or self.day, merchant, amount),
            ).lastrowid
            if excluded:
                db.execute(
                    "INSERT INTO transaction_exclusions(transaction_id) VALUES(?)", (identifier,)
                )
            if background:
                codef_sync.touch_revision(db, owner)
        db.close()
        return identifier

    def rows(self, sql, args=()):
        db = connect()
        try:
            return [dict(row) for row in db.execute(sql, args)]
        finally:
            db.close()

    def detail(self, identifier=None, client=None):
        response = (client or self.owner).get(
            f"/api/bank/transactions/{identifier or self.identifier}/review"
        )
        self.assertEqual(response.status_code, 200)
        return response

    def save(self, decision, identifier=None, client=None, **changes):
        client = client or self.owner
        response = self.detail(identifier, client)
        payload = dict(
            decision=decision,
            merchant="가상 계좌 식당",
            category="식비",
            memo="가상 개인 메모",
            candidate_token=response.json["candidate_token"],
            confirm_separate=False,
        )
        payload.update(changes)
        return client.put(
            f"/api/bank/transactions/{identifier or self.identifier}/review",
            json=payload,
            headers={**HEADERS, "If-Match": response.headers["X-Moa-Revision"]},
        )

    def ledger(self):
        return self.owner.get("/api/data").json["transactions"]

    def active_total(self):
        return sum(row["amount"] for row in self.ledger() if not row["excluded"])

    def select(self, included, identifier=None, **changes):
        """사용처·분야 입력 없이 선택만 저장하며 목록의 버전과 후보를 함께 확인합니다."""
        response = self.detail(identifier)
        payload = dict(included=included, candidate_token=response.json["candidate_token"])
        payload.update(changes)
        return self.owner.put(
            f"/api/bank/transactions/{identifier or self.identifier}/consumption",
            json=payload,
            headers={**HEADERS, "If-Match": response.headers["X-Moa-Revision"]},
        )

    def test_unreviewed_and_non_expense_decisions_never_add_consumption(self):
        response = self.owner.get("/api/bank?month=" + self.month)
        self.assertEqual(response.json["review_summary"]["pending_count"], 0)
        self.assertEqual(response.json["review_summary"]["unselected_count"], 1)
        self.assertFalse(response.json["transactions"][0]["selected"])
        self.assertEqual(len(response.json["transactions"]), 1)
        with patch.object(
            codef_sync.Client, "send", side_effect=AssertionError("unexpected API request")
        ):
            for decision in ("card_payment", "transfer", "topup", "not_expense", "pending"):
                self.assertEqual(self.save(decision).status_code, 200)
                self.assertEqual(self.ledger(), [])
        self.assertEqual(len(self.rows("SELECT * FROM bank_transactions")), 2)
        self.assertEqual(self.rows("SELECT * FROM imports"), [])
        self.assertEqual(
            self.owner.get("/api/bank?month=" + self.month).json["review_summary"]["pending_count"],
            0,
        )

    def test_expense_confirmation_is_idempotent_and_separate_from_billing(self):
        for _ in range(2):
            self.assertEqual(self.save("expense").status_code, 200)
        rows = self.ledger()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["payment_kind"], "bank")
        self.assertEqual(rows[0]["card"], expenses.PAYMENT_LABEL)
        self.assertEqual(rows[0]["category"], "식비")
        self.assertTrue(rows[0]["category_reviewed"])
        self.assertEqual(self.active_total(), 1200)
        self.assertEqual(len(self.rows("SELECT * FROM imports")), 1)
        self.assertEqual(len(self.rows("SELECT * FROM bank_expense_reviews")), 1)
        bills = self.owner.get("/api/billing?month=" + self.month).json
        self.assertNotIn(expenses.PAYMENT_LABEL, [row["card"] for row in bills["cards"]])
        raw = self.rows("SELECT * FROM bank_transactions WHERE id=?", (self.identifier,))[0]
        db = connect()
        with db:
            codef_bank.store(
                db,
                "fixture-link-1",
                [
                    (
                        raw["fingerprint"],
                        raw["used_on"],
                        raw["used_time"],
                        0,
                        1200,
                        5000,
                        "가상 설명 수정",
                    )
                ],
            )
            codef_sync.touch_revision(db, 1)
        db.close()
        self.assertEqual(len(self.ledger()), 1)
        self.assertEqual(self.ledger()[0]["merchant"], "가상 계좌 식당")

    def test_simple_selection_only_adds_chosen_withdrawals_and_leaves_category_unreviewed(self):
        untouched = self.bank_row(outgoing=3400)
        with patch.object(
            codef_sync.Client, "send", side_effect=AssertionError("unexpected API request")
        ):
            for _ in range(2):
                self.assertEqual(self.select(True).status_code, 200)
        record = self.ledger()[0]
        self.assertEqual(record["merchant"], "가상 은행 설명")
        self.assertEqual(record["category"], "미분류")
        self.assertFalse(record["category_reviewed"])
        self.assertEqual(self.active_total(), 1200)
        self.assertEqual(len(self.ledger()), 1)
        self.assertEqual(self.owner.get("/api/category-reviews").json["pending_count"], 1)
        self.assertFalse(self.detail(untouched).json["transaction"]["needs_review"])
        summary = self.owner.get("/api/bank?month=" + self.month).json["review_summary"]
        self.assertEqual(
            (summary["selected_count"], summary["unselected_count"], summary["pending_count"]),
            (1, 1, 0),
        )
        self.assertEqual(self.select(False).status_code, 200)
        self.assertEqual(self.active_total(), 0)
        self.assertEqual(self.owner.get("/api/category-reviews").json["pending_count"], 0)
        self.assertFalse(self.detail().json["transaction"]["selected"])
        self.assertEqual(self.select(True).status_code, 200)
        self.assertEqual(self.ledger()[0]["id"], record["id"])
        self.assertFalse(self.ledger()[0]["category_reviewed"])

    def test_simple_reselection_preserves_all_edited_fields_and_category_confirmation(self):
        self.assertEqual(self.save("expense").status_code, 200)
        original = self.ledger()[0]
        db = connect()
        with db:
            db.execute(
                "UPDATE transaction_annotations SET actual_merchant=? WHERE transaction_id=?",
                ("가상 수정 사용처", original["id"]),
            )
        db.close()
        before = self.rows("SELECT * FROM transaction_annotations")
        self.assertEqual(self.select(False).status_code, 200)
        self.assertEqual(
            self.select(
                True, merchant="덮어쓰지 않음", category="미분류", memo="덮어쓰지 않음"
            ).status_code,
            200,
        )
        self.assertEqual(self.rows("SELECT * FROM transaction_annotations"), before)
        self.assertEqual(self.ledger()[0]["id"], original["id"])
        self.assertTrue(self.ledger()[0]["category_reviewed"])

    def test_selected_conflicts_only_alert_after_new_card_and_unchecking_clears_hold(self):
        self.assertEqual(self.select(True).status_code, 200)
        self.card(background=True)
        state = self.detail().json["transaction"]
        self.assertTrue(state["selected"])
        self.assertTrue(state["needs_review"])
        self.assertFalse(state["reflected"])
        self.assertEqual(self.select(True).status_code, 409)
        self.assertEqual(self.select(True, confirm_separate=True).status_code, 200)
        self.assertFalse(self.detail().json["transaction"]["needs_review"])
        self.assertEqual(self.select(False).status_code, 200)
        self.card(background=True, merchant="가상 두 번째 카드")
        self.assertFalse(self.detail().json["transaction"]["selected"])
        self.assertFalse(self.detail().json["transaction"]["needs_review"])
        self.assertEqual(self.select(True).status_code, 409)

    def test_simple_selection_checks_ownership_revision_eligibility_and_boolean(self):
        opened = self.detail()
        route = f"/api/bank/transactions/{self.identifier}/consumption"
        headers = {**HEADERS, "If-Match": opened.headers["X-Moa-Revision"]}
        for value in (None, 1, "true"):
            self.assertEqual(
                self.owner.put(route, json={"included": value}, headers=headers).status_code, 400
            )
        for identifier in (self.other_identifier, 999999):
            self.assertEqual(
                self.owner.put(
                    f"/api/bank/transactions/{identifier}/consumption",
                    json={"included": True},
                    headers=headers,
                ).status_code,
                404,
            )
        self.assertEqual(
            self.owner.put(route, json={"included": False}, headers=HEADERS).status_code, 428
        )
        for identifier in (self.bank_row(outgoing=0), self.bank_row(outgoing=0, incoming=1200)):
            self.assertEqual(self.select(True, identifier=identifier).status_code, 400)
        self.assertEqual(self.select(True).status_code, 200)
        self.assertEqual(
            self.owner.put(route, json={"included": False}, headers=headers).status_code, 409
        )
        self.assertTrue(self.detail().json["transaction"]["reflected"])

    def test_simple_selection_rejects_stale_candidates_and_foreign_ledger_binding(self):
        opened = self.detail()
        self.card()
        self.assertEqual(
            self.select(
                True, candidate_token=opened.json["candidate_token"], confirm_separate=True
            ).status_code,
            409,
        )
        foreign = self.card(owner=2)
        db = connect()
        with db:
            db.execute(
                "INSERT INTO bank_expense_reviews(bank_transaction_id,decision,ledger_transaction_id) VALUES(?,'expense',?)",
                (self.identifier, foreign),
            )
        db.close()
        for included in (True, False):
            self.assertEqual(self.select(included, confirm_separate=True).status_code, 400)
        self.assertEqual(self.rows("SELECT * FROM transaction_exclusions"), [])

    def test_manually_excluded_expense_does_not_become_selected_when_card_arrives(self):
        self.assertEqual(self.select(True).status_code, 200)
        record = self.ledger()[0]
        # 이전 집계 제외 상태가 있더라도 새 카드가 선택 의사를 되살리면 안 됩니다.
        db = connect()
        with db:
            db.execute(
                "INSERT INTO transaction_exclusions(transaction_id) VALUES(?)", (record["id"],)
            )
        db.close()
        self.card(background=True)
        state = self.detail().json["transaction"]
        self.assertFalse(state["selected"])
        self.assertFalse(state["needs_review"])

    def test_only_own_active_card_candidates_are_returned(self):
        expected = self.card()
        self.card(owner=2, merchant="fixture-private-other-owner")
        self.card(excluded=True)
        self.card(amount=-1200)
        self.card(used_on=(date.fromisoformat(self.day) + timedelta(days=4)).isoformat())
        data = self.detail().json
        self.assertEqual([item["id"] for item in data["candidates"]], [expected])
        self.assertNotIn("fixture-private-other-owner", json.dumps(data))
        self.assertEqual(data["transaction"]["decision"], "pending")
        self.assertEqual(self.rows("SELECT * FROM bank_expense_reviews"), [])

    def test_ambiguous_expense_requires_explicit_acknowledgement(self):
        self.card()
        self.assertEqual(self.save("expense").status_code, 409)
        self.assertEqual(self.rows("SELECT * FROM bank_expense_reviews"), [])
        self.assertEqual(self.save("expense", confirm_separate="true").status_code, 409)
        self.assertEqual(self.save("expense", confirm_separate=True).status_code, 200)
        self.assertEqual(self.active_total(), 2400)

    def test_duplicate_links_only_a_matching_own_card_and_does_not_change_card(self):
        card = self.card()
        foreign = self.card(owner=2)
        self.assertEqual(
            self.save("card_duplicate", matched_transaction_id=foreign).status_code, 400
        )
        self.assertEqual(
            self.save("card_duplicate", matched_transaction_id=999999).status_code, 400
        )
        self.assertEqual(self.save("card_duplicate", matched_transaction_id=card).status_code, 200)
        self.assertEqual(self.active_total(), 1200)
        self.assertEqual(len(self.ledger()), 1)
        review = self.rows("SELECT * FROM bank_expense_reviews")[0]
        self.assertEqual(review["matched_transaction_id"], card)
        self.assertIsNone(review["ledger_transaction_id"])
        self.assertEqual(self.rows("SELECT * FROM transaction_exclusions"), [])

    def test_retract_and_restore_keep_transaction_category_and_memo(self):
        self.assertEqual(self.save("expense").status_code, 200)
        original = self.ledger()[0]
        self.assertEqual(self.save("transfer").status_code, 200)
        after = self.ledger()[0]
        self.assertTrue(after["excluded"])
        self.assertEqual(
            (after["id"], after["category"], after["memo"]),
            (original["id"], original["category"], original["memo"]),
        )
        response = self.owner.get("/api/finance?month=" + self.month)
        revision = response.headers["X-Moa-Revision"]
        blocked = self.owner.put(
            f"/api/transactions/{original['id']}/exclusion",
            json={"excluded": False},
            headers={**HEADERS, "If-Match": revision},
        )
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(self.active_total(), 0)
        self.assertEqual(self.save("expense").status_code, 200)
        self.assertEqual(self.ledger()[0]["id"], original["id"])
        self.assertEqual(self.active_total(), 1200)

    def test_optional_memo_is_preserved_when_reconfirming_without_a_new_value(self):
        self.assertEqual(self.save("expense").status_code, 200)
        self.assertEqual(self.save("transfer").status_code, 200)
        data = self.detail()
        response = self.owner.put(
            f"/api/bank/transactions/{self.identifier}/review",
            json={
                "decision": "expense",
                "merchant": "가상 계좌 식당",
                "category": "식비",
                "candidate_token": data.json["candidate_token"],
            },
            headers={**HEADERS, "If-Match": data.headers["X-Moa-Revision"]},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.ledger()[0]["memo"], "가상 개인 메모")

    def test_late_card_holds_bank_only_and_acknowledged_candidates_do_not_repeat(self):
        self.assertEqual(self.save("expense").status_code, 200)
        bank_id = self.ledger()[0]["id"]
        card = self.card(background=True)
        self.assertEqual(self.active_total(), 1200)
        self.assertEqual(self.rows("SELECT held FROM bank_expense_reviews")[0]["held"], 1)
        self.assertEqual(
            self.rows("SELECT transaction_id FROM transaction_exclusions"),
            [{"transaction_id": bank_id}],
        )
        self.assertTrue(self.detail().json["transaction"]["needs_review"])
        self.assertEqual(self.save("expense", confirm_separate=True).status_code, 200)
        self.assertEqual(self.active_total(), 2400)
        db = connect()
        with db:
            codef_sync.touch_revision(db, 1)
        db.close()
        self.assertFalse(self.detail().json["transaction"]["needs_review"])
        self.assertEqual(self.active_total(), 2400)
        self.card(background=True, merchant="가상 새 카드 거래")
        self.assertTrue(self.detail().json["transaction"]["needs_review"])
        self.assertEqual(self.active_total(), 2400)
        self.assertEqual(
            self.rows("SELECT amount FROM transactions WHERE id=?", (card,))[0]["amount"], 1200
        )

    def test_other_owner_card_does_not_hold_my_expense(self):
        self.assertEqual(self.save("expense").status_code, 200)
        self.card(owner=2, background=True)
        self.assertTrue(self.detail().json["transaction"]["reflected"])
        self.assertEqual(self.active_total(), 1200)

    def test_candidate_token_and_screen_revision_reject_stale_confirmation(self):
        opened = self.detail()
        self.card()
        stale = self.owner.put(
            f"/api/bank/transactions/{self.identifier}/review",
            json={
                "decision": "expense",
                "merchant": "가상 사용처",
                "category": "식비",
                "candidate_token": opened.json["candidate_token"],
                "confirm_separate": True,
            },
            headers={**HEADERS, "If-Match": opened.headers["X-Moa-Revision"]},
        )
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(self.rows("SELECT * FROM bank_expense_reviews"), [])
        self.assertEqual(self.save("transfer").status_code, 200)
        stale = self.owner.put(
            f"/api/bank/transactions/{self.identifier}/review",
            json={"decision": "not_expense"},
            headers={**HEADERS, "If-Match": opened.headers["X-Moa-Revision"]},
        )
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(
            self.rows("SELECT decision FROM bank_expense_reviews")[0]["decision"], "transfer"
        )

    def test_foreign_and_missing_bank_rows_are_indistinguishable(self):
        revision = self.detail().headers["X-Moa-Revision"]
        for identifier in (self.other_identifier, 999999):
            self.assertEqual(
                self.owner.get(f"/api/bank/transactions/{identifier}/review").status_code, 404
            )
            self.assertEqual(
                self.owner.put(
                    f"/api/bank/transactions/{identifier}/review",
                    json={"decision": "transfer"},
                    headers={**HEADERS, "If-Match": revision},
                ).status_code,
                404,
            )
        self.assertEqual(
            self.partner.get("/api/bank?month=" + self.month).json["review_summary"][
                "pending_count"
            ],
            0,
        )
        self.assertEqual(self.rows("SELECT * FROM bank_expense_reviews"), [])

    def test_incorrect_ledger_binding_never_exposes_or_excludes_other_owner(self):
        foreign = self.card(owner=2, merchant="fixture-private-foreign-ledger")
        db = connect()
        with db:
            db.execute(
                "INSERT INTO bank_expense_reviews(bank_transaction_id,decision,ledger_transaction_id) VALUES(?,'expense',?)",
                (self.identifier, foreign),
            )
        db.close()
        data = self.detail().json
        self.assertEqual(data["merchant"], "")
        self.assertNotIn("fixture-private-foreign-ledger", json.dumps(data))
        self.assertEqual(self.save("transfer").status_code, 400)
        self.assertEqual(self.rows("SELECT * FROM transaction_exclusions"), [])

    def test_revision_is_required_even_when_no_manager_is_active(self):
        db = connect()
        with db:
            db.execute("UPDATE users SET active=0 WHERE id=2")
        db.close()
        response = self.owner.put(
            f"/api/bank/transactions/{self.identifier}/review",
            json={"decision": "transfer"},
            headers=HEADERS,
        )
        self.assertEqual(response.status_code, 428)
        self.assertEqual(self.rows("SELECT * FROM bank_expense_reviews"), [])

    def test_income_zero_rows_and_invalid_fields_are_rejected_without_partial_writes(self):
        for identifier in (self.bank_row(outgoing=0), self.bank_row(outgoing=0, incoming=1200)):
            self.assertEqual(self.save("expense", identifier=identifier).status_code, 400)
        for changes in (
            {"category": "fixture-unknown-category"},
            {"merchant": ""},
            {"merchant": "x" * 501},
            {"memo": "\x00"},
        ):
            response = self.save("expense", **changes)
            self.assertEqual(response.status_code, 400)
        self.assertEqual(self.save(True).status_code, 400)
        self.assertEqual(self.rows("SELECT * FROM imports"), [])
        self.assertEqual(self.rows("SELECT * FROM bank_expense_reviews"), [])

    def test_personal_custom_categories_are_supported(self):
        db = connect()
        with db:
            db.execute(
                "INSERT INTO spending_categories VALUES(1,'custom:fixture','가상 맞춤 분야',1)"
            )
            db.execute("INSERT INTO spending_categories VALUES(2,'custom:other','상대방 분야',1)")
        db.close()
        self.assertEqual(self.save("expense", category="custom:other").status_code, 400)
        self.assertEqual(self.save("expense", category="custom:fixture").status_code, 200)
        self.assertEqual(self.ledger()[0]["category"], "가상 맞춤 분야")

    def test_uploaded_card_holds_bank_in_the_same_http_commit(self):
        self.assertEqual(self.save("expense").status_code, 200)
        content = f"date,merchant,amount\n{self.day},fixture-card,1200\n".encode()
        preview = self.owner.post(
            "/api/preview", data={"file": (io.BytesIO(content), "fixture.csv")}, headers=HEADERS
        )
        self.assertEqual(preview.status_code, 200)
        revision = self.owner.get("/api/data").headers["X-Moa-Revision"]
        response = self.owner.post(
            "/api/import",
            json={
                "token": preview.json["token"],
                "card": "신한",
                "date": 0,
                "merchant": 1,
                "amount": 2,
            },
            headers={**HEADERS, "If-Match": revision},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.active_total(), 1200)
        self.assertTrue(self.detail().json["transaction"]["needs_review"])

    def test_import_deletion_does_not_silently_restore_bank_expense(self):
        self.assertEqual(self.save("expense").status_code, 200)
        ledger = self.ledger()[0]
        revision = self.owner.get("/api/data").headers["X-Moa-Revision"]
        response = self.owner.delete(
            "/api/imports/" + str(ledger["import_id"]), headers={**HEADERS, "If-Match": revision}
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(
            self.rows("SELECT ledger_transaction_id FROM bank_expense_reviews")[0][
                "ledger_transaction_id"
            ]
        )
        db = connect()
        with db:
            codef_sync.touch_revision(db, 1)
        db.close()
        self.assertEqual(self.ledger(), [])
        self.assertEqual(self.save("expense").status_code, 200)
        self.assertEqual(len(self.ledger()), 1)

    def test_failed_background_transaction_rolls_back_card_and_bank_hold(self):
        self.assertEqual(self.save("expense").status_code, 200)
        db = connect()
        try:
            with self.assertRaises(RuntimeError), db:
                source = db.execute(
                    "INSERT INTO imports(card,filename,digest,owner_id) VALUES('신한','가상 실패','fixture-rollback',1)"
                ).lastrowid
                db.execute(
                    "INSERT INTO transactions(import_id,used_on,merchant,amount) VALUES(?,?,?,1200)",
                    (source, self.day, "가상 카드 실패"),
                )
                codef_sync.touch_revision(db, 1)
                raise RuntimeError("fixture rollback")
        finally:
            db.close()
        self.assertEqual(len(self.ledger()), 1)
        self.assertTrue(self.detail().json["transaction"]["reflected"])
        self.assertEqual(self.rows("SELECT * FROM transaction_exclusions"), [])

    def test_household_only_receives_aggregate_and_shared_management_omits_bank_details(self):
        merchant = "가상 계좌 비공개 상대"
        day = min(int(self.day[-2:]), 28)
        for offset in range(3):
            used_on = finance.month_name(finance.month_index(self.month) - offset) + f"-{day:02}"
            identifier = self.bank_row(used_on=used_on)
            self.assertEqual(
                self.save("expense", identifier=identifier, merchant=merchant).status_code, 200
            )
        private = self.owner.get("/api/finance?month=" + self.month).get_data(as_text=True)
        self.assertIn(merchant, json.dumps(json.loads(private), ensure_ascii=False))
        shared = self.partner.get("/api/shared-management?month=" + self.month).get_data(
            as_text=True
        )
        self.assertNotIn(merchant, json.dumps(json.loads(shared), ensure_ascii=False))
        household = self.partner.get("/api/household?month=" + self.month)
        self.assertEqual(household.json["net"], 1200)
        self.assertNotIn(merchant, json.dumps(household.json, ensure_ascii=False))
        self.assertNotIn("가상 개인 메모", json.dumps(household.json, ensure_ascii=False))
        self.assertEqual(self.partner.get("/api/data").json["transactions"], [])

    @unittest.skipUnless(
        os.environ.get("MOA_BROWSER_TESTS") == "1", "샌드박스 Chromium 환경에서 실행"
    )
    def test_phone_review_confirmation_duplicate_and_retraction(self):
        """휴대폰에서 직접 확인·되돌리기·후보 확인과 원장·대시보드 반영을 검증합니다."""
        from werkzeug.serving import make_server, WSGIRequestHandler

        self.card()
        simple_month = finance.month_name(finance.month_index(self.month) - 1)
        self.bank_row(used_on=simple_month + "-02", outgoing=3456, description="가상 간단 선택")
        db = connect()
        with db:
            db.execute(
                "INSERT OR REPLACE INTO codef_worker_state VALUES(1,?)",
                (int(datetime.now().timestamp()),),
            )
        db.close()

        class QuietHandler(WSGIRequestHandler):
            def log(self, *args, **kwargs):
                pass

        if os.environ.get("MOA_SCREENSHOT_DIR"):
            Path(os.environ["MOA_SCREENSHOT_DIR"]).mkdir(parents=True, exist_ok=True)
        server = make_server("127.0.0.1", 0, app, threaded=True, request_handler=QuietHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            subprocess.run(
                ["node", "tests/bank-expenses-ui.cjs"],
                cwd=Path(__file__).resolve().parents[1],
                env={
                    **os.environ,
                    "MOA_TEST_URL": f"http://127.0.0.1:{server.server_port}",
                    "MOA_SIMPLE_BANK_MONTH": simple_month,
                },
                check=True,
                timeout=120,
            )
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()


if __name__ == "__main__":
    unittest.main()
