#!/usr/bin/env python3
"""inventory.py — 사이트 프로필(sites/<회사>.yaml)을 Zabbix 호스트로 맞춘다 (Lesson 1-1).

1단계: 프로필 읽기와 검사. 2단계: Zabbix 조회와 --dry-run.
쓰기 API(create, update, massadd)는 아직 쓰지 않는다.
"""

from __future__ import annotations

import argparse
import ipaddress
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from zabbix_api import ADMIN_USER, ApiError, ZabbixClient, read_env

TOP_KEYS = ("company", "sites")
SITE_KEYS = ("code", "devices")
DEVICE_KEYS = ("name", "ip")
# name 은 Zabbix 호스트 이름, code 는 그룹 Sites/<code> 가 되므로 둘 다
# 영문·숫자·. - _ 만 허용한다 ('/' 도 여기서 막힌다)
NAME_RE = re.compile(r"[A-Za-z0-9._-]+")
MERGE_TAG = "tag:yaml.org,2002:merge"
# "이 네트워크" 대역. is_unspecified 는 0.0.0.0 하나만 보므로 대역 전체를 따로 막는다
THIS_NETWORK = ipaddress.IPv4Network("0.0.0.0/8")

SITES_PREFIX = "Sites/"  # 관리 범위. 상위 그룹 Sites 자체는 제외
ICMP_TEMPLATE = "ICMP Ping"  # 템플릿 기술 이름(host)
EXIT_NOT_YET = 2  # --dry-run 없이 실행했을 때

log = logging.getLogger("inventory")


@dataclass(frozen=True)
class Device:
    name: str
    ip: str
    site: str  # 거점 code


@dataclass
class Profile:
    company: str
    sites: dict[str, list[Device]] = field(default_factory=dict)

    @property
    def devices(self) -> list[Device]:
        return [d for devices in self.sites.values() for d in devices]


