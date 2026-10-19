#!/usr/bin/env bash
# install.sh — my-netops-platform 설치 스크립트
# Lesson 0-2 / 1단계: 사전 점검, 2단계: .env 생성, 3단계: 기동, 4~6단계: bootstrap.py
set -euo pipefail

# --- 도구 확인 ---
# command -v 는 bash 내장이라 PATH 가 비어도 동작한다
require_cmd() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "[ERROR] $1 명령을 찾을 수 없습니다. 설치 후 다시 실행하세요." >&2
        exit 1
    fi
}

require_cmd docker
require_cmd python3
require_cmd openssl

if ! docker compose version >/dev/null 2>&1; then
    echo "[ERROR] docker compose v2 가 없습니다. (docker-compose v1 은 지원하지 않음)" >&2
    exit 1
fi

echo "[OK] $(docker --version)"
echo "[OK] $(docker compose version)"
echo "[OK] $(python3 --version)"

# --- 저장소 루트로 이동 (이후 상대경로는 전부 여기 기준) ---
cd "$(dirname "$0")"

# --- .env 생성 ---
# compose.yaml 이 ${VAR:?} 형식이라 .env 없이는 docker compose 명령이 전부 실패한다.
# 그래서 compose 를 처음 호출하기 전에 .env 가 있어야 한다.
# 이미 있으면 절대 건드리지 않는다 (재실행 때 비밀번호가 바뀌면 기존 DB 볼륨과 어긋남).
if [ -f .env ]; then
    echo "[INFO] .env 가 이미 있습니다. 그대로 사용합니다."
else
    cp .env.example .env
    # change_me 를 변수별로 따로 치환해 비밀번호가 서로 다르게 한다
    for key in POSTGRES_PASSWORD ZBX_ADMIN_PASSWORD GRAFANA_ADMIN_PASSWORD; do
        sed -i "s/^${key}=.*/${key}=$(openssl rand -hex 16)/" .env
    done
    chmod 600 .env
    echo "[OK] .env 생성 (비밀번호 자동 생성, 권한 600)"
fi

# 손으로 복사해 놓고 비밀번호를 안 바꾼 경우를 compose up 전에 막는다
if grep -q '=change_me$' .env; then
    echo "[ERROR] .env 에 change_me 가 남아 있습니다. 값을 채운 뒤 다시 실행하세요." >&2
    exit 1
fi

# .env.example 에 나중에 추가된 변수가 옛 .env 에 없으면 bootstrap.py 가 실패한다
for key in $(grep -oE '^[A-Z_]+=' .env.example | tr -d '='); do
    if ! grep -q "^${key}=" .env; then
        echo "[ERROR] .env 에 ${key} 가 없습니다. .env.example 을 참고해 추가하세요." >&2
        exit 1
    fi
done

# --- .env 값 읽기 ---
read_env() {
    grep "^$1=" .env | cut -d= -f2
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

# --- 3단계: 기동 ---
# --wait: healthcheck 있는 서비스는 healthy, 없는 서비스는 running 까지 기다린다
docker compose up -d --wait
echo "[OK] 컨테이너 기동 완료"

# --- 4~6단계: Zabbix 초기 설정, Grafana 데이터소스 등록, 검증 ---
python3 scripts/bootstrap.py --env-file .env

echo "[OK] 설치 완료"
echo "     Zabbix  http://$(hostname):${ZBX_WEB_PORT}  (Admin / .env 의 ZBX_ADMIN_PASSWORD)"
echo "     Grafana http://$(hostname):${GRAFANA_PORT}  (admin / .env 의 GRAFANA_ADMIN_PASSWORD)"
