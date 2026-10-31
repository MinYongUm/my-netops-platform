# 아키텍처와 진행 상태

기준: `main` 커밋 `7bac73d` (2026-10-31). 작업 규칙은 [CLAUDE.md](../CLAUDE.md).

## 표기

| 표기 | 의미 |
|---|---|
| **[구현]** | `main`의 코드에서 확인함 |
| **[기록]** | 과거 테스트 기록 (`docs/lessons/` 등). 이 문서를 쓰면서 다시 실행하지 않음 |
| **[결정]** | 설계는 확정, 코드는 아직 없음 |
| **[예정]** | 다음 단계에서 상세화. 세부 사항 미확정 |
| **[미결]** | 아직 정하지 않았거나 검증하지 않은 위험 |

## 로드맵

| 단계 | 상태 | 비고 |
|---|---|---|
| v0.1 monitoring | 완료 | 설치 패키지. `v0.1` annotated tag → `479e4ff` |
| v0.2 inventory | 진행 중 | 설계 확정, 구현 전 |
| v0.3 snmp | 계획 | v0.2 검증 이후 |
| health-check → config-backup → troubleshooting → reporting | 계획 | 이 순서. 모두 미구현 |
| NEP / SoT / AI·변경 자동화 | 장기 | 미구현 |

단계 순서와 경계는 바꾸지 않는다. Web UI 커스터마이징이나 자체 UI 제작은 확정된 개발 작업이 아니다.

---

## v0.1 Monitoring — 완료

v0.1에서 완료한 것은 **설치 패키지**다. Catalyst 자동 등록, SNMP 템플릿·대시보드, Inventory 자동 반영은 v0.1 범위가 아니며 구현되지 않았다.

### 구성 요소 [구현]

| 서비스 | 이미지 | 호스트 포트 | 데이터 |
|---|---|---|---|
| `db` | `postgres:16-alpine` | 없음 (내부 5432) | volume `pg-data` |
| `zabbix-server` | `zabbix/zabbix-server-pgsql:alpine-7.0.31` | 없음 (내부 10051) | DB에 저장 |
| `zabbix-web` | `zabbix/zabbix-web-nginx-pgsql:alpine-7.0.31` | `${ZBX_WEB_PORT}` (기본 8081) → 8080 | 없음 |
| `grafana` | `grafana/grafana:13.2.4` + `alexanderzobnin-zabbix-app@6.9.1` | `${GRAFANA_PORT}` (기본 3000) → 3000 | volume `grafana-data` |

- compose 프로젝트 이름 `netops` (`compose.yaml` 최상위 `name:`)
- 기동 순서: `db` healthy → `zabbix-server` started → `zabbix-web` healthy → `grafana`
- `postgres:16-alpine`은 메이저만 고정. 마이너 버전은 이미지를 받을 때마다 달라질 수 있다
- 4개 서비스 모두 `restart: unless-stopped` (커밋 `f56285a`, v0.1 태그 이후 추가)

### 설치 흐름 [구현]

```
./install.sh
  1. docker, docker compose v2, python3, openssl 확인
  2. .env 없으면 .env.example 복사 + 비밀번호 3개 난수 생성, 권한 600 (있으면 그대로 둠)
  3. change_me 잔존, .env.example 키 누락 검사
  4. 포트 점검 (netops 스택이 이미 떠 있으면 건너뜀)
  5. docker compose up -d --wait
  6. python3 scripts/bootstrap.py --env-file .env
```

`bootstrap.py` (Python 표준 라이브러리만 사용):

1. Zabbix API(`apiinfo.version`)가 응답할 때까지 대기 (3초 간격, 최대 120초)
2. `Admin` 로그인. `.env` 비밀번호가 안 되면 기본값 `zabbix`로 로그인해 `.env` 값으로 변경
3. 호스트 그룹 `Sites` 생성 (없을 때만)
4. 사용자 그룹 `grafana` 생성 또는 권한 갱신, 사용자 `grafana`(User role), API 토큰 틀 생성
5. Grafana `/api/health` 대기 → Zabbix 앱 활성화 → 데이터소스 `Zabbix` 생성/갱신 → health 확인
6. `Admin` 로그아웃

모든 단계는 "조회 → 없으면 생성"이라 재실행해도 결과가 같다.

### 연결과 경계 [구현]

```
          호스트 포트                         compose 내부 네트워크
사용자 ──▶ :8081 ─▶ zabbix-web:8080 ──▶ db:5432 ◀── zabbix-server
사용자 ──▶ :3000 ─▶ grafana:3000 ──(API 토큰)──▶ http://zabbix-web:8080/api_jsonrpc.php
install.sh/bootstrap.py ─▶ localhost:8081 (Admin), localhost:3000 (admin, Basic 인증)
```