class ProfileError(Exception):
    """프로필 검사 실패. 찾은 오류를 전부 담는다."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__(f"프로필 오류 {len(errors)}건")
        self.errors = errors


@dataclass
class ZabbixState:
    """조회 결과. 비교(plan)는 이 값만 보고 API 를 부르지 않는다."""

    template_id: str
    groups: dict[str, str]  # Sites/* 그룹 이름 → groupid
    hosts: list[dict[str, Any]]  # Sites/* 그룹에 속한 호스트 (host.get)
    # Sites/* 밖에서 프로필 이름과 대소문자 무시로 같은 호스트
    others: list[dict[str, Any]]


def find_anchors(text: str, errors: list[str]) -> None:
    """앵커(&)와 별칭(*)을 찾는다. 파일에 보이는 그대로 적용되도록 둘 다 금지."""
    for event in yaml.parse(text, Loader=yaml.SafeLoader):
        if not isinstance(event, yaml.NodeEvent) or not event.anchor:
            continue
        line = event.start_mark.line + 1
        if isinstance(event, yaml.AliasEvent):
            errors.append(f"{line}번째 줄: 별칭(*{event.anchor})은 쓸 수 없습니다")
        else:
            errors.append(f"{line}번째 줄: 앵커(&{event.anchor})는 쓸 수 없습니다")


def find_duplicate_keys(
    node: yaml.Node | None, path: str, errors: list[str], seen_nodes: set[int]
) -> None:
    """compose 노드 트리에서 같은 mapping 안의 중복 키와 merge key(<<)를 찾는다.

    safe_load 는 중복 키를 조용히 덮어쓰므로 객체를 만들기 전 노드 단계에서 본다.
    """
    # 별칭이 있으면 같은 노드가 여러 번(또는 자기 자신 안에) 나올 수 있다
    if node is None or id(node) in seen_nodes:
        return
    seen_nodes.add(id(node))

    if isinstance(node, yaml.MappingNode):
        keys: set[str] = set()
        for key_node, value_node in node.value:
            key = None
            if isinstance(key_node, yaml.ScalarNode):
                key = key_node.value
            line = key_node.start_mark.line + 1
            if key_node.tag == MERGE_TAG:
                # << 는 앵커 없이 {..} 로도 쓸 수 있어 따로 막는다
                errors.append(f"{line}번째 줄: merge key(<<)는 쓸 수 없습니다")
            elif key is not None:
                if key in keys:
                    errors.append(
                        f"{path or '(최상위)'}: 키 '{key}' 가 중복됩니다 "
                        f"({line}번째 줄)"
                    )
                keys.add(key)
            child = f"{path}.{key}" if path else str(key)
            find_duplicate_keys(value_node, child, errors, seen_nodes)
    elif isinstance(node, yaml.SequenceNode):
        for i, item in enumerate(node.value):
            find_duplicate_keys(item, f"{path}[{i}]", errors, seen_nodes)


def check_str(value: Any, path: str, errors: list[str]) -> str | None:
    """문자열이고, 비어 있지 않고, 앞뒤 공백이 없으면 그 값을 돌려준다."""
    if value is None:
        errors.append(f"{path}: 값이 없습니다")
        return None
    if not isinstance(value, str):
        # YAML 1.1 에서 NO/ON 은 bool, 0으로 시작하는 숫자는 8진수 int 가 된다
        kind = type(value).__name__
        errors.append(
            f"{path}: 문자열이어야 합니다 ({kind} {value!r} 로 읽힘). "
            "따옴표로 감싸세요"
        )
        return None
    if not value:
        errors.append(f"{path}: 빈 문자열입니다")
        return None
    if value != value.strip():
        errors.append(f"{path}: 앞뒤에 공백이 있습니다 ({value!r})")
        return None
    return value


def check_mapping(
    value: Any, path: str, allowed: tuple[str, ...], errors: list[str]
) -> dict[Any, Any] | None:
    """mapping 이면 그 값을 돌려준다. 모르는 키와 빠진 필수 키는 오류로 남긴다.

    오류를 한 번에 다 보여주기 위해 mapping 이 아닐 때만 None 을 돌려준다.
    호출한 쪽은 get_str 로 있는 키만 검사한다.
    """
    if not isinstance(value, dict):
        errors.append(f"{path}: 키: 값 형태(mapping)여야 합니다")
        return None
    for key in value:
        if key not in allowed:
            errors.append(
                f"{path}: 모르는 키 {key!r} (허용: {', '.join(allowed)})"
            )
    for key in allowed:
        if key not in value:
            errors.append(f"{path}: 필수 키 '{key}' 가 없습니다")
    return value


def get_str(
    mapping: dict[Any, Any], key: str, path: str, errors: list[str]
) -> str | None:
    """키가 있으면 check_str 로 검사한다. 없으면 check_mapping 이 이미 알렸다."""
    if key not in mapping:
        return None
    return check_str(mapping[key], path, errors)


def check_ident(
    value: str, path: str, seen: dict[str, tuple[str, str]], errors: list[str]
) -> str | None:
    """code/name 공통: 허용 문자만 쓰고 대소문자 구분 없이 처음 나온 값이면 돌려준다.

    seen: casefold 값 → (처음 나온 위치, 원래 값)
    """
    if not NAME_RE.fullmatch(value):
        errors.append(f"{path}: 영문·숫자·. - _ 만 쓸 수 있습니다 ({value!r})")
        return None
    folded = value.casefold()
    if folded in seen:
        first_path, first = seen[folded]
        errors.append(
            f"{path}: '{value}' 가 {first_path} 의 '{first}' 와 겹칩니다 "
            "(대소문자 구분 없이 유일해야 함)"
        )
        return None
    seen[folded] = (path, value)
    return value


def check_ip(
    value: str,
    path: str,
    seen: dict[ipaddress.IPv4Address, str],
    errors: list[str],
) -> str | None:
    """장비에 쓸 수 있는 IPv4 이고 처음 나온 주소이면 돌려준다."""
    try:
        # 3.12 의 IPv4Address 는 앞자리 0(010.0.0.1)과 /24 표기를 거부한다
        addr = ipaddress.IPv4Address(value)
    except ValueError:
        errors.append(f"{path}: IPv4 주소가 아닙니다 ({value!r})")
        return None
    # is_reserved 는 240.0.0.0/4 이고 255.255.255.255 도 여기에 들어간다
    if (
        addr in THIS_NETWORK
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
    ):
        errors.append(
            f"{path}: 장비 주소로 쓸 수 없는 특수 주소입니다 ({value!r}). "
            "0.0.0.0/8, 127.0.0.0/8, 169.254.0.0/16, 224.0.0.0/4, "
            "240.0.0.0/4 는 금지"
        )
        return None
    if addr in seen:
        errors.append(f"{path}: '{value}' 가 {seen[addr]} 와 겹칩니다")
        return None
    seen[addr] = path
    return value


def check_list(value: Any, path: str, errors: list[str]) -> list[Any] | None:
    """비어 있지 않은 목록이면 그 값을 돌려준다."""
    if not isinstance(value, list):
        errors.append(f"{path}: 목록(- 항목)이어야 합니다")
        return None
    if not value:
        errors.append(f"{path}: 비어 있습니다. 항목이 하나 이상 있어야 합니다")
        return None
    return value


def validate(data: Any, errors: list[str]) -> Profile | None:
    """safe_load 결과를 검사한다. 오류는 errors 에 모으고, 없으면 Profile."""
    top = check_mapping(data, "(최상위)", TOP_KEYS, errors)
    if top is None:
        return None

    company = get_str(top, "company", "company", errors)
    if "sites" not in top:
        return None
    sites = check_list(top["sites"], "sites", errors)
    if sites is None:
        return None

    profile_sites: dict[str, list[Device]] = {}
    codes: dict[str, tuple[str, str]] = {}
    names: dict[str, tuple[str, str]] = {}
    ips: dict[ipaddress.IPv4Address, str] = {}

    for i, site_value in enumerate(sites):
        site_path = f"sites[{i}]"
        site = check_mapping(site_value, site_path, SITE_KEYS, errors)
        if site is None:
            continue

        code_path = f"{site_path}.code"
        code = get_str(site, "code", code_path, errors)
        if code is not None:
            code = check_ident(code, code_path, codes, errors)

        if "devices" not in site:
            continue
        devices = check_list(site["devices"], f"{site_path}.devices", errors)
        if devices is None:
            continue

        site_devices: list[Device] = []
        for j, device_value in enumerate(devices):
            dev_path = f"{site_path}.devices[{j}]"
            device = check_mapping(device_value, dev_path, DEVICE_KEYS, errors)
            if device is None:
                continue

            name_path = f"{dev_path}.name"
            name = get_str(device, "name", name_path, errors)
            if name is not None:
                name = check_ident(name, name_path, names, errors)

            ip_path = f"{dev_path}.ip"
            ip = get_str(device, "ip", ip_path, errors)
            if ip is not None:
                ip = check_ip(ip, ip_path, ips, errors)

            if name is not None and ip is not None and code is not None:
                site_devices.append(Device(name=name, ip=ip, site=code))

        if code is not None:
            profile_sites[code] = site_devices

    if errors or company is None:
        return None
    return Profile(company=company, sites=profile_sites)


def load_profile(path: Path) -> Profile:
    """프로필을 읽고 전부 검사한다. 하나라도 틀리면 ProfileError."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise ProfileError([f"{path}: 읽을 수 없습니다 ({e.strerror})"]) from e
    except UnicodeDecodeError as e:
        raise ProfileError([f"{path}: UTF-8 텍스트가 아닙니다"]) from e

    errors: list[str] = []
    try:
        # 중복 키는 노드 단계(compose)에서, 값은 safe_load 로 읽는다.
        # compose 는 Python 객체를 만들지 않으므로 safe_load 와 같이 안전하다
        find_anchors(text, errors)
        root = yaml.compose(text, Loader=yaml.SafeLoader)
        find_duplicate_keys(root, "", errors, set())
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ProfileError([f"{path}: YAML 을 읽을 수 없습니다\n{e}"]) from e

    profile = validate(data, errors)
    if profile is None:
        raise ProfileError(errors)
    return profile


