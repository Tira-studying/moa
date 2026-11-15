"""가상 KB·신한·삼성·현대 문자로 금액 구분·재전송·개인 권한·쓰기 전용 키·대조 경계를 검증합니다."""

from datetime import datetime, timedelta
from pathlib import Path
import hashlib
import os
import tempfile
import unittest

from app import app, connect
from auth import set_password
from sms_inbox import KST, parse_kb, parse_sms

HEADERS = {"X-Requested-With": "CardDashboard"}


def message(kind="승인", amount="12,500", stamp="11/01 12:30", merchant="가상상점"):
    """실제 문자·개인정보를 복제하지 않고 지원하는 구조만 만듭니다."""
    return f"[Web발신]\nKB국민카드2222{kind}\n가*상님\n{amount}원 일시불\n{stamp}\n{merchant}\n누적98,000원"


def other_message(card, kind="승인", amount="12,500", stamp="11/01 12:30", merchant="가상상점"):
    """신한 한 줄·삼성 여러 줄 형식만 재현하며 실제 카드 번호나 거래 정보는 쓰지 않습니다."""
    if card == "신한":
        return f"[Web발신]\n신한카드(3333){kind} 가*상 {amount}원(일시불){stamp} {merchant} 누적98,000원"
    return f"[Web발신]\n삼성4444{kind} 가*상\n{amount}원 일시불\n{stamp} {merchant}\n누적98,000원"


def hyundai_message(
    kind="승인", amount="12,500", stamp="11/01 12:30", product="가상상품", merchant="가상상점"
):
    """카드 번호가 없는 현대 문자 구조를 가상 상품·이름·거래로 만듭니다."""
    return f"[Web발신]\n현대 {product} {kind}\n가*상\n{amount}원 일시불\n{stamp}\n{merchant}\n누적98,000원"


