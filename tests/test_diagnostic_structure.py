"""검증 범위: 진단 구조의 개인정보 제거.

실제 명세서·비밀번호 대신 가상 데이터와 임시 DB를 사용합니다.
브라우저가 필요한 검증은 MOA_BROWSER_TESTS=1인 환경에서 실행합니다.
"""

import unittest
from diagnostic_structure import sanitize_structure


class StructureTests(unittest.TestCase):
    def test_identifiers_urls_scripts_and_values_are_removed(self):
        raw = """<html><head><script>SECRET_SCRIPT_98472</script></head><body>
        <form action="https://bank.example/private?token=SECRET_TOKEN_8273"><input type="password" value="SECRET_PASSWORD_8172">
        <table id="SECRET_ID_92381" class="private-SECRET_NAME merchant"><tr><td>개인가맹점_93821</td><td class="price private-SECRET_CLASS">94,880원</td></tr><tr><td><span>26. 9. 8</span> 본인 786*</td></tr></table>
        <a href="https://private.example/SECRET_URL" onclick="SECRET_HANDLER()">더보기 (25/38)</a></form>
        <div data-moa-hidden="1">개인주소_9281</div></body></html>"""
        sample = sanitize_structure(raw)
        self.assertTrue(sample["fictional"])
        html = sample["html"]
        for token in [
            "SECRET",
            "개인가맹점",
            "개인주소",
            "94,880",
            "786",
            "93821",
            "https://",
            "action=",
            "value=",
            "id=",
        ]:
            self.assertNotIn(token, html)
        self.assertIn("11,111원", html)
        self.assertIn('class="price"', html)
        self.assertIn("더보기 (25/38)", html)
        self.assertIn("hidden=", html)
        self.assertIn('data-click="1"', html)
        self.assertIn('data-link-kind="external"', html)

    def test_scripts_and_nested_attributes_cannot_escape(self):
        raw = '<html><body><svg><script>SECRET</script></svg><img src="SECRET" onerror="SECRET"><span title="SECRET">&lt;script&gt;SECRET&lt;/script&gt;</span></body></html>'
        html = sanitize_structure(raw)["html"]
        self.assertNotIn("SECRET", html)
        self.assertNotIn("<script", html)
        self.assertNotIn("onerror", html)
        self.assertNotIn("src=", html)

    def test_large_document_is_bounded_and_explicitly_truncated(self):
        raw = "<html><body>" + ("<span>private-data-982734</span>" * 15000) + "</body></html>"
        sample = sanitize_structure(raw)
        self.assertTrue(sample["truncated"])
        self.assertLess(len(sample["html"]), 501000)
        self.assertNotIn("private-data", sample["html"])

    def test_worker_raw_html_is_sanitized_before_exception_leaves_reader(self):
        import json
        from unittest.mock import MagicMock, patch
        from secure_html import unlock_statement, StatementReadError

        process = MagicMock()
        process.communicate.return_value = (
            json.dumps(
                {
                    "error": "incomplete",
                    "rawStructure": "<html><body><p>SECRET_CUSTOMER_992847</p></body></html>",
                }
            ).encode(),
            b"",
        )
        with (
            patch("secure_html.subprocess.Popen", return_value=process),
            patch("secure_html.os.killpg"),
        ):
            with self.assertRaises(StatementReadError) as caught:
                unlock_statement(b"<html>fixture</html>", "fixture-only")
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertNotIn("SECRET", caught.exception.structure["html"])