def fetch_state(zbx: ZabbixClient, profile: Profile) -> ZabbixState:
    """비교에 필요한 것을 읽기 API 로만 조회한다."""
    found = zbx.call(
        "template.get",
        {"output": ["templateid"], "filter": {"host": [ICMP_TEMPLATE]}},
    )
    if not found:
        raise ApiError(f"템플릿 '{ICMP_TEMPLATE}' 을(를) 찾을 수 없습니다")
    template_id = found[0]["templateid"]

    # search 는 대소문자를 무시하고 부분 일치이므로 이름으로 한 번 더 거른다
    groups = {
        g["name"]: g["groupid"]
        for g in zbx.call(
            "hostgroup.get",
            {
                "output": ["groupid", "name"],
                "search": {"name": SITES_PREFIX},
                "startSearch": True,
            },
        )
        if g["name"].startswith(SITES_PREFIX)
    }

    hosts: list[dict[str, Any]] = []
    # groupids 가 빠지면 전체 호스트가 오므로 그룹이 없으면 부르지 않는다
    if groups:
        hosts = zbx.call(
            "host.get",
            {
                "groupids": list(groups.values()),
                "output": ["hostid", "host", "status"],
                "selectInterfaces": [
                    "interfaceid", "type", "main", "useip", "ip", "port",
                ],
                "selectHostGroups": ["groupid", "name"],
                "selectParentTemplates": ["templateid", "host"],
            },
        )

    # Sites/* 에서 찾지 못한 프로필 이름만 밖에 있는지 확인한다 (결정 1)
    inside = {h["host"] for h in hosts}
    missing = [d.name for d in profile.devices if d.name not in inside]
    others: list[dict[str, Any]] = []
    if missing:
        inside_ids = {h["hostid"] for h in hosts}
        wanted = {name.casefold() for name in missing}
        candidates = zbx.call(
            "host.get",
            {
                "output": ["hostid", "host"],
                "search": {"host": missing},
                "searchByAny": True,
                "selectHostGroups": ["name"],
            },
        )
        others = [
            h
            for h in candidates
            if h["hostid"] not in inside_ids
            and h["host"].casefold() in wanted
        ]

    return ZabbixState(
        template_id=template_id, groups=groups, hosts=hosts, others=others
    )


