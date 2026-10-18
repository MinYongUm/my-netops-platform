# my-netops-platform

어디를 가든 동일하게 설치해서 쓰는 개인 NetOps 플랫폼.
clone → install.sh → 사이트 프로필 작성 → 끝.

## Status

- v0.1 monitoring (Zabbix 7.0 LTS + PostgreSQL + Grafana): in progress

## Quick start (v0.1 target)

```
git clone git@github.com:MinYongUm/my-netops-platform.git
cd my-netops-platform
cp .env.example .env
docker compose up -d
```

## Structure

- compose.yaml: 서비스 정의
- .env.example: 환경변수 양식
- sites/: 사이트 프로필 (SITE-EXAMPLE.json만 추적)
- modules/: 모듈별 디렉터리
- docs/lessons/: Lesson 기록
