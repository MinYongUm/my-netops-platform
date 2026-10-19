#!/usr/bin/env bash
# install.sh — my-netops-platform 설치 스크립트
# Lesson 0-2 / 1단계: 사전 점검
set -euo pipefail

# --- 도구 확인 ---
# command -v 는 bash 내장이라 PATH 가 비어도 동작한다 (테스트 3 용)
require_cmd() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "[ERROR] $1 명령을 찾을 수 없습니다. 설치 후 다시 실행하세요." >&2
        exit 1
    fi
}

require_cmd docker
require_cmd python3

if ! docker compose version >/dev/null 2>&1; then
    echo "[ERROR] docker compose v2 가 없습니다. (docker-compose v1 은 지원하지 않음)" >&2
    exit 1
fi

echo "[OK] $(docker --version)"
echo "[OK] $(docker compose version)"
echo "[OK] $(python3 --version)"

# --- 저장소 루트로 이동 (이후 상대경로는 전부 여기 기준) ---
cd "$(dirname "$0")"

# --- .env 값 읽기 (.env 없으면 .env.example) ---
# source 를 안 쓰는 이유: 2단계까지는 .env 가 없을 수 있고, 변수 한 개만 필요하다
read_env() {
    local file=.env
    [ -f "$file" ] || file=.env.example
    grep "^$1=" "$file" | cut -d= -f2
}

ZBX_WEB_PORT=$(read_env ZBX_WEB_PORT)
GRAFANA_PORT=$(read_env GRAFANA_PORT)

# --- 포트 점검 ---
# netops 스택이 이미 떠 있으면 포트는 우리 것이므로 건너뛴다 (재실행 = 멱등)
if [ -n "$(docker compose ps -q)" ]; then
    echo "[INFO] netops 스택이 이미 실행 중입니다. 포트 점검을 건너뜁니다."
else
    for port in "$ZBX_WEB_PORT" "$GRAFANA_PORT"; do
        if ss -ltn | grep -qE ":${port}\b"; then
            echo "[ERROR] 포트 ${port} 이(가) 이미 사용 중입니다. .env 의 포트 값을 바꾸세요." >&2
            exit 1
        fi
    done
    echo "[OK] 포트 ${ZBX_WEB_PORT}, ${GRAFANA_PORT} 사용 가능"
fi

echo "[OK] 사전 점검 완료"