def connect(env_file: Path) -> ZabbixClient:
    """.env 의 Admin 비밀번호로만 로그인한다.

    bootstrap 의 login_admin 은 비밀번호를 바꾸는 쓰기(user.update)가 있어 쓰지 않는다.
    """
    env = read_env(env_file)
    for key in ("ZBX_WEB_PORT", "ZBX_ADMIN_PASSWORD"):
        if not env.get(key):
            raise ApiError(f"{env_file} 에 {key} 가 없습니다")
    zbx = ZabbixClient(
        f"http://localhost:{env['ZBX_WEB_PORT']}/api_jsonrpc.php"
    )
    version = zbx.call("apiinfo.version", {})
    log.info("Zabbix API 응답: %s", version)
    try:
        zbx.login(ADMIN_USER, env["ZBX_ADMIN_PASSWORD"])
    except ApiError as e:
        raise ApiError(
            f".env 비밀번호로 Admin 로그인 실패 ({e}). ./install.sh 를 먼저 실행하세요"
        ) from e
    return zbx


def main() -> int:
    parser = argparse.ArgumentParser(
        description="사이트 프로필을 Zabbix 호스트로 맞춘다. "
        "지금은 --dry-run(변경 목록 출력)만 된다"
    )
    parser.add_argument(
        "profile",
        type=Path,
        help="사이트 프로필 YAML (예: sites/SITE-EXAMPLE.yaml)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Zabbix 를 바꾸지 않고 바뀔 내용만 출력한다",
    )
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    args = parser.parse_args()

    # install.sh, bootstrap.py 와 같은 [INFO] / [ERROR] 형식
    logging.basicConfig(
        level=logging.INFO, format="[%(levelname)s] %(message)s"
    )

    try:
        profile = load_profile(args.profile)
    except ProfileError as e:
        for message in e.errors:
            log.error("%s", message)
        log.error(
            "프로필 검사 실패 (%d건). 아무것도 바꾸지 않았습니다.",
            len(e.errors),
        )
        return 1

    log.info(
        "프로필 검사 통과: company=%s, 거점 %d, 장비 %d",
        profile.company,
        len(profile.sites),
        len(profile.devices),
    )

    if not args.dry_run:
        log.error(
            "실행 모드는 다음 단계에서 추가됩니다. --dry-run 으로 실행하세요"
        )
        return EXIT_NOT_YET

    try:
        zbx = connect(args.env_file)
        try:
            state = fetch_state(zbx, profile)
        finally:
            # logout 실패가 조회 중 난 원래 오류를 덮지 않게 경고만 남긴다
            try:
                zbx.logout()
            except (ApiError, OSError) as e:
                log.warning("로그아웃 실패: %s", e)
    except ApiError as e:
        log.error("API 오류: %s", e)
        return 1
    except OSError as e:
        # .env 읽기 실패, 연결 거부·타임아웃은 모두 OSError
        log.error("연결할 수 없습니다: %s", e)
        return 1

    log.info(
        "[dry-run] 조회: Sites/* 그룹 %d, 호스트 %d, Sites/* 밖 같은 이름 %d",
        len(state.groups),
        len(state.hosts),
        len(state.others),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
