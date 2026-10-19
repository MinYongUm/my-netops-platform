#!/usr/bin/env python3
"""bootstrap.py — Zabbix 초기 설정과 Grafana 데이터소스 등록 (Lesson 0-2, 4~6단계)."""

from __future__ import annotations

import argparse
import base64
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

ZBX_APP_ID = "alexanderzobnin-zabbix-app"
DS_NAME = "Zabbix"
# Grafana 컨테이너에서 보는 주소. 호스트 포트(8081)가 아니라 compose 서비스 이름과 내부 포트
DS_URL = "http://zabbix-web:8080/api_jsonrpc.php"

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


class GrafanaClient:
    """Grafana HTTP API 호출. 매 요청 Basic 인증(admin)."""

    def __init__(self, url: str, user: str, password: str) -> None:
        self.url = url
        cred = base64.b64encode(f"{user}:{password}".encode()).decode()
        self.auth = f"Basic {cred}"

    def request(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        """상태 코드와 JSON 을 반환한다. 404·409 같은 판단은 호출한 쪽에서."""
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method)
        req.add_header("Authorization", self.auth)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return resp.status, json.load(resp)
        except urllib.error.HTTPError as e:
            # urllib 은 4xx/5xx 를 예외로 던진다. 여기서 잡지 않으면
            # main 에서 연결 실패(URLError)로 잘못 처리된다
            try:
                payload = json.load(e)
            except json.JSONDecodeError:
                payload = {}
            return e.code, payload


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


def generate_token(zbx: ZabbixClient, tokenid: str) -> str:
    """토큰 문자열 발급. 부를 때마다 새 문자열이 나오고 이전 것은 무효가 된다."""
    result = zbx.call("token.generate", [tokenid])
    log.info("토큰 %s 문자열 발급", GRAFANA_NAME)
    return result[0]["token"]


def enable_plugin(gf: GrafanaClient) -> None:
    status, data = gf.request("GET", f"/api/plugins/{ZBX_APP_ID}/settings")
    if status != 200:
        raise ApiError(f"플러그인 조회 실패: HTTP {status} {data.get('message', '')}")
    if data.get("enabled"):
        log.info("Grafana Zabbix 앱 이미 활성화됨")
        return
    status, data = gf.request(
        "POST",
        f"/api/plugins/{ZBX_APP_ID}/settings",
        {"enabled": True, "pinned": True, "jsonData": {}},
    )
    if status != 200:
        raise ApiError(f"플러그인 활성화 실패: HTTP {status} {data.get('message', '')}")
    log.info("Grafana Zabbix 앱 활성화")


def datasource_body(token: str) -> dict[str, Any]:
    return {
        "name": DS_NAME,
        "type": "alexanderzobnin-zabbix-datasource",
        "access": "proxy",
        "url": DS_URL,
        "jsonData": {"authType": "token"},
        "secureJsonData": {"apiToken": token},
    }


def datasource_health(gf: GrafanaClient, uid: str) -> tuple[bool, str]:
    status, data = gf.request("GET", f"/api/datasources/uid/{uid}/health")
    ok = status == 200 and data.get("status") == "OK"
    return ok, data.get("message", "")


def ensure_datasource(gf: GrafanaClient, zbx: ZabbixClient, tokenid: str) -> None:
    status, data = gf.request("GET", f"/api/datasources/name/{DS_NAME}")

    if status == 200:
        uid = data["uid"]
        ok, message = datasource_health(gf, uid)
        if ok:
            log.info("데이터소스 %s 이미 있음, health OK (%s)", DS_NAME, message)
            return
        log.info("데이터소스 %s health 실패 (%s). 토큰을 재발급해 갱신합니다.",
                 DS_NAME, message)
        token = generate_token(zbx, tokenid)
        status, data = gf.request(
            "PUT", f"/api/datasources/uid/{uid}", datasource_body(token)
        )
        if status != 200:
            raise ApiError(f"데이터소스 갱신 실패: HTTP {status} {data.get('message', '')}")
        log.info("데이터소스 %s 갱신", DS_NAME)

    elif status == 404:
        token = generate_token(zbx, tokenid)
        status, data = gf.request("POST", "/api/datasources", datasource_body(token))
        if status != 200:
            raise ApiError(f"데이터소스 생성 실패: HTTP {status} {data.get('message', '')}")
        uid = data["datasource"]["uid"]
        log.info("데이터소스 %s 생성", DS_NAME)

    else:
        raise ApiError(f"데이터소스 조회 실패: HTTP {status} {data.get('message', '')}")

    # 6단계 검증: 만들었거나 고쳤으면 실제로 붙는지 한 번 더 확인
    ok, message = datasource_health(gf, uid)
    if not ok:
        raise ApiError(f"데이터소스 {DS_NAME} health 실패: {message}")
    log.info("데이터소스 %s health OK (%s)", DS_NAME, message)


def main() -> int:
    parser = argparse.ArgumentParser(description="Zabbix/Grafana 초기 설정")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    args = parser.parse_args()

    # install.sh 와 같은 [INFO] / [ERROR] 형식
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")

    env = read_env(args.env_file)
    zbx = ZabbixClient(f"http://localhost:{env['ZBX_WEB_PORT']}/api_jsonrpc.php")
    gf = GrafanaClient(
        f"http://localhost:{env['GRAFANA_PORT']}",
        "admin",
        env["GRAFANA_ADMIN_PASSWORD"],
    )

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

        # token.generate 에 Admin 세션이 필요하므로 logout 전에 진행
        enable_plugin(gf)
        ensure_datasource(gf, zbx, tokenid)

        zbx.logout()
    except ApiError as e:
        log.error("API 오류: %s", e)
        return 1
    except (urllib.error.URLError, TimeoutError) as e:
        log.error("연결할 수 없습니다: %s", e)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
