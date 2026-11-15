"""개인 카드사 추가·표시 이름·사용 해제와 공유 읽기·개인 쓰기 경계를 가상 자료로 검증합니다."""

from pathlib import Path
import tempfile
import unittest

from app import app, connect
from auth import set_password

HEADERS = {"X-Requested-With": "CardDashboard"}


class PersonalCardSettingsTests(unittest.TestCase):
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

    def tearDown(self):
        app.config.update(self.previous)
        self.directory.cleanup()

    def report(self, client=None, month="2026-11"):
        return (client or self.owner).get("/api/billing?month=" + month)

    def write(self, path, payload, client=None, method="PUT"):
        client = client or self.owner
        revision = self.report(client).headers["X-Moa-Revision"]
        return client.open(
            path, method=method, json=payload, headers={**HEADERS, "If-Match": revision}
        )

    def add(self, client=None, **patch):
        return self.write(
            "/api/billing/cards",
            {"card": "가상추가", "display_name": "내가상카드", "due_day": 31, **patch},
            client,
            "POST",
        )

    def test_personal_custom_cards_names_and_shared_reading(self):
        self.assertEqual(self.add().status_code, 201)
        self.assertEqual(self.add(self.partner, display_name="상대가상카드").status_code, 201)
        for client, name in ((self.owner, "내가상카드"), (self.partner, "상대가상카드")):
            self.assertEqual(
                self.write(
                    "/api/billing/settings/삼성",
                    dict(enabled=True, due_day=15, display_name=name),
                    client,
                ).status_code,
                200,
            )
            data = self.report(client).json
            self.assertEqual(len(data["cards"]), 7)
            self.assertEqual(
                next(row for row in data["cards"] if row["card"] == "삼성")["display_name"], name
            )
            self.assertEqual(
                next(row for row in data["cards"] if row["card"] == "가상추가")["display_name"],
                name,
            )
            self.assertEqual(
                client.get("/api/finance?month=2026-11").json["cards"][-1]["display_name"], name
            )
        for client in (self.owner, self.partner):
            data = client.get("/api/shared-management?month=2026-11").json
            for member, name in zip(data["members"], ("내가상카드", "상대가상카드")):
                self.assertEqual(
                    next(row for row in member["billing"]["cards"] if row["card"] == "가상추가")[
                        "display_name"
                    ],
                    name,
                )
        # 다른 계정 번호를 URL에 지정해도 본인 설정만 반환합니다.
        self.assertEqual(
            self.owner.get("/api/billing?month=2026-11&owner_id=2").json["cards"][-1][
                "display_name"
            ],
            "내가상카드",
        )

    def test_unregistered_partner_cannot_write_and_history_survives_rename_disable(self):
        self.add()
        bill = dict(statement_status="received", amount=1200, due_on=None, paid=False)
        self.assertEqual(
            self.write("/api/billing/2026-11/가상추가", bill, self.partner).status_code, 400
        )
        plan = dict(
            name="가상노트북",
            card="가상추가",
            principal=900,
            months=3,
            first_month="2026-11",
            paid_count=0,
        )
        self.assertEqual(
            self.write("/api/installments", plan, self.partner, "POST").status_code, 400
        )
        self.assertEqual(self.write("/api/billing/2026-11/가상추가", bill).status_code, 200)
        result = self.write("/api/installments", plan, method="POST")
        self.assertEqual(result.status_code, 200)
        plan_id = result.json["id"]
        self.assertEqual(
            self.write(
                "/api/billing/settings/가상추가",
                dict(enabled=False, due_day=31, display_name="변경한이름"),
            ).status_code,
            200,
        )
        data = self.report().json
        self.assertEqual(data["total"], 0)
        row = next(row for row in data["cards"] if row["card"] == "가상추가")
        self.assertEqual(
            (row["amount"], row["display_name"], row["due_on"]), (1200, "변경한이름", "2026-11-30")
        )
        self.assertEqual(
            self.write(f"/api/installments/{plan_id}", {**plan, "paid_count": 1}).status_code, 200
        )
        # 표시 이름을 보내지 않는 구버전 요청은 기존 이름을 지우지 않습니다.
        self.write("/api/billing/settings/가상추가", dict(enabled=True, due_day=15))
        self.assertEqual(self.report().json["cards"][-1]["display_name"], "변경한이름")
        self.write(
            "/api/billing/settings/가상추가", dict(enabled=True, due_day=15, display_name="")
        )
        self.assertEqual(self.report().json["total"], 1200)
        self.assertEqual(self.report().json["cards"][-1]["display_name"], "가상추가")
        self.assertIsNone(self.report(month="2026-12").json["cards"][-1]["amount"])
        self.assertEqual(self.owner.get("/api/data").json["transactions"], [])

    def test_validation_duplicate_detection_and_transaction_guards(self):
        self.add()
        for card in ("가상 추가", " 가상추가 ", "삼성", "kb국민"):
            self.assertEqual(self.add(card=card).status_code, 409)
        for patch in (
            {"card": "../bad"},
            {"card": "<img>"},
            {"card": "x" * 41},
            {"card": []},
            {"card": "가상신규", "due_day": 32},
            {"card": "가상신규", "display_name": "x" * 41},
            {"card": "가상신규", "display_name": "한줄\n두줄"},
        ):
            self.assertEqual(self.add(**patch).status_code, 400)
        self.assertEqual(len(self.report().json["cards"]), 7)
        endpoint = "/api/billing/cards"
        self.assertEqual(
            app.test_client()
            .post(endpoint, json={"card": "가상신규"}, headers=HEADERS)
            .status_code,
            401,
        )
        self.assertEqual(
            self.owner.post(endpoint, json={"card": "가상신규"}, headers=HEADERS).status_code, 428
        )
        self.assertEqual(
            self.owner.post(
                endpoint, json={"card": "가상신규"}, headers={**HEADERS, "If-Match": "0"}
            ).status_code,
            409,
        )
        self.assertNotIn("가상신규", [row["card"] for row in self.report().json["cards"]])

    def test_custom_card_limit_is_per_person(self):
        db = connect()
        with db:
            db.executemany(
                "INSERT INTO billing_settings(card,owner_id) VALUES(?,1)",
                [(f"가상카드{i}",) for i in range(30)],
            )
        db.close()
        self.assertEqual(self.add().status_code, 400)
        self.assertEqual(self.add(self.partner).status_code, 201)
