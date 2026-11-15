"""검증 범위: 거래 영수증 사진의 추가·보기·삭제, 형식·크기·장수 검사, 본인만 접근, 거래 삭제 시 함께 삭제,
함께 보기로 보이는 거래의 사진만 상대가 읽는지, 이력에 사진이 새지 않는지, 화면 동작(거래 상세·새 직접 입력·작은 화면).

가상 거래와 임시 DB, 코드로 만든 가짜 이미지 바이트만 사용합니다.
"""

import json
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

import receipts
from app import app, connect
from auth import set_password

HEADERS = {"X-Requested-With": "CardDashboard"}
JPEG = b"\xff\xd8\xff\xe0" + b"FAKE-JPEG-BODY" * 10
PNG = b"\x89PNG\r\n\x1a\n" + b"FAKE-PNG"
WEBP = b"RIFF\x10\x00\x00\x00WEBPVP8 "


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.previous = dict(app.config)
        self.directory = tempfile.TemporaryDirectory()
        app.config.update(
            TESTING=True, PUBLIC_ORIGIN="", DATABASE=str(Path(self.directory.name) / "db.sqlite3")
        )
        set_password(connect, "fixture-owner-password")
        self.owner = app.test_client()
        self.owner.post("/api/login", json={"password": "fixture-owner-password"}, headers=HEADERS)
        self.owner.post(
            "/api/members",
            json={"username": "partner", "password": "fixture-partner-password"},
            headers={**HEADERS, "If-Match": self.revision(self.owner, "/api/members")},
        )
        self.partner = app.test_client()
        self.partner.post(
            "/api/login",
            json={"username": "partner", "password": "fixture-partner-password"},
            headers=HEADERS,
        )
        db = connect()
        with db:
            db.execute(
                "INSERT INTO imports(id,card,filename,digest,owner_id) VALUES(11,'삼성','fixture.csv','fixture-11',1)"
            )
            db.execute(
                "INSERT INTO transactions(id,import_id,used_on,merchant,amount) "
                "VALUES(101,11,'2026-11-03','가상 마트',23000)"
            )
        db.close()

    def tearDown(self):
        app.config.update(self.previous)
        self.directory.cleanup()

    def revision(self, client, path="/api/data"):
        return client.get(path).headers["X-Moa-Revision"]

    def upload(self, client, data, transaction_id=101):
        from io import BytesIO

        return client.post(
            f"/api/transactions/{transaction_id}/receipts",
            data={"file": (BytesIO(data), "receipt.jpg")},
            headers={**HEADERS, "If-Match": self.revision(client)},
            content_type="multipart/form-data",
        )

    def receipt_ids(self, client=None):
        rows = (client or self.owner).get("/api/data").json["transactions"]
        return {row["id"]: row["receipt_ids"] for row in rows}

    def test_upload_view_delete(self):
        response = self.upload(self.owner, JPEG)
        self.assertEqual(response.status_code, 201, response.json)
        receipt = response.json["id"]
        self.assertEqual(self.receipt_ids(), {101: str(receipt)})
        image = self.owner.get(f"/api/receipts/{receipt}")
        self.assertEqual(image.status_code, 200)
        self.assertEqual(image.data, JPEG)
        self.assertEqual(image.mimetype, "image/jpeg")
        self.assertEqual(image.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(image.headers["Cache-Control"], "no-store")
        self.assertEqual(self.upload(self.owner, PNG).status_code, 201)
        self.assertEqual(self.upload(self.owner, WEBP).status_code, 201)
        self.assertEqual(len(self.receipt_ids()[101].split(",")), 3)
        deleted = self.owner.delete(
            f"/api/receipts/{receipt}", headers={**HEADERS, "If-Match": self.revision(self.owner)}
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(self.owner.get(f"/api/receipts/{receipt}").status_code, 404)
        self.assertEqual(len(self.receipt_ids()[101].split(",")), 2)
        # 이력에는 거래 번호만 남고 사진 내용은 남지 않습니다.
        history = json.dumps(self.owner.get("/api/audit").json, ensure_ascii=False)
        self.assertIn("영수증 사진 추가", history)
        self.assertIn("거래 #101", history)
        self.assertNotIn("FAKE", history)

    def test_rejects_other_formats_sizes_and_count(self):
        for data in (b"<svg xmlns='http://www.w3.org/2000/svg'/>", b"<html>", b"", b"GIF89a"):
            response = self.upload(self.owner, data)
            self.assertEqual(response.status_code, 400, data)
        big = JPEG + b"0" * receipts.MAX_BYTES
        self.assertIn("3MB", self.upload(self.owner, big).json["error"])
        for _ in range(receipts.MAX_PER_TRANSACTION):
            self.assertEqual(self.upload(self.owner, JPEG).status_code, 201)
        self.assertIn("5장", self.upload(self.owner, JPEG).json["error"])
        self.assertEqual(self.upload(self.owner, JPEG, transaction_id=999).status_code, 404)

    def test_only_owner_can_touch_receipts(self):
        receipt = self.upload(self.owner, JPEG).json["id"]
        self.assertEqual(self.partner.get(f"/api/receipts/{receipt}").status_code, 404)
        self.assertEqual(self.upload(self.partner, JPEG).status_code, 404)
        response = self.partner.delete(
            f"/api/receipts/{receipt}",
            headers={**HEADERS, "If-Match": self.revision(self.partner)},
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.owner.get(f"/api/receipts/{receipt}").status_code, 200)
        # 함께 보기: 상대는 지금 보이는 거래의 사진을 함께 보기 주소로만 읽을 수 있습니다.
        shared = self.partner.get("/api/shared-transactions?month=2026-11")
        [member] = shared.json["members"]
        [row] = member["transactions"]
        self.assertEqual((row["merchant"], row["receipts"]), ("가상 마트", [receipt]))
        image = self.partner.get(f"/api/shared-receipts/{receipt}")
        self.assertEqual((image.status_code, image.data), (200, JPEG))
        self.assertEqual(image.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(self.owner.get(f"/api/shared-receipts/{receipt}").status_code, 200)
        db = connect()
        with db:
            db.execute("INSERT INTO shared_hidden(transaction_id) VALUES(101)")
        db.close()
        self.assertEqual(self.partner.get(f"/api/shared-receipts/{receipt}").status_code, 404)
        db = connect()
        with db:
            db.execute("DELETE FROM shared_hidden")
            db.execute(
                "INSERT INTO transaction_exclusions(transaction_id,reason) VALUES(101,'not_spending')"
            )
        db.close()
        self.assertEqual(self.partner.get(f"/api/shared-receipts/{receipt}").status_code, 404)
        db = connect()
        with db:
            db.execute("DELETE FROM transaction_exclusions")
            db.execute(
                "INSERT INTO user_features(user_id,feature,enabled) VALUES(1,'share_transactions',0)"
            )
        db.close()
        self.assertEqual(self.partner.get(f"/api/shared-receipts/{receipt}").status_code, 404)
        self.assertEqual(self.partner.get("/api/shared-receipts/999").status_code, 404)
        # 저장할 때는 화면 버전이 필요합니다(공동 관리 중).
        from io import BytesIO

        missing = self.owner.post(
            "/api/transactions/101/receipts",
            data={"file": (BytesIO(JPEG), "receipt.jpg")},
            headers=HEADERS,
            content_type="multipart/form-data",
        )
        self.assertEqual(missing.status_code, 428)

    def test_deleting_the_transaction_deletes_photos(self):
        receipt = self.upload(self.owner, JPEG).json["id"]
        response = self.owner.delete(
            "/api/imports/11", headers={**HEADERS, "If-Match": self.revision(self.owner)}
        )
        self.assertEqual(response.status_code, 200)
        db = connect()
        try:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM transaction_receipts").fetchone()[0], 0
            )
        finally:
            db.close()
        self.assertEqual(self.owner.get(f"/api/receipts/{receipt}").status_code, 404)

    def test_sniff(self):
        self.assertEqual(receipts.sniff(JPEG), "image/jpeg")
        self.assertEqual(receipts.sniff(PNG), "image/png")
        self.assertEqual(receipts.sniff(WEBP), "image/webp")
        self.assertIsNone(receipts.sniff(b"RIFF\x00\x00\x00\x00WAVE...."))


class ReceiptBrowserTests(unittest.TestCase):
    setUp = ReceiptTests.setUp
    tearDown = ReceiptTests.tearDown
    revision = ReceiptTests.revision

    @unittest.skipUnless(
        os.environ.get("MOA_BROWSER_TESTS") == "1", "샌드박스 Chromium 환경에서 실행"
    )
    def test_receipt_screen(self):
        from werkzeug.serving import make_server, WSGIRequestHandler

        class QuietHandler(WSGIRequestHandler):
            def log(self, *args, **kwargs):
                pass

        server = make_server("127.0.0.1", 0, app, threaded=True, request_handler=QuietHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            subprocess.run(
                ["node", "tests/receipts-ui.cjs"],
                cwd=Path(__file__).resolve().parents[1],
                env={**os.environ, "MOA_TEST_URL": f"http://127.0.0.1:{server.server_port}"},
                check=True,
                timeout=90,
            )
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()


if __name__ == "__main__":
    unittest.main()
