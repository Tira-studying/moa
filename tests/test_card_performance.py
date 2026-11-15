"""검증 범위: 카드 상품별 전월 실적 계산, 거래의 카드 연결 순서, 본인 소유 제한, CODEF 끝자리 저장.

실제 카드 번호·거래 대신 가상 끝자리와 가맹점을 사용합니다.
"""

import os
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import card_performance as perf
import codef_sync
from app import app, connect
from auth import set_password

HEADERS = {"X-Requested-With": "CardDashboard"}


def product(**values):
    base = {
        "issuer": "우리",
        "name": "가상 카드",
        "last_fours": [],
        "benefits": [],
        "excluded_categories": [],
        "note": "",
    }
    return {**base, **values}


class CalculationTests(unittest.TestCase):
    def test_last_four_keeps_only_trailing_digits(self):
        self.assertEqual(perf.last_four_of("5310-12**-****-1234"), "1234")
        self.assertEqual(perf.last_four_of("****0000"), "0000")
        self.assertEqual(perf.last_four_of("본인"), "")
        self.assertEqual(perf.last_four_of(None), "")

    def test_validation_sorts_benefits_and_rejects_bad_input(self):
        cleaned = perf.validate_product(
            product(
                last_fours="1111, 2222 1111",
                benefits=[
                    {"label": "높은 구간", "threshold": 500000},
                    {"label": "낮은 구간", "threshold": 300000},
                ],
                excluded_categories=["통신·공과금"],
            ),
            ["우리"],
            {"통신·공과금"},
        )
        self.assertEqual(cleaned["last_fours"], ["1111", "2222"])
        self.assertEqual([b["threshold"] for b in cleaned["benefits"]], [300000, 500000])
        bad_inputs = [
            product(issuer="없는카드사"),
            product(name=""),
            product(last_fours=["12a4"]),
            product(benefits=[{"label": "구간", "threshold": True}]),
            product(benefits=[{"label": "구간", "threshold": 0}]),
            product(excluded_categories=["없는분야"]),
        ]
        for payload in bad_inputs:
            with self.assertRaises(ValueError):
                perf.validate_product(payload, ["우리"], {"통신·공과금"})

    def test_transactions_are_linked_by_digits_then_statement_then_single_card(self):
        travel = {**product(name="여행", last_fours=["1111"]), "id": 1}
        everyday = {**product(name="생활", last_fours=["2222"]), "id": 2}
        only = {**product(issuer="신한", name="한 장"), "id": 3}
        products = [travel, everyday, only]
        row = lambda **v: {"card": "우리", "card_last_four": "", "import_card_product": None, **v}
        self.assertEqual(perf.resolve_product(row(card_last_four="2222"), products), everyday)
        self.assertEqual(perf.resolve_product(row(import_card_product=1), products), travel)
        self.assertIsNone(perf.resolve_product(row(), products))
        self.assertIsNone(perf.resolve_product(row(card_last_four="9999"), products))
        self.assertEqual(perf.resolve_product(row(card="신한"), products), only)
        self.assertEqual(
            perf.resolve_product(row(card="신한", card_last_four="9999"), products), only
        )
        # 끝자리를 입력한 한 장짜리 카드는 다른 끝자리 거래를 추측해서 넣지 않습니다.
        numbered = {**only, "last_fours": ["3333"]}
        self.assertIsNone(perf.resolve_product(row(card="신한", card_last_four="9999"), [numbered]))

    def test_summary_counts_month_excludes_categories_bank_and_excluded_rows(self):
        card = {
            **product(
                last_fours=["1111"],
                benefits=[
                    {"label": "1구간", "threshold": 3000},
                    {"label": "2구간", "threshold": 10000},
                ],
                excluded_categories=["통신·공과금"],
            ),
            "id": 1,
        }
        base = {"card": "우리", "card_last_four": "1111", "import_card_product": None}
        rows = [
            {**base, "used_on": "2026-11-01", "amount": 4000, "category_key": "식비"},
            {**base, "used_on": "2026-11-02", "amount": -500, "category_key": "식비"},
            {**base, "used_on": "2026-11-03", "amount": 7000, "category_key": "통신·공과금"},
            {
                **base,
                "used_on": "2026-11-04",
                "amount": 9000,
                "category_key": "식비",
                "excluded": 1,
            },
            {
                **base,
                "used_on": "2026-11-05",
                "amount": 9000,
                "category_key": "식비",
                "payment_kind": "bank",
            },
            {**base, "used_on": "2026-10-20", "amount": 12000, "category_key": "식비"},
            {**base, "used_on": "2026-09-20", "amount": 50000, "category_key": "식비"},
            {**base, "card_last_four": "2222", "used_on": "2026-11-06", "amount": 800},
        ]
        report = perf.summarize([card], rows, "2026-11", "2026-10")
        result = report["cards"][0]
        self.assertEqual((result["spent"], result["count"]), (3500, 2))
        self.assertEqual(result["excluded_spent"], 7000)
        self.assertEqual(result["previous_spent"], 12000)
        self.assertEqual([t["met"] for t in result["tiers"]], [True, False])
        self.assertEqual(result["next"]["remaining"], 6500)
        self.assertEqual([t["met"] for t in result["previous_tiers"]], [True, True])
        self.assertEqual(
            report["unassigned"],
            [{"issuer": "우리", "last_four": "2222", "amount": 800, "count": 1}],
        )