- Grafana → Zabbix: 전용 Zabbix 사용자 `grafana`의 API 토큰. `Admin`으로 연결하지 않는다
- 사용자 그룹 `grafana`의 호스트 그룹 권한: `Zabbix servers`, `Sites` 둘 다 읽기(permission 2). 코드(`ensure_usergroup`)에서 설정. `Sites`는 커밋 `d572ca2`(v0.1 태그 이후)에서 추가
- `hostgroup_rights`는 전체 교체 API라서 기존 권한과 합쳐서 보낸다 (`Sites/*` 하위 그룹 권한을 지우지 않기 위해)
- 하위 그룹 `Sites/<code>`가 `Sites`의 권한을 물려받는다는 동작은 코드 주석의 전제다. 지식파일에 과거 실험으로 기록돼 있지만 저장소에는 테스트 기록이 없다. **v0.2 구현 때 다시 확인해야 한다** [미결]
- API 토큰은 디스크·`.env`·compose에 쓰지 않는다. 발급 즉시 Grafana `secureJsonData`로 넘긴다. 데이터소스가 있고 health OK면 재발급하지 않는다

### 비밀과 데이터 [구현]

- 비밀번호 3개(`POSTGRES_PASSWORD`, `ZBX_ADMIN_PASSWORD`, `GRAFANA_ADMIN_PASSWORD`)는 `.env`에만. `.env`는 권한 600, `.gitignore`로 제외
- `compose.yaml`은 `${VAR:?missing in .env}` 형식이라 `.env` 없이 기동하지 않는다
- 데이터는 named volume 2개(`pg-data`, `grafana-data`). `docker compose down`은 유지, `down -v`는 삭제

### 테스트 이력 [기록]

| 시기 | 내용 | 출처 |
|---|---|---|
| 2026-10-18 ~ 19 | compose 수동 구성, `down -v` 후 재기동, Grafana Explore 그래프 확인 | `docs/lessons/0-1.md` |
| 2026-10-19 | `./install.sh` 12개 시나리오 (첫 설치 약 21초, 재실행 무변경, 오류 입력 시 exit 1, grafana 토큰 권한 확인 등) 통과 | `docs/lessons/0-2.md` |
| 2026-10-19 | `v0.1` 태그 (`479e4ff`) | git tag |
| 2026-10-26 | `restart: unless-stopped`, `Sites` 그룹·권한 추가 | 커밋 `f56285a`, `d572ca2`. 저장소에 테스트 기록 없음 |

- 검증 환경은 한 곳이다: Proxmox VM, Ubuntu Server 24.04.5, Docker 29.9.0, Compose v5.6.0, python3 3.12.3, 인터넷 연결 있음.
- 이 표는 과거 기록이다. 다른 OS·폐쇄망·다른 Docker 버전에서 설치가 성공한다는 뜻이 아니다.
- v0.1 태그 이후 커밋(`restart` 정책, `Sites` 권한)은 Lesson 0-2의 검증 범위에 포함되지 않는다. 재부팅 후 자동 기동과 `Sites` 권한은 따로 확인해야 한다 [미결].

---

## v0.2 Inventory — 진행 중

사이트 프로필(YAML)에 적은 장비를 Zabbix에 등록·갱신·비활성한다. 설계는 확정됐고 **코드는 아직 없다**.

### 현재 상태

| 항목 | 상태 |
|---|---|
| 사이트 프로필 예시 `sites/SITE-EXAMPLE.yaml` | [구현] 파일만 있음. 읽는 코드 없음 |
| `.gitignore`: `sites/` 아래는 예시만 추적 | [구현] |
| 상위 호스트 그룹 `Sites` + grafana 읽기 권한 | [구현] `bootstrap.py` |
| ICMP 수동 실험 | 완료 (지식파일 기록). 저장소에는 기록 없음 |
| YAML 스키마, Inventory 동작 정책 | [결정] |
| `scripts/zabbix_api.py` (Zabbix API 공용 로직 분리) | 미구현 |
| `scripts/inventory.py` | 미구현 |
| 프로필 → Zabbix 자동 반영 | 미구현 |

수동 실험으로 확인한 것과 구현 완료는 다르다. `inventory.py`가 생기고 테스트를 통과하기 전까지 v0.2는 완료가 아니다.

### 사이트 프로필 스키마 [결정]

```yaml
company: example
sites:
  - code: SEL
    devices:
      - name: SEL-CORE-01
        ip: 10.0.0.1
```

| 필드 | 제약 |
|---|---|
| `company` | 회사 식별자 |
| `sites[].code` | 거점 코드. 파일 안에서 유일, `/` 사용 불가. Zabbix 호스트 그룹 `Sites/<code>`가 된다 |
| `sites[].devices[].name` | 장비 이름. 파일 전체에서 유일, 영문·숫자·`.`·`-`·`_`만. Zabbix 호스트 이름이 된다 |
| `sites[].devices[].ip` | IPv4. 파일 전체에서 유일 |

