# my-netops-platform

어디를 가든 동일하게 설치해서 쓰는 개인 NetOps 플랫폼.
목표 흐름: clone → `./install.sh` → 사이트 프로필 작성 → 장비 자동 등록.
현재는 설치(`./install.sh`)까지 동작한다. 사이트 프로필 자동 반영은 v0.2에서 구현 예정이다.

## Status

- v0.1 monitoring (Zabbix 7.0 LTS + PostgreSQL 16 + Grafana): 완료 (`v0.1` 태그). 설치 패키지
- v0.2 inventory (사이트 프로필 → Zabbix 호스트 등록): 진행 중. 설계 확정, `scripts/inventory.py` 미구현
- v0.3 snmp: 계획

상세: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

## Quick start

```bash
git clone https://github.com/MinYongUm/my-netops-platform.git
cd my-netops-platform
./install.sh
```

`.env`는 `install.sh`가 자동 생성한다. 요구사항, 접속, 포트 변경: [docs/INSTALL.md](docs/INSTALL.md)

## Structure

- compose.yaml: 서비스 정의
- .env.example: 환경변수 양식
- install.sh: 설치 스크립트 (사전 점검, `.env` 생성, 기동, bootstrap.py 호출)
- scripts/bootstrap.py: Zabbix 초기 설정, Grafana 데이터소스 등록
- sites/: 사이트 프로필 (SITE-EXAMPLE.yaml만 추적). v0.2 inventory가 읽을 예정
- docs/: 설치(INSTALL), 보안(SECURITY), 아키텍처·진행 상태(ARCHITECTURE)
- docs/lessons/: Lesson 기록
- CLAUDE.md: Claude Code 작업 규칙
