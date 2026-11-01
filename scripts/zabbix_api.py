"""zabbix_api.py — Zabbix JSON-RPC 공통 코드 (bootstrap.py, inventory.py 가 함께 쓴다)."""

from __future__ import annotations

import json
import logging
import time
import urllib.request
from pathlib import Path
from typing import Any

TIMEOUT = 10  # 모든 API 요청의 응답 대기 한도(초)
WAIT_TIMEOUT = 120  # 서비스가 실제로 준비될 때까지 기다리는 최대 시간(초)
WAIT_INTERVAL = 3  # 재시도 간격(초)

ADMIN_USER = "Admin"
ADMIN_USERID = "1"  # Zabbix 설치 시 고정
DEFAULT_ADMIN_PASSWORD = "zabbix"  # down -v 직후의 기본값

log = logging.getLogger(__name__)


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

    def login(self, username: str, password: str) -> None:
        # user.login 은 인증 헤더 없이 보내야 하므로 기존 세션을 먼저 비운다
        self.session = None
        self.session = self.call(
            "user.login", {"username": username, "password": password}
        )

    def logout(self) -> None:
        if self.session:
            self.call("user.logout", [])
            self.session = None


def wait_for_zabbix(zbx: ZabbixClient) -> str:
    """zabbix-server 가 DB 스키마를 다 만들 때까지 apiinfo.version 을 재시도한다.

    compose --wait 는 zabbix-web 이 응답하는 것까지만 보장한다.
    스키마가 없으면 'Unable to select configuration' 으로 실패한다.
    """
    deadline = time.monotonic() + WAIT_TIMEOUT
    while True:
        try:
            return zbx.call("apiinfo.version", {})
        except (ApiError, OSError) as e:
            if time.monotonic() >= deadline:
                raise
            log.info("Zabbix 준비 대기 중 (%s)", e)
            time.sleep(WAIT_INTERVAL)


def login_admin(zbx: ZabbixClient, env: dict[str, str]) -> None:
    """.env 비밀번호로 로그인. 안 되면 기본 비밀번호로 로그인해 .env 값으로 바꾼다."""
    new_password = env["ZBX_ADMIN_PASSWORD"]
    try:
        zbx.login(ADMIN_USER, new_password)
        log.info("Admin 로그인 (.env 비밀번호)")
        return
    except ApiError:
        log.info("Admin .env 비밀번호 로그인 실패. 기본 비밀번호로 시도합니다.")

    # 여기서도 실패하면 ApiError 가 main 까지 올라가 중단된다
    zbx.login(ADMIN_USER, DEFAULT_ADMIN_PASSWORD)
    zbx.call(
        "user.update",
        {
            "userid": ADMIN_USERID,
            "current_passwd": DEFAULT_ADMIN_PASSWORD,
            "passwd": new_password,
        },
    )
    log.info("Admin 기본 비밀번호를 .env 값으로 변경")
    # 비밀번호 변경 후 기존 세션이 끊길 수 있으므로 새로 로그인한다
    zbx.login(ADMIN_USER, new_password)


def ensure_hostgroup(zbx: ZabbixClient, name: str) -> str:
    found = zbx.call(
        "hostgroup.get",
        {"output": ["groupid"], "filter": {"name": [name]}},
    )
    if found:
        log.info("호스트 그룹 %s 이미 있음", name)
        return found[0]["groupid"]
    created = zbx.call("hostgroup.create", {"name": name})
    log.info("호스트 그룹 %s 생성", name)
    return created["groupids"][0]