- 실제 파일은 `sites/<회사>.yaml`. 공개 저장소에는 예시만 둔다
- 값은 문자열로 다룬다. `NO`, `ON`, `OFF`, `YES`나 숫자로 시작하는 값은 따옴표로 감싼다
- 읽기: PyYAML `safe_load`
- 검증(계획): 중복 키, 정의되지 않은 필드, 잘못된 타입은 오류로 거부

### 실행 방식 [결정]

- `install.sh`는 설치 전용으로 둔다. Inventory는 별도 명령으로 실행한다: `python3 scripts/inventory.py ...` (인자 형식은 구현 때 확정)
- `--dry-run`: 바뀔 내용만 보여주고 Zabbix에는 아무것도 바꾸지 않는다
- Zabbix 인스턴스 1개에 프로필 1개만 적용한다
- 관리 범위는 `Sites/*` 호스트 그룹뿐이다. 그 밖에 있는 같은 이름의 호스트를 가져다 쓰거나 바꾸지 않는다
- Zabbix API 공용 로직(`bootstrap.py`의 `ZabbixClient` 등)을 `scripts/zabbix_api.py`로 먼저 분리하고, 그다음 `inventory.py`를 만든다. Lesson 0-2에서 Python 표준 라이브러리로 API 클라이언트를 만든 이유가 이 재사용이다

### 동작 정책 [결정]

| 프로필 변화 | Zabbix 동작 |
|---|---|
| 장비 추가 | ICMP Ping 템플릿으로 호스트 생성, `Sites/<code>` 그룹에 넣음 |
| IP 변경 | 호스트를 새로 만들지 않고 interface update. 이력 유지 |
| 거점 이동 (`code` 변경) | 호스트 그룹을 `Sites/<새 code>`로 변경 |
| 프로필에서 제거 | **삭제하지 않고 비활성** (감시 중지, 이력 유지) |
| 제거했던 장비 재추가 | 기존 호스트를 다시 활성 |
| 변경 없음 | 아무것도 바꾸지 않음 (멱등) |

---

## v0.3 SNMP — 다음 단계 [예정]

v0.2 Inventory 검증이 끝난 뒤 시작한다. 세부 사항은 별도 Issue에서 정한다.

- SNMPv2c read-only를 기본으로, SNMPv3는 선택 사항으로 검토
- Cisco Catalyst 공통 템플릿, 수집 메트릭, Grafana 대시보드
- SNMP community·v3 키 등 비밀 값은 사이트 프로필(YAML)이나 공개 저장소에 두지 않는다. 보관 방식은 미정

v0.1의 완료 범위를 SNMP 쪽으로 소급해 넓히지 않는다.

## 이후 모듈 [예정]

health-check → config-backup → troubleshooting → reporting 순서로 진행하고, NEP·SoT·AI·변경 자동화는 장기 단계로 둔다. 전부 미구현이다. 저장소에 모듈별 디렉터리(초기 README의 `modules/`)도 아직 없다.

---

## 미결 위험 [미결]

이 문서는 기록만 한다. 아래 항목의 운영 변경은 별도 Issue와 사용자 승인으로 진행한다.

| 항목 | 현재 상태 |
|---|---|
| 외부 노출·접근 통제 | `compose.yaml` 포트에 바인딩 주소가 없어 호스트의 모든 인터페이스에 8081, 3000이 열린다. 방화벽·접근 제한은 저장소 범위 밖에서 처리해야 함 |
| TLS | 없음. Zabbix Web, Grafana 모두 HTTP |
| 외부로 나가는 호출 | Zabbix 업데이트 확인, Grafana 업데이트 확인·사용 통계가 기본값 그대로 (`docs/SECURITY.md`) |
| 폐쇄망 | 이미지, Grafana 플러그인 반입 방법 미검증 (`docs/INSTALL.md`) |
| 백업·복원, 보존 정책 | 없음. volume 백업 절차, Zabbix history 보존 기간 미정 |
| 감시 시스템 자체 가용성 | 단일 호스트, 단일 컨테이너. 재부팅 후 자동 기동(`restart: unless-stopped`) 검증 기록 없음. 감시 서버가 죽었을 때 알릴 수단 없음 |
| Inventory 계정 권한 | `inventory.py`는 `Admin` 계정을 재사용하는 설계(미구현). 최소 권한 전용 계정은 미검토 |
| `Sites/*` 권한 상속 | 저장소에 테스트 기록 없음. v0.2에서 재확인 |
| API 토큰 만료 | grafana 토큰은 만료 없이 생성됨 (`token.create`에 만료 미지정) |
| v0.2 의존성 | PyYAML은 표준 라이브러리가 아니다. 현재 `docs/INSTALL.md`는 "pip 불필요"라고 쓴다. 설치 방법(배포판 패키지 등)과 요구사항 문서 변경은 v0.2 구현 때 정한다 |
