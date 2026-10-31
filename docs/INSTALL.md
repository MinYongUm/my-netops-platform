# 설치

## 요구사항

- Linux (검증: Ubuntu Server 24.04.5)
- Docker (검증: 29.9.0), Docker Compose v2 (검증: v5.6.0). `docker-compose` v1은 지원하지 않음
- python3 (검증: 3.12.3). 표준 라이브러리만 사용, pip 불필요
- openssl, ss (Ubuntu 기본 포함)
- 인터넷: Docker Hub(이미지), grafana.com(Grafana 플러그인)
- 실행 계정이 docker 그룹에 속해 있을 것

Docker 설치는 이 저장소의 범위가 아니다. 없으면 install.sh가 메시지를 내고 중단한다.

## 설치

```bash
git clone https://github.com/MinYongUm/my-netops-platform.git
cd my-netops-platform
./install.sh
```

처음 실행 시 `.env`가 자동 생성된다 (비밀번호 3개 난수, 권한 600).

## 접속

| 서비스 | 주소 | 계정 | 비밀번호 |
|---|---|---|---|
| Zabbix | `http://<호스트>:8081` | Admin | `.env`의 `ZBX_ADMIN_PASSWORD` |
| Grafana | `http://<호스트>:3000` | admin | `.env`의 `GRAFANA_ADMIN_PASSWORD` |

```bash
grep -E '^(ZBX_ADMIN_PASSWORD|GRAFANA_ADMIN_PASSWORD)=' .env
```

## 포트 변경

8081 또는 3000이 이미 사용 중이면 install.sh가 중단된다. 첫 실행 전에 `.env`를 직접 만들어 포트를 바꾼다.

```bash
cp .env.example .env
nano .env      # ZBX_WEB_PORT, GRAFANA_PORT 변경. change_me 는 그대로 두면 안 됨
```

`.env`를 손으로 만들 때는 `change_me` 세 곳을 모두 바꿔야 한다. 이미 설치한 뒤 포트를 바꾸려면 `.env` 수정 후 `docker compose up -d`.

## 재실행

`./install.sh`는 몇 번 실행해도 된다. 이미 있는 것은 건너뛴다. Grafana 데이터소스가 사라졌거나 연결이 실패하면 토큰을 재발급해 복구한다.

## 사이트 프로필 반영 (v0.2, 미구현)

현재 `./install.sh`는 Zabbix·Grafana 설치와 초기 설정만 한다. `sites/<회사>.yaml`을 작성해도 Zabbix에 장비가 등록되지 않는다.

- 프로필 형식: `sites/SITE-EXAMPLE.yaml`
- 반영 명령은 v0.2에서 `install.sh`와 별개로 추가할 예정이다: `python3 scripts/inventory.py sites/<회사>.yaml` (`--dry-run`은 변경 없이 미리보기). 아직 파일이 없으므로 실행할 수 없다
- v0.2는 YAML 파싱에 배포판 PyYAML을 쓸 예정이다 (Ubuntu: `sudo apt install python3-yaml`). pip 설치는 필요 없다. v0.1 설치(`./install.sh`)에는 필요 없다

설계: [ARCHITECTURE.md](ARCHITECTURE.md)

## 중지와 삭제

```bash
docker compose down                # 컨테이너만 중지. 데이터 유지
docker compose down -v && rm .env  # 데이터까지 전부 삭제. 순서 지킬 것 (down 이 .env 를 읽음)
```

`down -v`만 하고 `.env`를 남기면, 다음 설치는 남은 `.env`의 비밀번호로 새로 설치된다. 문제는 없다.

## 폐쇄망 (미검증)

인터넷이 없는 환경에서는 아래가 실패한다. 아직 검증하지 않았다.

- 이미지 다운로드: 외부에서 `docker save`, 내부에서 `docker load`로 반입하는 방법 후보
- Grafana Zabbix 플러그인: grafana.com에서 받음. 사전 반입 방법 필요
- Grafana 13 기본 플러그인: 첫 기동 때 자동으로 내려받으려 함. 끄는 방법 확인 필요

## 문제가 생기면

install.sh와 bootstrap.py는 실패한 단계를 `[ERROR]` 한 줄로 출력하고 exit 1로 끝난다. 원인을 고친 뒤 다시 실행하면 된다.

```bash
docker compose ps
docker compose logs --tail 50 zabbix-server
docker compose logs --tail 50 grafana
```
