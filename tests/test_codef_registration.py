"""가상 응답으로 등록/승인 필드 차이와 안전한 기관 오류 안내를 검증합니다.

실제 카드번호·로그인 정보·CODEF 요청은 사용하지 않습니다.
"""

import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import codef_card as card


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "connect_codef_registration",
            Path(__file__).resolve().parents[1] / "scripts/connect-codef-card.py",
        )
        self.cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.cli)

    def test_registration_uses_card_no_only_for_hyundai_and_preserves_approval_fields(self):
        for issuer, organization in (("hyundai", "0302"), ("woori", "0309")):
            with self.subTest(issuer=issuer):
                payload = dict(
                    organization=organization,
                    loginType="1",
                    id="fixture-id",
                    password="fixture-encrypted-password",
                    startDate="20260101",
                    endDate="20260107",
                    memberStoreInfoType="0",
                )
                if issuer == "hyundai":
                    payload.update(loginCardNo="0" * 16, cardPassword="fixture-encrypted-pin")
                original = dict(payload)
                client = Mock()
                client.send.return_value = {
                    "result": {"code": "CF-00000"},
                    "data": {
                        "connectedId": "fixture-connected",
                        "errorList": [],
                        "successList": [
                            dict(
                                organization=organization,
                                businessType="CD",
                                clientType="P",
                                code="CF-00000",
                            )
                        ],
                    },
                }
                operator = {
                    "read_card_payload": Mock(return_value=payload),
                    "answer_challenge": Mock(),
                }
                with patch("builtins.print") as output:
                    self.assertEqual(
                        self.cli.register_account(client, {}, "fixture-token", issuer, operator),
                        "fixture-connected",
                    )
                token, request, operation = client.send.call_args.args
                self.assertEqual(operation, "register")
                entry = request["accountList"][0]
                self.assertEqual(entry["organization"], organization)
                for name in ("loginCardNo", "startDate", "memberStoreInfoType"):
                    self.assertNotIn(name, entry)
                if issuer == "hyundai":
                    self.assertEqual(entry["cardNo"], "0" * 16)
                    self.assertEqual(entry["cardPassword"], "fixture-encrypted-pin")
                else:
                    self.assertNotIn("cardNo", entry)
                    self.assertNotIn("cardPassword", entry)
                self.assertEqual(payload, original)
                self.assertNotIn("fixture-encrypted", str(output.call_args_list))

    def test_cli_preserves_safe_institution_code_and_parameter_without_retry(self):
        client = Mock()
        client.send.return_value = {
            "result": {"code": "CF-04000", "message": "fixture-private-login"},
            "data": {
                "successList": [],
                "errorList": [
                    {
                        "organization": "0302",
                        "code": "CF-12401",
                        "extraMessage": "cardNo: fixture-private-number",
                    }
                ],
            },
        }
        operator = {
            "read_card_payload": Mock(
                return_value=dict(
                    organization="0302", loginCardNo="0" * 16, cardPassword="fixture-encrypted-pin"
                )
            ),
            "answer_challenge": Mock(),
        }
        with patch("builtins.print"):
            with self.assertRaises(self.cli.RegistrationError) as raised:
                self.cli.register_account(client, {}, "fixture-token", "hyundai", operator)
        self.assertEqual(client.send.call_count, 1)
        operator["answer_challenge"].assert_not_called()
        error = raised.exception
        self.assertEqual(error.code, "CF-04000")
        self.assertFalse(error.allow_login_retry)
        self.assertIn("기관 상세 코드: CF-12401", str(error))
        self.assertIn("cardNo", str(error))
        self.assertNotIn("fixture-private", str(error))

    def test_wrong_or_ambiguous_institution_details_are_not_selected(self):
        entry = dict(organization="0302", code="CF-12401", extraMessage="cardNo: fixture-private")
        for errors, success in (
            ([dict(entry, organization="0309")], []),
            ([entry, entry], []),
            ([dict(entry, countryCode="OTHER")], []),
            ([dict(entry, clientType="B")], []),
            ([dict(entry, code="fixture-private-code")], []),
            ([entry], [{"code": "CF-00000"}]),
        ):
            with self.subTest(errors=errors):
                self.assertEqual(
                    card.registration_failure_hint(
                        {"data": {"errorList": errors, "successList": success}}, "0302"
                    ),
                    "",
                )
        hint = card.registration_failure_hint(
            {"data": {"errorList": [dict(entry, organization="0309", code="CF-12800")]}}, "0309"
        )
        self.assertIn("CF-12800", hint)
        self.assertIn("분류 불가", hint)
        self.assertNotIn("fixture-private", hint)

    def test_documented_user_actions_do_not_expose_provider_values_or_retry(self):
        for code, action in (("CF-12800", "홈페이지 아이디"), ("CF-12831", "본인 인증")):
            with self.subTest(code=code):
                response = {"result": {"code": code, "extraMessage": "fixture-private-value"}}
                with patch("codef_card.post_approval", return_value=response) as post:
                    with self.assertRaises(card.CardCheckError) as error:
                        card.query_approvals("fixture-token", {"organization": "0302"}, Mock())
                self.assertEqual(post.call_count, 1)
                self.assertIn(action, str(error.exception))
                self.assertNotIn("fixture-private", str(error.exception))

    def test_unknown_or_malformed_codes_keep_safe_topic_classification(self):
        for code in (None, [], {}, "fixture-private-code"):
            hint = card.failure_hint({"code": code, "message": "fixture-private 접속 제한 안내"})
            self.assertIn("접속 제한·잠금", hint)
            self.assertNotIn("fixture-private", hint)