class SmsParserTests(unittest.TestCase):
    def test_approval_cancellation_and_cumulative_are_separate(self):
        now = datetime(2026, 11, 1, 13, tzinfo=KST)
        for kind, value in [("승인", 12500), ("취소", -12500)]:
            result = parse_kb(message(kind), now)
            self.assertEqual(result["amount"], value)
            self.assertEqual(result["occurred_at"], "2026-11-01T12:30+09:00")
            self.assertEqual(result["last_four"], "2222")
            self.assertNotIn("누적", str(result))
            self.assertNotIn("가*상님", str(result))
        installments = parse_kb(message().replace("일시불", "할부 03개월"), now)
        self.assertEqual(installments["amount"], 12500)
        self.assertEqual(installments["payment"], "할부 03개월")

    def test_year_boundary_and_ambiguous_or_invalid_input(self):
        now = datetime(2027, 1, 1, 1, tzinfo=KST)
        self.assertTrue(
            parse_kb(message(stamp="12/31 23:30"), now)["occurred_at"].startswith("2026-12-31")
        )
        for text in [
            message(stamp="11/01 12:30"),
            message(stamp="02/30 12:30"),
            message(stamp="01/01 02:30"),
            message().replace("승인", "거절"),
            message().replace("12,500원", "USD 12.50"),
            message().replace("12,500원", "12,50원"),
            message().replace("KB국민카드", "알수없는카드"),
            message() + "\n다른 거래 내용",
            message().replace("가상상점\n", ""),
            message(merchant="가상\ud800상점"),
            message(merchant="가상\x00상점"),
            None,
            ["본문"],
            "x" * 2001,
        ]:
            with self.assertRaises(ValueError):
                parse_kb(text, now)

    def test_shinhan_samsung_signs_amount_and_fields(self):
        now = datetime(2026, 11, 1, 13, tzinfo=KST)
        for card, last_four in (("신한", "3333"), ("삼성", "4444")):
            for kind, raw_amount, expected in (
                ("승인", "12,500", 12500),
                ("취소", "12,500", -12500),
                ("취소", "-12,500", -12500),
            ):
                with self.subTest(card=card, kind=kind, raw_amount=raw_amount):
                    result = parse_sms(other_message(card, kind, raw_amount), now)
                    self.assertEqual(result["card"], card)
                    self.assertEqual(result["last_four"], last_four)
                    self.assertEqual(result["amount"], expected)
                    self.assertEqual(result["merchant"], "가상상점")
                    self.assertEqual(result["payment"], "일시불")
                    self.assertEqual(result["occurred_at"], "2026-11-01T12:30+09:00")
                    self.assertNotIn("가*상", str(result))
                    self.assertNotIn("누적", str(result))
        # 기존 KB 재전송 판정이 새 카드 지원 때문에 달라지지 않습니다.
        self.assertEqual(parse_sms(message(), now), parse_kb(message(), now))

    def test_new_cards_reject_unsupported_or_combined_messages(self):
        now = datetime(2026, 11, 1, 13, tzinfo=KST)
        for card in ("신한", "삼성"):
            original = other_message(card)
            for text in (
                other_message(card, amount="-12,500"),
                other_message(card, amount="0"),
                original.replace("12,500원", "12,50원"),
                original.replace("12,500원", "USD 12.50"),
                original.replace("승인", "거절"),
                original.replace("11/01 12:30", "02/30 12:30"),
                original.replace("가상상점", ""),
                original + "\n" + original,
                (original + " " + original).replace("\n", " "),
            ):
                with self.subTest(card=card, text_index=len(text)):
                    with self.assertRaises(ValueError):
                        parse_sms(text, now)

    def test_hyundai_product_without_guessed_card_number_and_incomplete_rcs(self):
        now = datetime(2026, 11, 1, 13, tzinfo=KST)
        for kind, amount, expected in (
            ("승인", "12,500", 12500),
            ("취소", "12,500", -12500),
            ("취소", "-12,500", -12500),
        ):
            result = parse_sms(hyundai_message(kind, amount, product="가상 상품2222"), now)
            self.assertEqual(result["card"], "현대")
            self.assertEqual(result["last_four"], "")
            self.assertEqual(result["product"], "가상 상품2222")
            self.assertEqual(result["merchant"], "가상상점")
            self.assertEqual(result["amount"], expected)
            self.assertEqual(result["occurred_at"], "2026-11-01T12:30+09:00")
            self.assertNotIn("가*상", str(result))
            self.assertNotIn("누적", str(result))
        original = hyundai_message()
        for text in (
            "[Web발신]\n현대 가상상품 승인\n가*상...",
            hyundai_message(amount="-12,500"),
            original.replace("현대 가상상품 승인", "현대   승인"),
            original.replace("가상상점\n", ""),
            original.replace("12,500원", "USD 12.50"),
            original.replace("승인", "거절"),
            original + "\n" + original,
        ):
            with self.assertRaises(ValueError):
                parse_sms(text, now)

    def test_shortcut_inline_sender_and_collapsed_line_breaks_preserve_events(self):
        now = datetime(2026, 11, 1, 13, tzinfo=KST)
        for kind in ("승인", "취소"):
            for original in (
                message(kind),
                other_message("신한", kind),
                other_message("삼성", kind, "-12,500" if kind == "취소" else "12,500"),
                hyundai_message(kind),
            ):
                expected = parse_sms(original, now)
                for changed in (
                    original.replace("\n", " "),
                    original.replace("\n", "\t"),
                    original.replace("\n", "\r\n"),
                    original.replace("[Web발신]\n", "[Web발신] "),
                    original.replace("[Web발신]\n", ""),
                ):
                    self.assertEqual(parse_sms(changed, now), expected)
        # 공백 형식에서도 누적액을 소비처로 오인하거나 여러 거래를 한 건으로 받지 않습니다.
        for original in (
            message(),
            other_message("신한"),
            other_message("삼성"),
            hyundai_message(),
        ):
            collapsed = original.replace("\n", " ")
            for invalid in (
                collapsed + " " + collapsed,
                (collapsed + " " + collapsed).replace("[Web발신]", ""),
                collapsed + " 잘린 다른 안내",
                collapsed.replace("가상상점", ""),
            ):
                with self.assertRaises(ValueError):
                    parse_sms(invalid, now)