class ApiTests(unittest.TestCase):
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
        db = connect()
        with db:
            db.executemany(
                "INSERT INTO imports(id,card,filename,digest,owner_id) VALUES(?,'우리',?,?,1)",
                [(1, "CODEF", "codef:woori:2026-11"), (2, "가상 명세서", "fixture-statement")],
            )
            db.executemany(
                "INSERT INTO transactions(id,import_id,used_on,merchant,amount,card_last_four) "
                "VALUES(?,?,?,?,?,?)",
                [
                    (1, 1, "2026-11-01", "가상카페", 4000, "1111"),
                    (2, 1, "2026-11-02", "가상상점", 6000, "2222"),
                    (3, 2, "2026-11-03", "가상마트", 1500, ""),
                ],
            )
        db.close()
        self.enable(self.owner)

    def tearDown(self):
        app.config.update(self.previous)
        self.directory.cleanup()

    def enable(self, client, enabled=True):
        return self.send(client, "PUT", "/api/card-performance/settings", {"enabled": enabled})

    def send(self, client, method, path, body=None):
        revision = client.get("/api/card-performance").headers["X-Moa-Revision"]
        return client.open(
            path, method=method, json=body, headers={**HEADERS, "If-Match": revision}
        )

    def create(self, client, **values):
        return self.send(client, "POST", "/api/card-performance/products", product(**values))

    def report(self, client=None):
        return (client or self.owner).get("/api/card-performance?month=2026-11").json

    def test_cards_split_by_digits_and_statement_link(self):
        travel = self.create(
            self.owner,
            name="여행 카드",
            last_fours=["1111"],
            benefits=[{"label": "캐시백", "threshold": 5000}],
        ).json["id"]
        self.create(self.owner, name="생활 카드", last_fours=["2222"])
        report = self.report()
        spent = {card["name"]: card["spent"] for card in report["cards"]}
        self.assertEqual(spent, {"여행 카드": 4000, "생활 카드": 6000})
        self.assertEqual(report["unassigned"][0]["amount"], 1500)
        self.assertEqual([s["id"] for s in report["statements"]], [2])
        self.assertEqual(report["cards"][0]["next"]["remaining"], 1000)
        self.assertTrue(any(p["name"] == "LG패밀리 RPM Platinum#" for p in report["presets"]))

        self.assertEqual(
            self.send(
                self.owner, "PUT", "/api/card-performance/imports/2", {"product_id": travel}
            ).status_code,
            200,
        )
        report = self.report()
        self.assertEqual(report["unassigned"], [])
        self.assertEqual(
            next(card for card in report["cards"] if card["id"] == travel)["spent"], 5500
        )

    def test_feature_is_off_by_default_per_user_and_keeps_cards_when_off(self):
        self.create(self.owner, name="보관할 카드")
        partner = self.report(self.partner)
        self.assertFalse(partner["enabled"])
        self.assertEqual(partner["cards"], [])
        self.assertEqual(self.enable(self.owner, False).status_code, 200)
        report = self.report()
        self.assertFalse(report["enabled"])
        self.assertEqual(report["presets"], [])
        self.assertEqual(self.enable(self.owner, "yes").status_code, 400)
        self.enable(self.owner)
        self.assertEqual([card["name"] for card in self.report()["cards"]], ["보관할 카드"])

    def test_owner_only_and_validation(self):
        card = self.create(self.owner, name="본인 카드").json["id"]
        self.assertEqual(self.report(self.partner)["cards"], [])
        self.assertEqual(
            self.send(
                self.partner, "PUT", f"/api/card-performance/products/{card}", product()
            ).status_code,
            404,
        )
        self.assertEqual(
            self.send(self.partner, "DELETE", f"/api/card-performance/products/{card}").status_code,
            404,
        )
        self.assertEqual(
            self.send(
                self.partner, "PUT", "/api/card-performance/imports/2", {"product_id": card}
            ).status_code,
            404,
        )
        self.assertEqual(self.create(self.owner, last_fours=["abcd"]).status_code, 400)
        self.assertEqual(
            self.send(
                self.owner, "PUT", "/api/card-performance/imports/2", {"product_id": 999}
            ).status_code,
            400,
        )
        self.assertEqual(self.owner.get("/api/card-performance?month=2026-13").status_code, 400)

    def test_deleting_card_unlinks_statement_and_keeps_transactions(self):
        card = self.create(self.owner, name="지울 카드").json["id"]
        self.create(self.owner, name="남길 카드")
        self.send(self.owner, "PUT", "/api/card-performance/imports/2", {"product_id": card})
        self.assertEqual(
            self.send(self.owner, "DELETE", f"/api/card-performance/products/{card}").status_code,
            200,
        )
        db = connect()
        try:
            self.assertIsNone(
                db.execute("SELECT card_product_id FROM imports WHERE id=2").fetchone()[0]
            )
            self.assertEqual(db.execute("SELECT COUNT(*) FROM transactions").fetchone()[0], 3)
        finally:
            db.close()


