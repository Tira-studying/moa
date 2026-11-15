"""가상 응답으로 6개 카드사 시험의 전송 주소·추가 인증 상한·건수 검증을 확인합니다."""

import base64
import io
import importlib.util
import json
import os
from pathlib import Path
import unittest
import urllib.error
import urllib.parse
import unicodedata
from unittest.mock import Mock, patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

import codef_card as card


class CardVerificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=1024)
        cls.public = base64.b64encode(
            cls.key.public_key().public_bytes(
                serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
            )
        ).decode()

    def test_direct_login_uses_documented_fields_and_encrypted_password(self):
        payload = card.kb_payload(
            {"public_key": self.public}, "fixture-id", "fixture-password", "20260101", "20260107"
        )
        self.assertEqual(payload["organization"], "0301")
        self.assertEqual(payload["inquiryType"], "1")
        self.assertEqual(payload["memberStoreInfoType"], "0")
        self.assertNotIn("connectedId", payload)
        self.assertNotIn("cardPassword", payload)
        self.assertEqual(
            self.key.decrypt(base64.b64decode(payload["password"]), padding.PKCS1v15()),
            b"fixture-password",
        )

    def test_bad_dates_and_excessive_password_do_not_reach_network(self):
        for start, end in (
            ("20260101", "20260108"),
            ("20260230", "20260301"),
            ("20260102", "20260101"),
        ):
            with self.assertRaises(card.CardCheckError):
                card.validate_dates(start, end)
        with self.assertRaises(card.CardCheckError) as error:
            card.encrypt_password(self.public, "fixture-secret" * 100)
        self.assertNotIn("fixture-secret", str(error.exception))

    def test_codef_environment_picks_only_fixed_hosts(self):
        """MOA_CODEF_ENV는 demo·production 두 고정 주소만 고르고, 다른 값은 요청 전에 멈춥니다."""
        import codef_connection

        for value, host in (
            (None, "https://development.codef.io"),
            ("demo", "https://development.codef.io"),
            ("production", "https://api.codef.io"),
        ):
            environ = {} if value is None else {"MOA_CODEF_ENV": value}
            with patch.dict(os.environ, environ, clear=False):
                if value is None:
                    os.environ.pop("MOA_CODEF_ENV", None)
                self.assertEqual(codef_connection.api_host(), host)
        for bad in ("https://evil.example", "prod", "sandbox"):
            with patch.dict(os.environ, {"MOA_CODEF_ENV": bad}):
                with self.assertRaises(ValueError):
                    codef_connection.api_host()

    def test_samsung_uses_its_organization_without_card_pin(self):
        payload = card.approval_payload(
            {"public_key": self.public},
            "samsung",
            "fixture-id",
            "fixture-password",
            "20260101",
            "20260107",
        )
        self.assertEqual(payload["organization"], "0303")
        self.assertNotIn("loginCardNo", payload)
        self.assertNotIn("cardPassword", payload)

    def test_shinhan_lotte_woori_use_correct_organization_and_no_extra_personal_fields(self):
        for issuer, organization in (("shinhan", "0306"), ("lotte", "0311"), ("woori", "0309")):
            with self.subTest(issuer=issuer):
                payload = card.approval_payload(
                    {"public_key": self.public},
                    issuer,
                    "fixture-id",
                    "fixture-password",
                    "20260101",
                    "20260107",
                    user_name="가상사용자" if issuer == "lotte" else "",
                    identity="0001010" if issuer == "lotte" else "",
                    phone_no="01000000000" if issuer == "lotte" else "",
                    telecom="0" if issuer == "lotte" else "",
                )
                self.assertEqual(payload["organization"], organization)
                self.assertEqual(payload["memberStoreInfoType"], "0")
                self.assertEqual(payload["inquiryType"], "1")
                for field in ("loginCardNo", "cardPassword"):
                    self.assertNotIn(field, payload)
                if issuer == "lotte":
                    self.assertEqual(payload["userName"], "가상사용자")
                    self.assertEqual(payload["identity"], "0001010")
                    self.assertEqual(payload["phoneNo"], "01000000000")
                    self.assertEqual(payload["telecom"], "0")
                else:
                    self.assertNotIn("userName", payload)
                    self.assertNotIn("identity", payload)
                    self.assertNotIn("phoneNo", payload)
                    self.assertNotIn("telecom", payload)
                self.assertEqual(
                    self.key.decrypt(base64.b64decode(payload["password"]), padding.PKCS1v15()),
                    b"fixture-password",
                )

    def test_hyundai_requires_card_number_and_encrypts_pin_separately(self):
        payload = card.approval_payload(
            {"public_key": self.public},
            "hyundai",
            "fixture-id",
            "fixture-password",
            "20260101",
            "20260107",
            card_number="0" * 16,
            card_pin="0000",
        )
        self.assertEqual(payload["organization"], "0302")
        self.assertEqual(payload["loginCardNo"], "0" * 16)
        self.assertEqual(payload["cardNo"], "0" * 16)
        self.assertEqual(payload["inquiryType"], "1")
        self.assertEqual(payload["memberStoreInfoType"], "0")
        self.assertEqual(
            self.key.decrypt(base64.b64decode(payload["cardPassword"]), padding.PKCS1v15()), b"0000"
        )
        self.assertNotEqual(payload["cardPassword"], payload["password"])

    def test_lotte_rejects_missing_name_without_exposing_input(self):
        for name in ("", "  ", "가상\n사용자", "1234", "가" * 81):
            with self.assertRaises(card.CardCheckError):
                card.approval_payload(
                    {"public_key": self.public},
                    "lotte",
                    "fixture-id",
                    "fixture-password",
                    "20260101",
                    "20260107",
                    user_name=name,
                )
        with self.assertRaises(card.CardCheckError) as error:
            card.approval_payload(
                {"public_key": self.public},
                "shinhan",
                "fixture-id",
                "fixture-password",
                "20260101",
                "20260107",
                user_name="fixture-private-name",
            )
        self.assertNotIn("fixture-private", str(error.exception))

    def test_lotte_name_normalizes_only_boundaries_and_canonical_korean(self):
        decomposed = unicodedata.normalize("NFD", "가상사용자")
        self.assertEqual(
            card.validate_lotte_name("\ufeff \u200b" + decomposed + " \u200b\r\n"), "가상사용자"
        )
        self.assertEqual(card.validate_lotte_name("  FAMILY  Given  "), "FAMILY  Given")
        for value in ("\u200b \ufeff", "가상\u200b사용자", "가상\x00사용자"):
            with self.assertRaises(card.CardCheckError) as error:
                card.validate_lotte_name(value)
            self.assertNotIn("가상", str(error.exception))

    def test_lotte_identity_accepts_only_seven_ascii_digits_and_never_truncates(self):
        for value in (
            "",
            "0" * 13,
            "0" * 6,
            "0" * 8,
            "０００１０１０",
            "000101-0",
            "fixture-private",
        ):
            with self.assertRaises(card.CardCheckError) as error:
                card.approval_payload(
                    {"public_key": self.public},
                    "lotte",
                    "fixture-id",
                    "fixture-password",
                    "20260101",
                    "20260107",
                    user_name="가상사용자",
                    identity=value,
                )
            self.assertNotIn("fixture-private", str(error.exception))
            self.assertNotIn("0" * 13, str(error.exception))
        with self.assertRaises(card.CardCheckError):
            card.approval_payload(
                {"public_key": self.public},
                "shinhan",
                "fixture-id",
                "fixture-password",
                "20260101",
                "20260107",
                identity="0001010",
            )

    def test_cf12851_explains_missing_name_without_provider_message(self):
        for code, hint in (("CF-12851", "사용자 이름"), ("CF-12850", "본인 확인 정보")):
            with patch(
                "codef_card.post_approval",
                return_value={"result": {"code": code, "message": "fixture-private-name"}},
            ) as post:
                with self.assertRaises(card.CardCheckError) as error:
                    card.query_approvals("fixture-token", {}, Mock())
            self.assertIn(hint, str(error.exception))
            self.assertNotIn("fixture-private", str(error.exception))
            self.assertEqual(post.call_count, 1)

    def test_lotte_phone_and_all_carrier_codes_are_validated_before_transmission(self):
        def build(phone="010-0000-0000", telecom="0", issuer="lotte"):
            return card.approval_payload(
                {"public_key": self.public},
                issuer,
                "fixture-id",
                "fixture-password",
                "20260101",
                "20260107",
                user_name="가상사용자" if issuer == "lotte" else "",
                identity="0001010" if issuer == "lotte" else "",
                phone_no=phone,
                telecom=telecom,
            )

        for telecom in ("0", "1", "2", "3", "4", "5"):
            payload = build(telecom=telecom)
            self.assertEqual(payload["phoneNo"], "01000000000")
            self.assertEqual(payload["telecom"], telecom)
        for phone, telecom in (
            ("", "0"),
            ("fixture-private-phone", "0"),
            ("01000000000", "6"),
            ("01000000000", 0),
            ("01000000000", ""),
        ):
            with self.assertRaises(card.CardCheckError) as error:
                build(phone, telecom)
            self.assertNotIn("fixture-private", str(error.exception))
        with self.assertRaises(card.CardCheckError):
            build(issuer="shinhan")

    def test_cf12401_only_discloses_allowlisted_parameter_names(self):
        response = {
            "result": {
                "code": "CF-12401",
                "extraMessage": "누락: phoneNo, telecom; fixture-private-value",
            }
        }
        with patch("codef_card.post_approval", return_value=response) as post:
            with self.assertRaises(card.CardCheckError) as error:
                card.query_approvals("fixture-token", {}, Mock())
        self.assertIn("phoneNo, telecom", str(error.exception))
        self.assertNotIn("fixture-private", str(error.exception))
        self.assertEqual(post.call_count, 1)
        self.assertIn(
            "확인 불가",
            card.missing_parameter_hint({"extraMessage": "fixture-private-phoneNoValue"}),
        )

    def test_wrong_issuer_or_missing_hyundai_fields_fail_before_network(self):
        for issuer, number, pin in (
            ("unsupported", "", ""),
            ("hyundai", "", ""),
            ("hyundai", "0" * 16, "00"),
            ("hyundai", "fixture-private-number", "0000"),
            ("samsung", "0" * 16, "0000"),
        ):
            with self.assertRaises(card.CardCheckError) as error:
                card.approval_payload(
                    {"public_key": self.public},
                    issuer,
                    "fixture-id",
                    "fixture-password",
                    "20260101",
                    "20260107",
                    card_number=number,
                    card_pin=pin,
                )
            self.assertNotIn("fixture", str(error.exception))

    def test_fixed_https_url_no_redirect_and_encoded_json(self):
        response = io.BytesIO(
            urllib.parse.quote(json.dumps({"result": {"code": "CF-00000"}, "data": []})).encode()
        )
        response.status = 200
        opener = Mock()
        opener.open.return_value = response
        with patch("codef_card.urllib.request.build_opener", return_value=opener) as build:
            self.assertEqual(card.post_approval("fixture-token", {"id": "fixture-id"})["data"], [])
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://development.codef.io" + card.APPROVAL_PATH)
        self.assertEqual(request.get_header("Authorization"), "Bearer fixture-token")
        self.assertEqual(
            json.loads(urllib.parse.unquote(request.data.decode())), {"id": "fixture-id"}
        )
        self.assertTrue(
            any(isinstance(handler, card.NoRedirect) for handler in build.call_args.args)
        )

    def test_http_and_invalid_response_never_expose_body_or_retry(self):
        opener = Mock()
        opener.open.side_effect = urllib.error.HTTPError(
            "https://development.codef.io" + card.APPROVAL_PATH,
            403,
            "fixture-private-message",
            {},
            io.BytesIO(b"fixture-secret"),
        )
        with patch("codef_card.urllib.request.build_opener", return_value=opener):
            with self.assertRaises(card.CardCheckError) as error:
                card.post_approval("fixture-token", {})
        self.assertNotIn("fixture", str(error.exception))
        self.assertEqual(opener.open.call_count, 1)

    def test_malformed_and_oversized_responses_are_rejected(self):
        for content in (b"fixture-private-response", b"[1,2]", b"x" * (card.BODY_LIMIT + 1)):
            response = io.BytesIO(content)
            response.status = 200
            opener = Mock()
            opener.open.return_value = response
            with patch("codef_card.urllib.request.build_opener", return_value=opener):
                with self.assertRaises(card.CardCheckError) as error:
                    card.post_approval("fixture-token", {})
            self.assertNotIn("fixture", str(error.exception))

    def test_two_way_uses_original_payload_and_only_documented_session_fields(self):
        challenge = {
            "result": {"code": "CF-03002"},
            "data": dict(
                continue2Way=True,
                method="smsAuthNo",
                jobIndex=0,
                threadIndex=1,
                jti="fixture-session",
                twoWayTimestamp=1,
                extraInfo="fixture-private",
            ),
        }
        answer = Mock(return_value="123456")
        with patch(
            "codef_card.post_approval",
            side_effect=[challenge, {"result": {"code": "CF-00000"}, "data": []}],
        ) as post:
            self.assertEqual(
                card.query_approvals("fixture-token", {"id": "fixture-id"}, answer), []
            )
        resumed = post.call_args.args[1]
        self.assertTrue(resumed["is2Way"])
        self.assertEqual(resumed["smsAuthNo"], "123456")
        self.assertEqual(resumed["id"], "fixture-id")
        self.assertNotIn("extraInfo", resumed["twoWayInfo"])

    def test_challenge_loop_is_bounded(self):
        response = {
            "result": {"code": "CF-03002"},
            "data": dict(
                continue2Way=True,
                method="simpleAuth",
                jobIndex=0,
                threadIndex=0,
                jti="fixture",
                twoWayTimestamp=1,
            ),
        }
        with patch("codef_card.post_approval", return_value=response) as post:
            with self.assertRaises(card.CardCheckError):
                card.query_approvals("fixture-token", {}, lambda method: "1")
        self.assertEqual(post.call_count, card.MAX_CHALLENGES + 1)

    def test_lotte_sms_without_method_resumes_with_exact_session_fields(self):
        info = dict(jobIndex=0, threadIndex=0, jti="fixture-session", twoWayTimestamp="1234567890")
        payload = dict(
            organization="0311",
            id="fixture-id",
            password="fixture-encrypted",
            userName="가상사용자",
            identity="0001010",
            startDate="20260101",
            endDate="20260107",
        )
        challenge = {
            "result": {"code": "CF-03002"},
            "data": dict(continue2Way=True, **info, extraInfo="fixture-private"),
        }
        answer = Mock(return_value="123456")
        with patch(
            "codef_card.post_approval",
            side_effect=[challenge, {"result": {"code": "CF-00000"}, "data": []}],
        ) as post:
            self.assertEqual(card.query_approvals("fixture-token", payload, answer), [])
        answer.assert_called_once_with("smsAuthNo")
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args_list[0].args[1], payload)
        self.assertEqual(
            post.call_args_list[1].args[1],
            dict(payload, is2Way=True, smsAuthNo="123456", twoWayInfo=info),
        )
        self.assertNotIn("is2Way", payload)

    def test_lotte_fallback_needs_continue_flag_and_does_not_override_unknown_method(self):
        for organization, flag, method in (
            ("0306", True, None),
            ("0311", False, None),
            ("0311", "true", None),
            ("0311", True, "unsupported"),
        ):
            challenge = {
                "result": {"code": "CF-03002"},
                "data": dict(
                    continue2Way=flag,
                    method=method,
                    jobIndex=0,
                    threadIndex=0,
                    jti="fixture",
                    twoWayTimestamp=1,
                ),
            }
            answer = Mock()
            with patch("codef_card.post_approval", return_value=challenge) as post:
                with self.assertRaises(card.CardCheckError):
                    card.query_approvals("fixture-token", {"organization": organization}, answer)
            answer.assert_not_called()
            self.assertEqual(post.call_count, 1)

    def test_lotte_comm_session_is_carried_in_memory_with_latest_two_way_info(self):
        info = dict(jobIndex=0, threadIndex=0, jti="fixture-session", twoWayTimestamp=1)
        first = {
            "result": {"code": "CF-03002"},
            "data": dict(continue2Way=True, **info, commSession="fixture-private-session"),
        }
        updated = dict(info, threadIndex=1, twoWayTimestamp=2)
        second = {"result": {"code": "CF-03002"}, "data": dict(continue2Way=True, **updated)}
        payload = dict(organization="0311", phoneNo="01000000000", telecom="0")
        with patch(
            "codef_card.post_approval",
            side_effect=[first, second, {"result": {"code": "CF-00000"}, "data": []}],
        ) as post:
            self.assertEqual(
                card.query_approvals("fixture-token", payload, lambda method: "123456"), []
            )
        self.assertEqual(post.call_count, 3)
        self.assertEqual(post.call_args_list[1].args[1]["commSession"], "fixture-private-session")
        resumed = post.call_args_list[2].args[1]
        self.assertEqual(resumed["commSession"], "fixture-private-session")
        self.assertEqual(resumed["twoWayInfo"], updated)
        self.assertEqual(resumed["phoneNo"], payload["phoneNo"])
        self.assertNotIn("commSession", payload)

    def test_lotte_missing_session_information_does_not_request_sms_code(self):
        for missing in ("jobIndex", "threadIndex", "jti", "twoWayTimestamp"):
            data = dict(
                continue2Way=True, jobIndex=0, threadIndex=0, jti="fixture", twoWayTimestamp=1
            )
            del data[missing]
            answer = Mock()
            with patch(
                "codef_card.post_approval",
                return_value={"result": {"code": "CF-03002"}, "data": data},
            ) as post:
                with self.assertRaises(card.CardCheckError):
                    card.query_approvals("fixture-token", {"organization": "0311"}, answer)
            answer.assert_not_called()
            self.assertEqual(post.call_count, 1)

    def test_lotte_invalid_sms_answer_is_not_sent(self):
        challenge = {
            "result": {"code": "CF-03002"},
            "data": dict(
                continue2Way=True, jobIndex=0, threadIndex=0, jti="fixture", twoWayTimestamp=1
            ),
        }
        with patch("codef_card.post_approval", return_value=challenge) as post:
            with self.assertRaises(card.CardCheckError) as error:
                card.query_approvals(
                    "fixture-token", {"organization": "0311"}, lambda method: "fixture-private-code"
                )
        self.assertEqual(post.call_count, 1)
        self.assertNotIn("fixture-private", str(error.exception))

    def test_lotte_register_and_nested_challenges_wait_for_answer_before_resending(self):
        info = dict(jobIndex=0, threadIndex=0, jti="fixture-private-session", twoWayTimestamp=1)
        for code in ("CF-03002", "CF-04034"):
            for wrapped in (False, True):
                with self.subTest(code=code, wrapped=wrapped):
                    source = dict(continue2Way=True, twoWayInfo=info)
                    response = {"result": {"code": code}, "data": source}
                    if wrapped:
                        response = {
                            "result": {"code": "CF-00000"},
                            "data": {
                                "successList": [],
                                "errorList": [
                                    dict(
                                        code=code,
                                        organization="0311",
                                        businessType="CD",
                                        clientType="P",
                                        data=source,
                                    )
                                ],
                            },
                        }
                    answer = Mock(return_value="123456")
                    send = Mock(
                        side_effect=[response, {"result": {"code": "CF-00000"}, "data": []}]
                    )

                    def wait_for_operator(method):
                        self.assertEqual(send.call_count, 1)
                        return answer(method)

                    self.assertEqual(
                        card.query_approvals(
                            "fixture-token",
                            {"accountList": [dict(organization="0311")]},
                            wait_for_operator,
                            sender=send,
                            organization="0311",
                        ),
                        [],
                    )
                    answer.assert_called_once_with("smsAuthNo")
                    self.assertEqual(send.call_args.args[1]["twoWayInfo"], info)
                    self.assertEqual(send.call_args.args[1]["smsAuthNo"], "123456")

    def test_cf04034_without_complete_context_cannot_ask_or_send_sms(self):
        info = dict(jobIndex=0, threadIndex=0, jti="fixture-private", twoWayTimestamp=1)
        for data in (None, {}, dict(continue2Way="true", **info), dict(continue2Way=True)):
            answer = Mock()
            send = Mock(return_value={"result": {"code": "CF-04034"}, "data": data})
            with self.assertRaises(card.CardCheckError) as error:
                card.query_approvals("fixture-token", {"organization": "0311"}, answer, sender=send)
            self.assertEqual(error.exception.code, "CF-04034")
            self.assertNotIn("fixture-private", str(error.exception))
            answer.assert_not_called()
            self.assertEqual(send.call_count, 1)

    def test_account_challenge_does_not_pick_other_institution_or_conflicting_metadata(self):
        info = dict(jobIndex=0, threadIndex=0, jti="fixture-private", twoWayTimestamp=1)
        for organization in ("0303", None):
            response = {
                "result": {"code": "CF-00000"},
                "data": {
                    "errorList": [
                        dict(organization=organization, code="CF-03002", continue2Way=True, **info)
                    ]
                },
            }
            self.assertIsNone(card.two_way_challenge(response, "0311"))
        with self.assertRaises(card.CardCheckError):
            card.two_way_challenge(
                {
                    "result": {"code": "CF-03002"},
                    "data": dict(continue2Way=True, **info, twoWayInfo=dict(info, threadIndex=1)),
                },
                "0311",
            )

    def test_unknown_challenge_does_not_prompt_or_forward_arbitrary_fields(self):
        answer = Mock()
        response = {
            "result": {"code": "CF-03002"},
            "data": dict(continue2Way=True, method="fixture-private-method"),
        }
        with patch("codef_card.post_approval", return_value=response) as post:
            with self.assertRaises(card.CardCheckError) as error:
                card.query_approvals("fixture-token", {}, answer)
        self.assertNotIn("fixture-private", str(error.exception))
        answer.assert_not_called()
        self.assertEqual(post.call_count, 1)

    def test_login_failure_is_not_retried_and_provider_message_is_hidden(self):
        for code in ("CF-12108", "CF-12834", "fixture-private-code"):
            response = {"result": {"code": code, "message": "fixture-private-message"}}
            with patch("codef_card.post_approval", return_value=response) as post:
                with self.assertRaises(card.CardCheckError) as error:
                    card.query_approvals("fixture-token", {}, Mock())
            self.assertEqual(post.call_count, 1)
            self.assertNotIn("fixture-private", str(error.exception))

    def test_cf04000_reports_registration_failure_without_inventing_password_cause(self):
        answer = Mock()
        with patch(
            "codef_card.post_approval",
            return_value={"result": {"code": "CF-04000", "message": "fixture-private-value"}},
        ) as send:
            with self.assertRaises(card.CardCheckError) as caught:
                card.query_approvals("fixture-token", {"organization": "0302"}, answer)
        self.assertEqual(caught.exception.code, "CF-04000")
        self.assertIn("사용자 계정정보 등록에 실패", str(caught.exception))
        self.assertIn("비밀번호 오류로 단정하지", str(caught.exception))
        self.assertNotIn("fixture-private-value", str(caught.exception))
        self.assertEqual(send.call_count, 1)
        answer.assert_not_called()

    def test_failure_hint_classifies_topics_without_revealing_provider_text(self):
        for message, label in (
            ("가상계정 fixture-private: 추가 인증 필요", "추가 본인 인증"),
            ("fixture-private 보안문자를 입력하세요", "보안문자·자동입력 방지"),
            ("fixture-private 비밀번호 변경 안내", "비밀번호 변경·만료"),
            ("fixture-private 로그인 정보 확인", "로그인·계정 정보"),
            ("fixture-private 접속 제한 안내", "접속 제한·잠금"),
            ("fixture-private 시스템 점검", "점검·일시적 장애"),
            ("fixture-private 상품 이용권한", "상품 이용 권한"),
        ):
            with self.subTest(label=label):
                hint = card.failure_hint({"message": message})
                self.assertIn(label, hint)
                self.assertNotIn("fixture-private", hint)
        hint = card.failure_hint({"message": None, "extraMessage": {"private": "fixture"}})
        self.assertIn("분류 불가", hint)
        self.assertNotIn("fixture", hint)

    def test_cf12800_reports_safe_topic_and_is_not_assumed_wrong_password(self):
        response = {
            "result": {
                "code": "CF-12800",
                "message": "fixture-private",
                "extraMessage": "보안 문자 확인 필요",
            }
        }
        with patch("codef_card.post_approval", return_value=response) as post:
            with self.assertRaises(card.CardCheckError) as error:
                card.query_approvals("fixture-token", {}, Mock())
        self.assertEqual(post.call_count, 1)
        message = str(error.exception)
        self.assertIn("CF-12800", message)
        self.assertIn("보안문자·자동입력 방지", message)
        self.assertIn("원인 확정 아님", message)
        self.assertNotIn("fixture-private", message)

    def test_real_field_names_and_ambiguous_cancellations_keep_separate_counts(self):
        row = dict(
            resUsedDate="20260101",
            resUsedTime="120000",
            resUsedAmount="1,234",
            resCancelYN="0",
            resMemberStoreName="가상 상점",
            resCardNo="fixture-private",
        )
        rows = [dict(row, resCancelYN=status) for status in ("0", "1", "2", "3")]
        rows[1]["resUsedAmount"] = "-1234"
        rows.append(dict(row, resUsedDate="20260230"))
        rows.append(dict(row, resUsedAmount="not-an-amount"))
        result = card.summarize(rows)
        self.assertEqual(
            result, dict(total=6, approval=1, cancel=1, partial=1, declined=1, invalid=2)
        )
        self.assertNotIn("fixture-private", json.dumps(result))
        self.assertNotIn("resMemberStoreName", result)
        self.assertNotIn("amount", result)

    def test_empty_and_single_row_are_valid_but_unknown_structure_is_not_zero(self):
        self.assertEqual(card.summarize([])["total"], 0)
        row = dict(resUsedDate="20260101", resUsedTime="000000", resUsedAmount="0", resCancelYN="0")
        self.assertEqual(card.summarize(row)["approval"], 1)
        for value in (None, {}, {"unexpected": []}):
            with self.assertRaises(card.CardCheckError):
                card.summarize(value)

    def test_cli_hides_secrets_and_returns_only_summary(self):
        spec = importlib.util.spec_from_file_location(
            "verify_codef_fixture",
            Path(__file__).resolve().parents[1] / "scripts/verify-codef-card.py",
        )
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)

        class Terminal(io.StringIO):
            def isatty(self):
                return True

        for issuer in card.CARD_OPTIONS:
            output = Terminal()
            arguments = [] if issuer == "samsung" else ["--card", issuer]
            secrets = ["fixture-id", "fixture-password"]
            if issuer == "hyundai":
                secrets += ["0" * 16, "0000"]
            if issuer == "lotte":
                secrets += ["가상사용자", "0001010", "01000000000", "0"]
            with (
                patch.object(cli.sys, "argv", ["verify-codef-card.py", *arguments]),
                patch.object(cli.sys, "stdin", Terminal()),
                patch.object(cli.sys, "stdout", output),
                patch.object(cli, "configuration", return_value={"public_key": self.public}),
                patch.object(cli.getpass, "getpass", side_effect=secrets) as hidden_input,
                patch.object(cli, "issue_token", return_value="fixture-token"),
                patch.object(cli, "query_approvals", return_value=[]) as query,
            ):
                self.assertEqual(cli.main(), 0)
            self.assertEqual(hidden_input.call_count, len(secrets))
            self.assertNotIn("fixture", output.getvalue())
            self.assertNotIn("0" * 16, output.getvalue())
            self.assertNotIn("가상사용자", output.getvalue())
            self.assertNotIn("0001010", output.getvalue())
            self.assertNotIn("01000000000", output.getvalue())
            self.assertIn("전체 0건", output.getvalue())
            self.assertIn(card.CARD_OPTIONS[issuer][1], output.getvalue())
            payload = query.call_args.args[1]
            self.assertEqual(payload["organization"], card.CARD_OPTIONS[issuer][0])
            if issuer == "lotte":
                self.assertEqual(payload["userName"], "가상사용자")
                self.assertEqual(payload["identity"], "0001010")
                self.assertEqual(payload["phoneNo"], "01000000000")
                self.assertEqual(payload["telecom"], "0")
            if issuer == "hyundai":
                self.assertEqual(
                    self.key.decrypt(base64.b64decode(payload["cardPassword"]), padding.PKCS1v15()),
                    b"0000",
                )

    def test_cli_card_selection_and_rejected_secret_arguments(self):
        spec = importlib.util.spec_from_file_location(
            "verify_codef_selection",
            Path(__file__).resolve().parents[1] / "scripts/verify-codef-card.py",
        )
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        for issuer in card.CARD_OPTIONS:
            with patch.object(cli.sys, "argv", ["verify-codef-card.py", "--card", issuer]):
                self.assertEqual(cli.selected_card(), issuer)
        with patch.object(cli.sys, "argv", ["verify-codef-card.py", "fixture-private-password"]):
            with self.assertRaises(card.CardCheckError) as error:
                cli.selected_card()
        self.assertNotIn("fixture-private", str(error.exception))


if __name__ == "__main__":
    unittest.main()
