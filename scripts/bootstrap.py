#!/usr/bin/env python3
"""bootstrap.py — Zabbix 초기 설정과 Grafana 데이터소스 등록 (Lesson 0-2, 4~6단계)."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

TIMEOUT = 10  # 모든 API 요청의 응답 대기 한도(초)

log = logging.getLogger("bootstrap")


class ApiError(Exception):
    """API 가 요청을 거부했을 때 (권한 없음, 중복 등)."""


def read_env(path: Path) -> dict[str, str]:
    """KEY=VALUE 줄만 읽는다. 빈 줄과 # 주석은 무시."""
    env: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip()
    return env


class ZabbixClient:
    """Zabbix JSON-RPC 호출. 로그인 후에는 세션을 Bearer 헤더로 붙인다."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.session: str | None = None
        self._id = 0

    def call(self, method: str, params: Any) -> Any:
        self._id += 1
        body = {"jsonrpc": "2.0", "method": method, "params": params, "id": self._id}
        req = urllib.request.Request(
            self.url, data=json.dumps(body).encode(), method="POST"
        )
        req.add_header("Content-Type", "application/json-rpc")
        if self.session:
            req.add_header("Authorization", f"Bearer {self.session}")
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.load(resp)
        # Zabbix 는 실패해도 HTTP 200. body 의 error 키로만 판단한다
        if "error" in data:
            raise ApiError(f"{method}: {data['error'].get('data')}")
        return data["result"]


def main() -> int:
    parser = argparse.ArgumentParser(description="Zabbix/Grafana 초기 설정")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    args = parser.parse_args()

    # install.sh 와 같은 [INFO] / [ERROR] 형식
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")

    env = read_env(args.env_file)
    zbx = ZabbixClient(f"http://localhost:{env['ZBX_WEB_PORT']}/api_jsonrpc.php")

    try:
        version = zbx.call("apiinfo.version", {})
        log.info("Zabbix API 응답: %s", version)
    except ApiError as e:
        log.error("Zabbix API 오류: %s", e)
        return 1
    except (urllib.error.URLError, TimeoutError) as e:
        log.error("Zabbix 에 연결할 수 없습니다: %s", e)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