class SmsApiTests(unittest.TestCase):
    def setUp(self):
        self.previous = dict(app.config)
        self.directory = tempfile.TemporaryDirectory()
        app.config.update(
            TESTING=True, PUBLIC_ORIGIN="", DATABASE=str(Path(self.directory.name) / "db.sqlite3")
        )
        set_password(connect, "fixture-owner-password")
        self.owner = app.test_client()
        self.owner.post("/api/login", json={"password": "fixture-owner-password"}, headers=HEADERS)
        revision = self.owner.get("/api/members").headers["X-Moa-Revision"]
        self.owner.post(
            "/api/members",
            json={"username": "partner", "password": "fixture-partner-password"},
            headers={**HEADERS, "If-Match": revision},
        )
        self.partner = app.test_client()
        self.partner.post(
            "/api/login",
            json={"username": "partner", "password": "fixture-partner-password"},
            headers=HEADERS,
        )
        self.sender = app.test_client()
        self.stamp = datetime.now(KST) - timedelta(minutes=2)
        self.month = self.stamp.strftime("%Y-%m")

    def tearDown(self):
        app.config.update(self.previous)
        self.directory.cleanup()

    def read(self, client=None):
        return (client or self.owner).get("/api/sms?month=" + self.month)

    def create_key(self, client=None):
        client = client or self.owner
        revision = self.read(client).headers["X-Moa-Revision"]
        response = client.post("/api/sms/key", headers={**HEADERS, "If-Match": revision})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        return response.json["key"]

    def receive(self, key, kind="승인", **options):
        return self.sender.post(
            "/api/sms/receive",
            json={"text": message(kind, stamp=self.stamp.strftime("%m/%d %H:%M")), **options},
            headers={**HEADERS, "Authorization": "Bearer " + key},
        )

    def test_receive_totals_replay_and_no_private_raw_storage_or_ledger_merge(self):
        key = self.create_key()
        for kind in ("승인", "취소"):
            self.assertEqual(self.receive(key, kind).status_code, 201)
            duplicate = self.receive(key, kind)
            self.assertEqual(duplicate.status_code, 200)
            self.assertTrue(duplicate.json["duplicate"])
        data = self.read().json
        self.assertEqual(
            (data["pending_count"], data["approvals"], data["cancellations"], data["net"]),
            (2, 12500, 12500, 0),
        )
        self.assertEqual(self.owner.get("/api/data").json["transactions"], [])
        self.assertEqual(self.partner.get("/api/data").json["transactions"], [])
        aggregate = self.owner.get("/api/household?month=" + self.month)
        self.assertNotIn("가상상점", aggregate.text)
        self.assertEqual(aggregate.json["net"], 0)
        self.assertEqual(self.read(self.partner).json["items"], [])
        self.assertNotIn("key", data["key"])
        db = connect()
        try:
            dump = "\n".join(db.iterdump())
            self.assertNotIn(key, dump)
            self.assertNotIn("가*상님", dump)
            self.assertNotIn("누적", dump)
            self.assertNotIn("98,000", dump)
            self.assertEqual(
                db.execute("SELECT digest FROM sms_keys WHERE user_id=1").fetchone()[0],
                hashlib.sha256(key.encode()).hexdigest(),
            )
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM audit_log WHERE action='문자 임시내역 수신'"
                ).fetchone()[0],
                2,
            )
        finally:
            db.close()

    def test_new_cards_share_receiver_without_cross_card_duplicates_or_raw_storage(self):
        key = self.create_key()
        for card in ("신한", "삼성"):
            for kind in ("승인", "취소"):
                amount = "-12,500" if card == "삼성" and kind == "취소" else "12,500"
                text = other_message(card, kind, amount, self.stamp.strftime("%m/%d %H:%M"))
                self.assertEqual(self.receive(key, text=text).status_code, 201)
                self.assertTrue(self.receive(key, text=text).json["duplicate"])
        # 취소의 원문 부호가 달라도 같은 취소 사건으로 처리합니다.
        positive_cancel = other_message("삼성", "취소", stamp=self.stamp.strftime("%m/%d %H:%M"))
        self.assertTrue(self.receive(key, text=positive_cancel).json["duplicate"])
        data = self.read().json
        self.assertEqual(
            (len(data["items"]), data["approvals"], data["cancellations"], data["net"]),
            (4, 25000, 25000, 0),
        )
        self.assertEqual({row["card"] for row in data["items"]}, {"신한", "삼성"})
        self.assertEqual(self.read(self.partner).json["items"], [])
        self.assertEqual(self.owner.get("/api/data").json["transactions"], [])
        db = connect()
        try:
            dump = "\n".join(db.iterdump())
            self.assertNotIn("가*상", dump)
            self.assertNotIn("누적", dump)
        finally:
            db.close()

    def test_hyundai_product_storage_personal_scope_and_replay(self):
        key = self.create_key()
        for kind in ("승인", "취소"):
            text = hyundai_message(kind, stamp=self.stamp.strftime("%m/%d %H:%M"))
            self.assertEqual(self.receive(key, text=text).status_code, 201)
            self.assertTrue(self.receive(key, text=text).json["duplicate"])
        data = self.read().json
        self.assertEqual(
            (len(data["items"]), data["approvals"], data["cancellations"], data["net"]),
            (2, 12500, 12500, 0),
        )
        self.assertTrue(
            all(row["product"] == "가상상품" and row["last_four"] == "" for row in data["items"])
        )
        self.assertEqual(self.read(self.partner).json["items"], [])
        self.assertEqual(self.owner.get("/api/data").json["transactions"], [])
        # 같은 시각·금액·상점이어도 상품명이 다르면 임의로 합치지 않습니다.
        another = hyundai_message(stamp=self.stamp.strftime("%m/%d %H:%M"), product="다른가상상품")
        self.assertEqual(self.receive(key, text=another).status_code, 201)
        self.assertEqual(self.read().json["net"], 12500)
        db = connect()
        try:
            dump = "\n".join(db.iterdump())
            self.assertNotIn("가*상", dump)
            self.assertNotIn("누적", dump)
        finally:
            db.close()

    def test_old_sms_database_upgrade_preserves_key_and_duplicate_history(self):
        key = self.create_key()
        self.assertEqual(self.receive(key).status_code, 201)
        original = self.read().json["items"][0]
        db = connect()
        with db:
            # 상품명이 없던 기존 버전의 표 구조를 임시 DB에 재현합니다.
            db.execute("ALTER TABLE sms_events DROP COLUMN product")
            # 버전 관리 도입 전 DB는 user_version이 0입니다. 다음 연결에서 이전 단계가 다시 실행됩니다.
            db.execute("PRAGMA user_version = 0")
        db.close()
        # 다음 연결에서 열을 추가해도 기존 키와 이벤트의 중복 해시는 바뀌면 안 됩니다.
        self.assertTrue(self.receive(key).json["duplicate"])
        restored = self.read().json["items"]
        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0], original)

    def test_shortcut_single_line_and_original_are_one_delivery(self):
        key = self.create_key()
        for original in (
            message(),
            other_message("신한"),
            other_message("삼성"),
            hyundai_message(),
        ):
            original = original.replace("11/01 12:30", self.stamp.strftime("%m/%d %H:%M"))
            self.assertEqual(self.receive(key, text=original.replace("\n", " ")).status_code, 201)
            self.assertTrue(self.receive(key, text=original).json["duplicate"])
        result = self.read().json
        self.assertEqual(len(result["items"]), 4)
        self.assertEqual(result["net"], 50000)
        self.assertEqual(self.read(self.partner).json["items"], [])

    def test_write_key_cannot_read_or_select_another_owner(self):
        key = self.create_key(self.partner)
        self.assertEqual(self.receive(key, owner_id=1).status_code, 201)
        self.assertEqual(self.read().json["items"], [])
        self.assertEqual(len(self.read(self.partner).json["items"]), 1)
        headers = {**HEADERS, "Authorization": "Bearer " + key}
        self.assertEqual(
            self.sender.get("/api/sms?month=" + self.month, headers=headers).status_code, 401
        )
        self.assertEqual(self.sender.get("/api/data", headers=headers).status_code, 401)
        self.assertEqual(self.sender.post("/api/sms/key", headers=headers).status_code, 401)
        event_id = self.read(self.partner).json["items"][0]["id"]
        revision = self.read().headers["X-Moa-Revision"]
        self.assertEqual(
            self.owner.put(
                f"/api/sms/events/{event_id}",
                json={"status": "settled"},
                headers={**HEADERS, "If-Match": revision},
            ).status_code,
            404,
        )
        self.assertEqual(
            self.sender.put(
                f"/api/sms/events/{event_id}", json={"status": "settled"}, headers=headers
            ).status_code,
            401,
        )

    def test_key_rotation_revocation_account_disable_and_revision_guards(self):
        old = self.create_key()
        new = self.create_key()
        self.assertEqual(self.receive(old).status_code, 401)
        self.assertEqual(self.receive(new).status_code, 201)
        self.assertEqual(self.owner.post("/api/sms/key", headers=HEADERS).status_code, 428)
        revision = self.read().headers["X-Moa-Revision"]
        self.assertEqual(
            self.owner.delete(
                "/api/sms/key", headers={**HEADERS, "If-Match": revision}
            ).status_code,
            200,
        )
        self.assertEqual(self.receive(new).status_code, 401)
        partner_key = self.create_key(self.partner)
        revision = self.owner.get("/api/members").headers["X-Moa-Revision"]
        self.owner.put(
            "/api/members/2", json={"active": False}, headers={**HEADERS, "If-Match": revision}
        )
        self.assertEqual(self.receive(partner_key).status_code, 401)
        revision = self.owner.get("/api/members").headers["X-Moa-Revision"]
        self.owner.put(
            "/api/members/2", json={"active": True}, headers={**HEADERS, "If-Match": revision}
        )
        self.assertEqual(self.receive(partner_key).status_code, 401)
        owner_key = self.create_key()
        set_password(connect, "fixture-new-owner-password")
        self.assertEqual(self.receive(owner_key).status_code, 401)

    def test_statement_candidate_is_private_manual_settlement_and_reversible(self):
        key = self.create_key()
        self.receive(key)
        db = connect()
        with db:
            for owner in (1, 2):
                inserted = db.execute(
                    "INSERT INTO imports(card,filename,digest,owner_id) VALUES('KB국민','가상.csv',?,?)",
                    (f"fixture-{owner}", owner),
                )
                db.execute(
                    "INSERT INTO transactions(import_id,used_on,merchant,amount) VALUES(?,?,?,?)",
                    (inserted.lastrowid, self.stamp.date().isoformat(), "가상상점", 12500),
                )
        db.close()
        response = self.read()
        row = response.json["items"][0]
        self.assertEqual(row["statement_candidates"], 1)
        self.assertEqual(response.json["net"], 12500)
        revision = response.headers["X-Moa-Revision"]
        event_path = f'/api/sms/events/{row["id"]}'
        for status, count in (("settled", 0), ("pending", 1), ("ignored", 0)):
            update = self.owner.put(
                event_path, json={"status": status}, headers={**HEADERS, "If-Match": revision}
            )
            self.assertEqual(update.status_code, 200)
            revision = update.headers["X-Moa-Revision"]
            self.assertEqual(self.read().json["pending_count"], count)
            self.assertEqual(
                self.read().json["spending"]["net"], 0 if status == "ignored" else 12500
            )
        self.assertEqual(self.owner.get("/api/data").json["transactions"][0]["amount"], 12500)
        stale = self.owner.put(
            event_path, json={"status": "pending"}, headers={**HEADERS, "If-Match": "0"}
        )
        self.assertEqual(stale.status_code, 409)

    def test_completed_spending_stays_visible_and_only_exclusion_changes_sms_total(self):
        key = self.create_key()
        self.receive(key)
        cancellation = message("취소", amount="5,000", stamp=self.stamp.strftime("%m/%d %H:%M"))
        self.receive(key, text=cancellation)
        data = self.read().json
        approval = next(row for row in data["items"] if row["kind"] == "approval")
        self.assertEqual(data["spending"]["net"], 7500)
        for status, total, count in (
            ("settled", 7500, 2),
            ("ignored", -5000, 1),
            ("pending", 7500, 2),
        ):
            revision = self.read().headers["X-Moa-Revision"]
            self.assertEqual(
                self.owner.put(
                    f'/api/sms/events/{approval["id"]}',
                    json={"status": status},
                    headers={**HEADERS, "If-Match": revision},
                ).status_code,
                200,
            )
            # 이전에 완료한 내역도 같은 조회에서 보존되며 재수신이나 DB 이전이 필요 없습니다.
            refreshed = self.read().json
            self.assertEqual(refreshed["spending"]["net"], total)
            self.assertEqual(refreshed["spending"]["count"], count)
            self.assertEqual(len(refreshed["items"]), 2)
            self.assertEqual(refreshed["spending"]["settled_count"], int(status == "settled"))
        self.assertEqual(self.read(self.partner).json["spending"]["net"], 0)
        self.assertEqual(self.owner.get("/api/data").json["transactions"], [])
        self.assertEqual(self.owner.get("/api/household?month=" + self.month).json["net"], 0)

    def test_payload_size_unsupported_messages_origin_and_rate_limit(self):
        key = self.create_key()
        self.assertEqual(
            self.sender.post("/api/sms/receive", json={}, headers=HEADERS).status_code, 401
        )
        self.assertEqual(
            self.sender.post(
                "/api/sms/receive", json={}, headers={"Authorization": "Bearer " + key}
            ).status_code,
            403,
        )
        response = self.sender.post(
            "/api/sms/receive",
            json={},
            headers={
                **HEADERS,
                "Authorization": "Bearer " + key,
                "Origin": "https://other.invalid",
            },
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.receive(key, text="지원하지 않는 형식").status_code, 422)
        self.assertEqual(self.receive(key, text="x" * 9000).status_code, 413)
        self.assertEqual(self.read().json["items"], [])
        db = connect()
        with db:
            import time

            db.execute("UPDATE sms_keys SET window_start=?,request_count=120", (int(time.time()),))
        db.close()
        self.assertEqual(self.receive(key).status_code, 429)
        # 설정된 HTTPS 호스트 검사도 수신 API에서 생략되지 않습니다.
        app.config["PUBLIC_ORIGIN"] = "https://fixture.invalid"
        self.assertEqual(self.receive(key).status_code, 400)