class BrowserTests(ApiTests):
    """ApiTests와 같은 가상 거래를 이번 달 날짜로 옮겨 실제 화면을 검증합니다."""

    # 상속한 API 검증을 브라우저 묶음에서 다시 실행하지 않습니다.
    test_cards_split_by_digits_and_statement_link = None
    test_owner_only_and_validation = None
    test_deleting_card_unlinks_statement_and_keeps_transactions = None
    test_feature_is_off_by_default_per_user_and_keeps_cards_when_off = None

    @unittest.skipUnless(
        os.environ.get("MOA_BROWSER_TESTS") == "1", "샌드박스 Chromium 환경에서 실행"
    )
    def test_card_performance_screen(self):
        from werkzeug.serving import make_server, WSGIRequestHandler

        class QuietHandler(WSGIRequestHandler):
            def log(self, *args, **kwargs):
                pass

        month = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m")
        db = connect()
        with db:
            db.execute("UPDATE transactions SET used_on=? || substr(used_on, 8)", (month,))
        db.close()
        # 화면 검증은 기본 꺼짐 상태에서 설정으로 켜는 것부터 시작합니다.
        self.enable(self.owner, False)
        server = make_server("127.0.0.1", 0, app, threaded=True, request_handler=QuietHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            subprocess.run(
                ["node", "tests/card-performance-ui.cjs"],
                cwd=Path(__file__).resolve().parents[1],
                env={
                    **os.environ,
                    "MOA_TEST_URL": f"http://127.0.0.1:{server.server_port}",
                    "MOA_TEST_MONTH": month,
                },
                check=True,
                timeout=75,
            )
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()


class CodefLastFourTests(unittest.TestCase):
    def setUp(self):
        self.previous = dict(app.config)
        self.directory = tempfile.TemporaryDirectory()
        app.config.update(TESTING=True, DATABASE=str(Path(self.directory.name) / "db.sqlite3"))
        set_password(connect, "fixture-owner-password")

    def tearDown(self):
        app.config.update(self.previous)
        self.directory.cleanup()

    def item(self, **values):
        base = dict(
            fingerprint="fixture-fingerprint",
            used_on="2026-11-01",
            merchant="가상상점",
            original_amount=1000,
            net=1000,
            kind="0",
            reason="",
            quarantined=0,
            last_four="",
        )
        return {**base, **values}

    def test_new_event_saves_digits_and_later_collection_fills_missing_digits(self):
        db = connect()
        try:
            with db:
                codef_sync.store_events(
                    db, 1, "samsung", [self.item(fingerprint="new", last_four="1234")]
                )
                codef_sync.store_events(db, 1, "samsung", [self.item(fingerprint="old")])
            with db:
                codef_sync.store_events(
                    db, 1, "samsung", [self.item(fingerprint="old", last_four="5678")]
                )
            digits = [
                row[0] for row in db.execute("SELECT card_last_four FROM transactions ORDER BY id")
            ]
            self.assertEqual(digits, ["1234", "5678"])
            self.assertEqual(
                db.execute("SELECT last_four FROM codef_events WHERE fingerprint='old'").fetchone()[
                    0
                ],
                "5678",
            )
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
