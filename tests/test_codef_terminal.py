"""가상 입력과 PTY로 한글 숨김 입력·지우기·인코딩 오류의 안전한 처리를 확인합니다."""

import importlib.util
import io
import os
from pathlib import Path
import pty
import sys
import termios
import unittest
from unittest.mock import patch

from codef_card import CardCheckError


class CodefTerminalTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "verify_codef_terminal",
            Path(__file__).resolve().parents[1] / "scripts/verify-codef-card.py",
        )
        self.cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.cli)

    def test_invalid_utf8_is_reentered_without_printing_input_or_exception(self):
        error = UnicodeDecodeError("utf-8", b"\xfffixture-private", 0, 1, "fixture-private")
        output = io.StringIO()
        with (
            patch.object(self.cli.getpass, "getpass", side_effect=[error, "가상사용자"]),
            patch.object(self.cli.sys, "stdout", output),
        ):
            self.assertEqual(self.cli.read_hidden("가상 입력: "), "가상사용자")
        self.assertNotIn("fixture-private", output.getvalue())
        self.assertNotIn("가상사용자", output.getvalue())
        self.assertIn("붙여 넣고", output.getvalue())

    def test_repeated_invalid_utf8_stops_without_network_or_raw_error(self):
        error = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "fixture-private")
        with (
            patch.object(self.cli.getpass, "getpass", side_effect=error) as hidden,
            patch.object(self.cli.sys, "stdout", io.StringIO()),
            patch.object(self.cli, "issue_token") as token,
        ):
            with self.assertRaises(CardCheckError) as caught:
                self.cli.read_hidden("가상 입력: ")
        self.assertEqual(hidden.call_count, 3)
        self.assertNotIn("fixture-private", str(caught.exception))
        token.assert_not_called()

    def test_echo_fallback_is_refused(self):
        def warn(prompt):
            self.cli.warnings.warn("fixture-private", self.cli.getpass.GetPassWarning)

        with patch.object(self.cli.getpass, "getpass", side_effect=warn):
            with self.assertRaises(CardCheckError) as caught:
                self.cli.read_hidden("가상 입력: ")
        self.assertIn("숨길 수 없는", str(caught.exception))
        self.assertNotIn("fixture-private", str(caught.exception))

    def test_sms_challenge_uses_hidden_reader_and_does_not_print_code(self):
        output = io.StringIO()
        with (
            patch.object(self.cli, "read_hidden", return_value="123456") as hidden,
            patch.object(self.cli.sys, "stdout", output),
        ):
            self.assertEqual(self.cli.answer_challenge("smsAuthNo"), "123456")
        hidden.assert_called_once()
        self.assertIn("문자", hidden.call_args.args[0])
        self.assertNotIn("123456", output.getvalue())

    def test_sms_blank_input_waits_locally_and_does_not_finish_before_valid_code(self):
        output = io.StringIO()
        with (
            patch.object(
                self.cli, "read_hidden", side_effect=["", "fixture-invalid", "123456"]
            ) as hidden,
            patch.object(self.cli.sys, "stdout", output),
        ):
            self.assertEqual(self.cli.answer_challenge("smsAuthNo"), "123456")
        self.assertEqual(hidden.call_count, 3)
        self.assertIn("아직 전송하지 않았습니다", output.getvalue())
        self.assertNotIn("123456", output.getvalue())
        self.assertNotIn("fixture-invalid", output.getvalue())

    def test_lotte_name_reasks_only_invalid_name_without_printing_it(self):
        output = io.StringIO()
        with (
            patch.object(
                self.cli,
                "read_hidden",
                side_effect=["", "가상\u200b사용자", "\ufeff가상사용자\u200b"],
            ) as hidden,
            patch.object(self.cli.sys, "stdout", output),
            patch.object(self.cli, "issue_token") as token,
        ):
            self.assertEqual(self.cli.read_lotte_name(), "가상사용자")
        self.assertEqual(hidden.call_count, 3)
        token.assert_not_called()
        self.assertNotIn("가상사용자", output.getvalue())
        self.assertIn("이름이 비어", output.getvalue())
        self.assertIn("이름 입력 형식: 정상", output.getvalue())

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux IUTF8 터미널 검증")
    def test_korean_backspace_preserves_utf8_and_restores_terminal_flags(self):
        master, slave = pty.openpty()
        try:
            original = termios.tcgetattr(slave)
            original[0] &= ~0x4000
            original[3] |= termios.ICANON
            original[3] &= ~termios.ECHO
            original[6][termios.VERASE] = b"\x7f"
            termios.tcsetattr(slave, termios.TCSANOW, original)

            def read_fixture(prompt):
                self.assertTrue(termios.tcgetattr(slave)[0] & 0x4000)
                os.write(master, "가상".encode() + b"\x7f" + "사용자\n".encode())
                return os.read(slave, 1024).decode("utf-8").rstrip("\n")

            with (
                patch.object(self.cli.os, "open", return_value=os.dup(slave)),
                patch.object(self.cli.getpass, "getpass", side_effect=read_fixture),
            ):
                self.assertEqual(self.cli.read_hidden("가상 입력: "), "가사용자")
            self.assertEqual(termios.tcgetattr(slave), original)
        finally:
            os.close(master)
            os.close(slave)


if __name__ == "__main__":
    unittest.main()
