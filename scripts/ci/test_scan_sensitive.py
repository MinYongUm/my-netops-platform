#!/usr/bin/env python3
"""scan_sensitive.py 단위 테스트. 실행: python3 -m unittest discover -s scripts/ci -p 'test_*.py'

탐지 대상 문자열은 실행 중에 이어 붙여 만든다. 이 파일 자체가 검사에 걸리지 않게 하기 위해서다.
"""

from __future__ import annotations

import unittest

import scan_sensitive as s


def kinds(text: str) -> list[str]:
    return [k for _, k in s.scan_text(text)]


class ScanTextTest(unittest.TestCase):
    def test_detects_token_patterns(self) -> None:
        cases = {
            "private-key": "-----BEGIN " + "OPENSSH PRIVATE KEY-----",
            "aws-access-key": "AKIA" + "Q" * 16,
            "github-token": "ghp" + "_" + "a" * 36,
            "github-pat": "github" + "_pat_" + "b" * 40,
            "api-key-sk": "key: s" + "k-" + "c" * 24,
            "slack-token": "xo" + "xb-" + "1234567890",
            "hex64-token": "token=" + "f" * 64,
        }
        for kind, text in cases.items():
            with self.subTest(kind=kind):
                self.assertIn(kind, kinds(text))

    def test_reports_line_number(self) -> None:
        text = "ok\n" + "AKIA" + "Z" * 16 + "\n"
        self.assertEqual(s.scan_text(text), [(2, "aws-access-key")])

    def test_public_ipv4_flagged(self) -> None:
        self.assertEqual(kinds("dns " + ".".join(["8", "8", "8", "8"])), ["public-ipv4"])

    def test_allowed_ipv4_not_flagged(self) -> None:
        for addr in ("10.0.0.1", "172.16.5.4", "192.168.1.1", "127.0.0.1",
                     "192.0.2.10", "198.51.100.7", "203.0.113.9", "0.0.0.0",
                     "255.255.255.0", "255.255.240.0"):
            with self.subTest(addr=addr):
                self.assertEqual(kinds(f"ip: {addr}"), [])

    def test_non_ip_numbers_not_flagged(self) -> None:
        for text in ("version 7.0.31", "999.1.1.1", "1.2.3.4.5", "6.0.1-2build2"):
            with self.subTest(text=text):
                self.assertEqual(kinds(text), [])

    def test_git_sha_not_flagged(self) -> None:
        self.assertEqual(kinds("uses: x@" + "a" * 40), [])

    def test_clean_text(self) -> None:
        self.assertEqual(kinds("task-runner disk-usage ask-me"), [])


class EnvExampleTest(unittest.TestCase):
    def test_placeholder_ok(self) -> None:
        text = "POSTGRES_PASSWORD=change_me\nZBX_WEB_PORT=8081\n# KEY=comment\n"
        self.assertEqual(s.check_env_example(text), [])

    def test_real_value_flagged(self) -> None:
        text = "POSTGRES_USER=zabbix\nGRAFANA_ADMIN_PASSWORD=" + "x" * 8 + "\n"
        self.assertEqual(
            s.check_env_example(text),
            [(2, "env-example-non-placeholder:GRAFANA_ADMIN_PASSWORD")],
        )


if __name__ == "__main__":
    unittest.main()
