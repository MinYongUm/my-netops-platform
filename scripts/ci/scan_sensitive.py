#!/usr/bin/env python3
"""scan_sensitive.py — 추적 중인 파일에서 민감정보 패턴을 찾는다 (Issue #5 Phase 1 CI).

- 대상: `git ls-files` 로 추적 중인 텍스트 파일
- 찾은 값 자체는 출력하지 않는다 (공개 저장소의 CI 로그에 남지 않도록). 파일·줄·종류만 출력
- 찾으면 exit 1. 표준 라이브러리만 사용
"""

from __future__ import annotations

import ipaddress
import re
import subprocess
import sys
from pathlib import Path

# (종류, 정규식). 정규식 원문이 자기 자신에 걸리지 않도록 문자 클래스로 시작/구분한다
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private-key", re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----")),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("github-pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}\b")),
    ("api-key-sk", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),  # OpenAI·Anthropic 등 sk- 형식
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("hex64-token", re.compile(r"\b[0-9a-f]{64}\b")),  # Zabbix API 토큰 형식
]

IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")

# 공개 저장소에 있어도 되는 IPv4 범위: 사설, 루프백, 문서용(RFC 5737), 미지정
ALLOWED_NETS = [
    ipaddress.ip_network(n)
    for n in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "127.0.0.0/8",
        "192.0.2.0/24",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "0.0.0.0/32",
    )
]

# .env.example 에서 값이 change_me 여야 하는 키
SECRET_KEY = re.compile(r"^([A-Z0-9_]*(?:PASSWORD|SECRET|TOKEN|KEY)[A-Z0-9_]*)=(.*)$")
PLACEHOLDER = "change_me"


def is_netmask(addr: ipaddress.IPv4Address) -> bool:
    """255.255.255.0 처럼 1이 연속된 서브넷 마스크 모양인지."""
    bits = int(addr)
    inverted = ~bits & 0xFFFFFFFF
    return bits != 0 and (inverted & (inverted + 1)) == 0


def ip_findings(line: str) -> list[str]:
    found = []
    for m in IPV4.finditer(line):
        try:
            addr = ipaddress.IPv4Address(m.group(0))
        except ipaddress.AddressValueError:
            continue  # 999.1.1.1 처럼 IP가 아닌 숫자열
        if any(addr in net for net in ALLOWED_NETS) or is_netmask(addr):
            continue
        found.append("public-ipv4")
    return found


def scan_text(text: str) -> list[tuple[int, str]]:
    """(줄 번호, 종류) 목록."""
    results = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for kind, pattern in PATTERNS:
            if pattern.search(line):
                results.append((lineno, kind))
        for kind in ip_findings(line):
            results.append((lineno, kind))
    return results


def check_env_example(text: str) -> list[tuple[int, str]]:
    """비밀 키의 값이 change_me 가 아니면 보고한다."""
    results = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        m = SECRET_KEY.match(line.strip())
        if m and m.group(2).strip() != PLACEHOLDER:
            results.append((lineno, f"env-example-non-placeholder:{m.group(1)}"))
    return results


def tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], check=True, capture_output=True
    ).stdout
    return [p for p in out.decode().split("\0") if p]


def main() -> int:
    findings: list[tuple[str, int, str]] = []
    for path in tracked_files():
        data = Path(path).read_bytes()
        if b"\0" in data:
            continue  # 바이너리
        text = data.decode("utf-8", errors="replace")
        findings += [(path, n, k) for n, k in scan_text(text)]
        if path == ".env.example":
            findings += [(path, n, k) for n, k in check_env_example(text)]

    for path, lineno, kind in findings:
        # GitHub Actions 주석 형식. 값은 출력하지 않는다
        print(f"::error file={path},line={lineno}::sensitive pattern: {kind}")
        print(f"[FAIL] {path}:{lineno}: {kind}", file=sys.stderr)
    if findings:
        print(f"[FAIL] 민감정보 패턴 {len(findings)}건", file=sys.stderr)
        return 1
    print("[OK] 민감정보 패턴 없음")
    return 0


if __name__ == "__main__":
    sys.exit(main())
