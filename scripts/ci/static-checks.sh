#!/usr/bin/env bash
# static-checks.sh — 비밀 없는 정적 검사 (Issue #5 Phase 1)
#
# 컨테이너를 띄우지 않고, 회사·LAB 장비에 접속하지 않는다. 통과해도 Docker 통합 테스트 통과가 아니다.
#
# 로컬 실행 (저장소 루트에서):
#   bash scripts/ci/static-checks.sh                    # 저장소 전체 트리 기준
#   BASE_SHA="$(git merge-base origin/main HEAD)" bash scripts/ci/static-checks.sh   # main 대비 변경분 기준
#
# 모든 검사를 끝까지 실행하고, 하나라도 실패하면 exit 1.
set -uo pipefail

cd "$(dirname "$0")/../.." || exit 1

EMPTY_TREE=$(git hash-object -t tree /dev/null)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

failed=()

group_start() { [ "${GITHUB_ACTIONS:-}" = "true" ] && echo "::group::$1" || echo "== $1"; }
group_end() { [ "${GITHUB_ACTIONS:-}" = "true" ] && echo "::endgroup::"; return 0; }

# check <이름> <함수>: 함수가 0이 아니면 실패로 기록
check() {
    local name=$1 fn=$2
    group_start "$name"
    if "$fn"; then
        echo "[OK] $name"
    else
        echo "[FAIL] $name"
        [ "${GITHUB_ACTIONS:-}" = "true" ] && echo "::error::$name 실패"
        failed+=("$name")
    fi
    group_end
}

# --- 1. 셸 문법 ---
shell_syntax() {
    local rc=0 f
    while IFS= read -r -d '' f; do
        echo "bash -n $f"
        bash -n "$f" || rc=1
    done < <(git ls-files -z '*.sh')
    return $rc
}

# --- 2. Python 문법 (바이트코드는 임시 디렉터리에만, 저장소에 남기지 않음) ---
python_compile() {
    local files=()
    while IFS= read -r -d '' f; do files+=("$f"); done < <(git ls-files -z '*.py')
    [ ${#files[@]} -eq 0 ] && return 0
    printf 'py_compile %s\n' "${files[@]}"
    PYTHONPYCACHEPREFIX="$WORK/pycache" python3 -m py_compile "${files[@]}"
}

# --- 3. 검사기 단위 테스트 ---
scanner_tests() {
    python3 -B -m unittest discover -s scripts/ci -p 'test_*.py'
}

# --- 3-2. 자동화 브리지 모의 테스트 (네트워크 없음, Issue #11) ---
automation_tests() {
    python3 -B -m unittest discover -s scripts/automation -p 'test_*.py'
}

# --- 4. Compose 구성 (가짜 값만, 출력 없이 검증) ---
compose_config() {
    if ! docker compose version >/dev/null 2>&1; then
        echo "docker compose v2 가 없습니다. 이 검사는 건너뛰지 않고 실패로 처리합니다."
        return 1
    fi
    # .env.example 의 change_me 만 가짜 값으로 바꾼다. 실제 .env 는 읽지 않는다
    sed 's/=change_me$/=ci-dummy-not-a-secret/' .env.example > "$WORK/ci.env"
    docker compose --project-directory . --env-file "$WORK/ci.env" config --quiet
}

# --- 5. 공백 오류 ---
whitespace() {
    local base=${BASE_SHA:-}
    if [ -z "$base" ] || [[ "$base" =~ ^0+$ ]]; then
        echo "기준: 저장소 전체 트리"
        base=$EMPTY_TREE
    elif ! git cat-file -e "${base}^{commit}" 2>/dev/null; then
        echo "BASE_SHA $base 커밋을 찾을 수 없습니다 (checkout fetch-depth 확인)."
        return 1
    else
        echo "기준: $base"
    fi
    git diff --check "$base" HEAD
}

# --- 6. 추적 금지 파일 (.env, 실제 사이트 프로필) ---
forbidden_files() {
    local bad
    bad=$(git ls-files | grep -E '(^|/)\.env' | grep -vxF '.env.example'
          git ls-files sites/ | grep -vxF 'sites/SITE-EXAMPLE.yaml')
    if [ -n "$bad" ]; then
        echo "저장소에 올리면 안 되는 파일:"
        echo "$bad"
        return 1
    fi
    echo "추적 금지 파일 없음"
}

# --- 7. 민감정보 패턴 ---
sensitive() {
    python3 -B scripts/ci/scan_sensitive.py
}

check "shell-syntax" shell_syntax
check "python-compile" python_compile
check "scanner-tests" scanner_tests
check "automation-tests" automation_tests
check "compose-config" compose_config
check "whitespace" whitespace
check "forbidden-files" forbidden_files
check "sensitive-patterns" sensitive

echo
if [ ${#failed[@]} -gt 0 ]; then
    echo "[FAIL] 실패한 검사: ${failed[*]}"
    exit 1
fi
echo "[OK] 정적 검사 전체 통과 (Docker 통합 테스트는 포함하지 않음)"
