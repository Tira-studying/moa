"""PWA 자원·로그인 경계와 실제 서비스 워커의 캐시 범위를 검증합니다.

브라우저 검증은 가상 거래와 임시 DB만 사용하며, 실제 홈 화면 설치는 기기에서 확인합니다.
"""

import io
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest

from PIL import Image
from werkzeug.serving import make_server, WSGIRequestHandler

from app import app, connect
from auth import set_password


class PwaAssetTests(unittest.TestCase):
    def setUp(self):
        self.previous = dict(app.config)
        self.directory = tempfile.TemporaryDirectory()
        app.config.update(
            TESTING=True,
            PUBLIC_ORIGIN="",
            DATABASE=str(Path(self.directory.name) / "pwa.sqlite3"),
        )
        set_password(connect, "fixture-owner-password")
        self.client = app.test_client()

    def tearDown(self):
        app.config.update(self.previous)
        self.directory.cleanup()

    def fetch(self, path):
        """정적 파일 응답도 검증이 끝나면 파일 핸들을 닫습니다."""
        response = self.client.get(path)
        self.addCleanup(response.close)
        return response

    def test_public_install_assets_do_not_open_private_routes(self):
        response = self.fetch("/static/manifest.webmanifest")
        self.assertEqual(response.status_code, 200)
        manifest = response.json
        self.assertEqual(manifest["id"], "/")
        self.assertEqual(manifest["start_url"], "/")
        self.assertEqual(manifest["scope"], "/")
        self.assertEqual(manifest["display"], "standalone")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        for icon in manifest["icons"]:
            response = self.fetch(icon["src"])
            self.assertEqual(response.status_code, 200)
            with Image.open(io.BytesIO(response.data)) as image:
                self.assertEqual(f"{image.width}x{image.height}", icon["sizes"])
        response = self.fetch("/static/icons/apple-touch-icon.png")
        with Image.open(io.BytesIO(response.data)) as image:
            self.assertEqual(image.size, (180, 180))
        self.assertEqual(self.fetch("/api/data").status_code, 401)
        self.assertEqual(self.fetch("/").location, "/login")

    def test_worker_scope_and_connection_page_are_available_without_login(self):
        response = self.fetch("/sw.js")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/javascript")
        self.assertEqual(response.headers["Service-Worker-Allowed"], "/")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        offline = self.fetch("/static/offline.html")
        self.assertEqual(offline.status_code, 200)
        self.assertIn("모아에 연결할 수 없어요", offline.text)
        login = self.fetch("/login")
        self.assertIn("/static/manifest.webmanifest", login.text)
        self.assertIn("/static/pwa.js", login.text)
        self.assertIn("pwa-install-guide", login.text)


@unittest.skipUnless(os.environ.get("MOA_BROWSER_TESTS") == "1", "샌드박스 브라우저가 필요합니다.")
class PwaBrowserTests(unittest.TestCase):
    def test_real_worker_offline_privacy_install_guidance_and_expired_session(self):
        class QuietHandler(WSGIRequestHandler):
            def log(self, *args, **kwargs):
                pass

        previous = dict(app.config)
        with tempfile.TemporaryDirectory() as directory:
            app.config.update(
                TESTING=True, PUBLIC_ORIGIN="", DATABASE=str(Path(directory) / "pwa.sqlite3")
            )
            set_password(connect, "fixture-owner-password")
            db = connect()
            with db:
                db.execute(
                    "INSERT INTO imports(card,filename,digest) VALUES('삼성','pwa-fixture.csv','pwa-fixture')"
                )
                db.execute(
                    "INSERT INTO transactions(import_id,used_on,merchant,amount) VALUES(1,'2026-11-01','가상PWA전용상점',1234)"
                )
            db.close()
            server = make_server("127.0.0.1", 0, app, threaded=True, request_handler=QuietHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                subprocess.run(
                    ["node", "tests/pwa-ui.cjs"],
                    cwd=Path(__file__).resolve().parents[1],
                    env={**os.environ, "MOA_TEST_URL": f"http://127.0.0.1:{server.server_port}"},
                    check=True,
                    timeout=55,
                )
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()
                app.config.update(previous)
