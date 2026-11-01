# 보안

## 계정

| 계정 | 용도 | 비밀번호/토큰 보관 |
|---|---|---|
| PostgreSQL `zabbix` | Zabbix DB | `.env` `POSTGRES_PASSWORD` |
| Zabbix `Admin` | 관리, bootstrap.py | `.env` `ZBX_ADMIN_PASSWORD`. 첫 설치 때 기본값 `zabbix`에서 변경 |
| Zabbix `grafana` | Grafana 연결 전용 | 비밀번호: 난수 생성 후 버림 (사용 안 함). API 토큰: Grafana 암호화 저장소에만 |
| Grafana `admin` | 관리, bootstrap.py | `.env` `GRAFANA_ADMIN_PASSWORD` |

## 권한

Zabbix `grafana` 계정은 읽기 전용이다.

- 역할: User role
- 사용자 그룹 `grafana`: `Zabbix servers` 호스트 그룹 읽기(permission 2)만
- 검증: 이 토큰으로 `host.get` 성공, `hostgroup.create`는 `No permissions`

Grafana에서 Admin 계정으로 Zabbix에 연결하지 않는다.

## 비밀 보관

- `.env`: 권한 600, `.gitignore`의 `.env*`로 저장소 제외 (`.env.example`만 포함)
- Zabbix API 토큰: `.env`, compose, 디스크 어디에도 쓰지 않는다. bootstrap.py가 발급 즉시 Grafana API로 넘기고, Grafana는 `secureJsonData`로 암호화 저장한다. 조회 API는 `"apiToken": true`만 돌려준다
- 출력 공유 시: `docker compose config | grep -v PASSWORD`. 비밀번호·토큰은 길이(`${#VAR}`)로만 확인

## 노출되었을 때

| 대상 | 조치 |
|---|---|
| Zabbix API 토큰 | Grafana에서 데이터소스 `Zabbix` 삭제 후 `./install.sh` → 토큰 재발급, 이전 토큰 무효 |
| Zabbix 세션 토큰 | 해당 세션으로 `user.logout` |
| Zabbix Admin 비밀번호 | Zabbix UI에서 변경 후 `.env`의 `ZBX_ADMIN_PASSWORD`도 같은 값으로 수정 |
| Grafana admin 비밀번호 | Grafana UI에서 변경 후 `.env` 수정. `.env`만 바꾸면 반영 안 됨 (첫 기동 때만 읽힘) |
| PostgreSQL 비밀번호 | `.env`만 바꾸면 기존 DB와 어긋나 기동 실패. 운영 데이터가 없으면 `down -v` 후 재설치 |

## 네트워크 노출

- 호스트에 열리는 포트: Zabbix Web(기본 8081), Grafana(기본 3000)만
- PostgreSQL 5432, Zabbix server 10051은 열지 않음 (compose 내부 네트워크만)

## 외부로 나가는 호출 (미적용)

기본 설정에서 아래 항목이 외부로 통신한다. 아직 끄지 않았다. 폐쇄망·회사 환경에서 검토 대상.

- Zabbix: `AllowSoftwareUpdateCheck`
- Grafana: `GF_ANALYTICS_CHECK_FOR_UPDATES`, `GF_ANALYTICS_CHECK_FOR_PLUGIN_UPDATES`, `GF_ANALYTICS_REPORTING_ENABLED`

## 범위

이 저장소의 스크립트는 Zabbix·Grafana 자체 설정만 바꾼다. 네트워크 장비에는 아무것도 하지 않는다.