class SmsBrowserTests(unittest.TestCase):
    @unittest.skipUnless(
        os.environ.get("MOA_BROWSER_TESTS") == "1", "샌드박스 Chromium 환경에서 실행"
    )
    def test_real_shortcut_receiver_and_mobile_screen(self):
        """운영 DB·문자·키 없이 임시 서버와 브라우저로 사용자 흐름을 확인합니다."""
        import subprocess
        import threading
        from werkzeug.serving import make_server, WSGIRequestHandler

        class QuietHandler(WSGIRequestHandler):
            def log(self, *args, **kwargs):
                pass

        previous = dict(app.config)
        with tempfile.TemporaryDirectory() as directory:
            app.config.update(
                TESTING=True, PUBLIC_ORIGIN="", DATABASE=str(Path(directory) / "db.sqlite3")
            )
            set_password(connect, "fixture-owner-password")
            server = make_server("127.0.0.1", 0, app, threaded=True, request_handler=QuietHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                subprocess.run(
                    ["node", "tests/sms-ui.cjs"],
                    cwd=Path(__file__).resolve().parents[1],
                    env={**os.environ, "MOA_TEST_URL": f"http://127.0.0.1:{server.server_port}"},
                    check=True,
                    timeout=45,
                )
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()
                app.config.update(previous)
