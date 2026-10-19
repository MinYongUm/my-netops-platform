#!/usr/bin/env python3
"""bootstrap.py — Zabbix 초기 설정과 Grafana 데이터소스 등록 (Lesson 0-2, 4~6단계)."""

from __future__ import annotations

import argparse
import json
import logging
import secrets
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

TIMEOUT = 10  # 모든 API 요청의 응답 대기 한도(초)
ADMIN_USER = "Admin"
ADMIN_USERID = "1"  # Zabbix 설치 시 고정
DEFAULT_ADMIN_PASSWORD = "zabbix"  # down -v 직후의 기본값

GRAFANA_NAME = "grafana"  # Zabbix 쪽 사용자 그룹·사용자·토큰 이름
READ_HOSTGROUP = "Zabbix servers"  # grafana 가 읽을 수 있는 호스트 그룹
GRAFANA_ROLE = "User role"
PERM_READ = 2  # 0 = 거부, 2 = 읽기 전용, 3 = 읽기·쓰기

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


def get_id(zbx: ZabbixClient, method: str, id_key: str, name: str) -> str:
    """이름이 정확히 일치하는 객체의 ID. 없으면 계속할 수 없으므로 ApiError."""
    found = zbx.call(method, {"output": [id_key], "filter": {"name": [name]}})
    if not found:
        raise ApiError(f"{method}: '{name}' 을(를) 찾을 수 없습니다")
    return found[0][id_key]


def ensure_usergroup(zbx: ZabbixClient, hostgroup_id: str) -> str:
    found = zbx.call(
        "usergroup.get",
        {"output": ["usrgrpid"], "filter": {"name": [GRAFANA_NAME]}},
    )
    if found:
        log.info("사용자 그룹 %s 이미 있음", GRAFANA_NAME)
        return found[0]["usrgrpid"]
    created = zbx.call(
        "usergroup.create",
        {
            "name": GRAFANA_NAME,
            "hostgroup_rights": [{"id": hostgroup_id, "permission": PERM_READ}],
        },
    )
    log.info("사용자 그룹 %s 생성", GRAFANA_NAME)
    return created["usrgrpids"][0]


def ensure_user(zbx: ZabbixClient, usrgrpid: str, roleid: str) -> str:
    found = zbx.call(
        "user.get",
        {"output": ["userid"], "filter": {"username": [GRAFANA_NAME]}},
    )
    if found:
        log.info("사용자 %s 이미 있음", GRAFANA_NAME)
        return found[0]["userid"]
    # Grafana 는 이 비밀번호를 쓰지 않는다 (API 토큰으로 접속). 만들고 버린다
    created = zbx.call(
        "user.create",
        {
            "username": GRAFANA_NAME,
            "passwd": secrets.token_hex(16),
            "roleid": roleid,
            "usrgrps": [{"usrgrpid": usrgrpid}],
        },
    )
    log.info("사용자 %s 생성", GRAFANA_NAME)
    return created["userids"][0]


def ensure_token(zbx: ZabbixClient, userid: str) -> str:
    """토큰 틀만 만든다. 문자열 발급(token.generate)은 Grafana 등록 단계에서."""
    found = zbx.call(
        "token.get",
        {"output": ["tokenid"], "userids": [userid], "filter": {"name": [GRAFANA_NAME]}},
    )
    if found:
        log.info("토큰 %s 이미 있음", GRAFANA_NAME)
        return found[0]["tokenid"]
    created = zbx.call("token.create", {"name": GRAFANA_NAME, "userid": userid})
    log.info("토큰 %s 생성", GRAFANA_NAME)
    return created["tokenids"][0]


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
        login_admin(zbx, env)

        hostgroup_id = get_id(zbx, "hostgroup.get", "groupid", READ_HOSTGROUP)
        role_id = get_id(zbx, "role.get", "roleid", GRAFANA_ROLE)
        usrgrpid = ensure_usergroup(zbx, hostgroup_id)
        userid = ensure_user(zbx, usrgrpid, role_id)
        tokenid = ensure_token(zbx, userid)
        log.info("Zabbix 설정 완료 (usrgrpid=%s, userid=%s, tokenid=%s)",
                 usrgrpid, userid, tokenid)

        zbx.logout()
    except ApiError as e:
        log.error("Zabbix API 오류: %s", e)
        return 1
    except (urllib.error.URLError, TimeoutError) as e:
        log.error("Zabbix 에 연결할 수 없습니다: %s", e)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
