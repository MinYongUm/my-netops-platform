# 자동화 (GitHub Actions)

Issue #5의 자동화 구성 기록. 이 문서는 **실제로 저장소에 있는 것**만 "구현"으로 쓴다.

## 현재 상태

| 단계 | 내용 | 상태 |
|---|---|---|
| Phase 1-A | 비밀 없는 정적 검사 CI (`.github/workflows/ci.yml`) | 구현. GitHub PR 이벤트 통과 1회 확인, 실패 1회는 미검증 (아래 "테스트 증적") |
| Phase 1-B | 봇 코멘트 전달 테스트 (`.github/workflows/bot-event-test.yml`) | 구현. **실행은 사용자 승인 후**. 결과 미판정 |
| Phase 2 | OpenAI API 기반 PR 리뷰 (`ai-review.yml`) | 미구현. Phase 1 증거와 사용자 승인(API 계정·과금·Secret) 전에는 만들지 않는다 |
| Phase 3 | PR → CI → AI 리뷰 → Claude Code 수정 → 재검토 E2E | 미구현 |

Phase 2·3의 설계는 Issue #5에 있다. 여기에 복제하지 않는다.

**아직 자동 수정 루프는 없다.** Phase 1-B 결과가 나오기 전에는 "봇 리뷰 → Claude Code 자동 수정"이 된다고 쓰지 않는다.

## 구성

```
PR opened / reopened / synchronize / ready_for_review, push main, 수동 실행
  └─ CI (ci.yml) ── static-checks job ── scripts/ci/static-checks.sh
                     권한: contents: read, Secret 없음

수동 실행 (저장소 소유자, 확인 문구 입력)
  └─ bot-event-test (bot-event-test.yml) ── post-test-comment job
                     권한: pull-requests: write, checkout 없음, Secret 없음
                     → 지정한 PR에 github-actions[bot] 명의 테스트 코멘트 1개
```

| 항목 | CI | bot-event-test |
|---|---|---|
| 트리거 | `pull_request`(opened, reopened, synchronize, ready_for_review), `push: main`, `workflow_dispatch` | `workflow_dispatch`만 |
| 실행자 제한 | 없음 (fork PR은 읽기 전용 토큰) | `github.actor == github.repository_owner`. 아니면 job skipped |
| 토큰 권한 | `contents: read` | `pull-requests: write` (워크플로 기본값 `{}`) |
| 저장소 코드 실행 | PR 코드를 읽기 전용 job에서 실행 | 없음 (checkout 안 함) |
| 사용자 Secret | 없음 | 없음 (자동 발급 `GITHUB_TOKEN`만) |
| `pull_request_target` | 사용 안 함 | 사용 안 함 |
| 외부 action | `actions/checkout` v7.0.1, 커밋 SHA 고정 | 없음 (runner 기본 `gh`, `jq`) |

## CI 정적 검사

`scripts/ci/static-checks.sh`가 아래를 모두 실행하고, 하나라도 실패하면 exit 1 (Check 빨간색).

| 검사 | 내용 |
|---|---|
| shell-syntax | 추적 중인 `*.sh`에 `bash -n` |
| python-compile | 추적 중인 `*.py`에 `python3 -m py_compile`. 바이트코드는 임시 디렉터리(`PYTHONPYCACHEPREFIX`)에만 생기고 저장소에 남지 않음 |
| scanner-tests | 민감정보 검사기 단위 테스트 (`scripts/ci/test_scan_sensitive.py`) |
| compose-config | `.env.example`의 `change_me`를 가짜 값으로 바꾼 임시 파일로 `docker compose config --quiet`. 컨테이너를 띄우지 않고, 실제 `.env`를 읽지 않음 |
| whitespace | `git diff --check`. PR은 base 커밋 대비, push는 이전 커밋 대비, 수동 실행은 전체 트리 |
| forbidden-files | 추적 중인 `.env*`(`.env.example` 제외), `sites/` 아래 `SITE-EXAMPLE.yaml` 외 파일 |
| sensitive-patterns | `scripts/ci/scan_sensitive.py`: 개인키, AWS·GitHub·Slack 토큰, `sk-` 형식 API 키, 64자리 hex(Zabbix API 토큰 형식), 사설·루프백·문서용 범위 밖의 IPv4, `.env.example`의 비밀 키 값이 `change_me`가 아닌 경우. **찾은 값은 로그에 출력하지 않고** 파일·줄·종류만 출력 |

CI가 통과해도 Docker 통합 테스트(설치·기동·API 연동) 통과를 뜻하지 않는다. 그 테스트는 CI에 없다.

### 로컬 실행

저장소 루트에서:

```bash
bash scripts/ci/static-checks.sh
```

`main` 대비 변경분만 공백 검사하려면:

```bash
BASE_SHA="$(git merge-base origin/main HEAD)" bash scripts/ci/static-checks.sh
```

필요: bash, python3, git, docker compose v2 (구성 해석만 함. Docker 데몬에 컨테이너를 만들지 않음).

### 오탐이 나면

- `public-ipv4`: 공개 저장소에는 사설·문서용(`192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`) 주소만 쓴다. 버전 문자열 등 IP가 아닌 4자리 숫자열이 걸리면 검사기 수정은 별도 PR로 한다.
- 검사를 끄거나 건너뛰어서 통과시키지 않는다.

## bot-event-test (Phase 1-B)

Claude Code 웹 세션이 **사람이 아닌 `github-actions[bot]`이 남긴 PR 코멘트**를 사용자 메시지 없이 받는지 확인한다. 사람 계정 코멘트가 전달된다고 해서 봇 코멘트도 전달된다고 보지 않는다.

### 실행 전 조건 (사용자)

1. 테스트에 쓸 PR을 정하고 승인한다. 조건: open, base `main`, 같은 저장소 브랜치(fork 아님)
2. 그 PR을 Claude Code 웹 세션이 구독 중인지 확인한다 (PR 활동 구독 / Auto-fix)
3. 이 워크플로가 `main`에 있어야 Actions 화면에서 수동 실행할 수 있다 (`workflow_dispatch`는 기본 브랜치의 워크플로 파일 기준)

### 실행

GitHub → Actions → bot-event-test → Run workflow:

- `pr_number`: 승인한 테스트 PR 번호
- `confirm`: `post-bot-event-test`

또는 gh CLI (테스트 PR 번호를 변수에 넣어서):

```bash
PR_NUMBER="REPLACE_ME"   # 승인한 테스트 PR 번호(숫자)로 바꾼다. 그대로 실행하면 워크플로가 거부한다
gh workflow run bot-event-test.yml --repo MinYongUm/my-netops-platform -f pr_number="$PR_NUMBER" -f confirm=post-bot-event-test
```

### 남용 방지

- 수동 실행만, 저장소 소유자만, 확인 문구가 정확해야 실행
- PR 번호는 양의 정수만. 입력값은 환경변수로만 스크립트에 전달 (명령 주입 방지)
- open·base `main`·같은 저장소 PR만
- 코멘트에 고유 마커(`<!-- bot-event-test -->`, `<!-- bot-event-test:run=실행번호 -->`)를 넣고, 같은 PR에 마커가 있는 봇 코멘트가 이미 있으면 **게시하지 않고 실패**. 다시 테스트하려면 기존 테스트 코멘트를 사람이 지운 뒤 실행
- `concurrency`로 동시에 하나만 실행

### 코멘트 내용

무해한 요청 하나만 담는다: "받았다면 이 PR에 `bot-event-test ack run=실행번호` 한 줄만 답글로 남기고, 파일 수정·push는 하지 말 것". 실행 기록 링크와 게시 시점 PR head SHA를 함께 적는다.

### 결과 판정

사용자 또는 Claude Code 세션 기록으로 판정한다. 사용자가 코멘트를 전달하거나 "확인해 줘"라고 말한 뒤의 반응은 수신으로 치지 않는다.

| 결과 | 기준 |
|---|---|
| 성공 | 사용자 메시지 없이 세션이 코멘트를 받고, 요청대로 `ack` 답글을 남김 |
| 수신은 됐지만 처리 안 함 | 세션 기록에 코멘트 이벤트가 있으나 답글이 없음 |
| 미수신 | 세션 기록에 해당 코멘트 이벤트가 없음 (사람 코멘트는 같은 조건에서 전달됨) |
| 확인 불가 | 세션 기록을 볼 수 없거나, 구독 상태를 확인할 수 없음 |

### 실패 시 (Gate)

성공이 아니면 자동 수정 루프 구현을 보류한다. Claude GitHub Action 등 공식 대안의 추가 인증·비용·권한을 다시 검토해 사용자 승인을 받는다. 그 전에 Phase 2(유료 API 리뷰)를 먼저 연결하지 않는다. 사람이 코멘트를 수동으로 전달해야 한다면 "전체 자동화 완료"라고 하지 않는다.

## 승인이 필요한 것

| 작업 | 승인 |
|---|---|
| bot-event-test 실행 | 사용자 (테스트 PR 지정) |
| CI를 필수 Check로 지정 (브랜치 보호 규칙) | 사용자가 직접 설정. Claude는 보호 규칙을 바꾸지 않음 |
| OpenAI API 계정·과금, `OPENAI_API_KEY` Secret 등록 | 사용자가 승인하고 직접 설정 (Phase 2) |
| 토큰 권한 확대, 새 Secret 추가 | 사용자 |
| PR 병합 | 사용자. 자동 병합 없음 |

## 실패와 재실행

| 상황 | 조치 |
|---|---|
| CI 빨간색 | Checks → static-checks 로그에서 `[FAIL]` 검사 이름 확인 → 로컬에서 `bash scripts/ci/static-checks.sh`로 재현 → 수정 후 push (`synchronize`로 다시 실행) |
| 러너·네트워크 오류로 중간 종료 | Actions 화면에서 Re-run 1회. 다시 실패하면 실제 실패로 본다 |
| bot-event-test "이미 테스트 코멘트가 있습니다" | 정상 차단. 재시험하려면 기존 테스트 코멘트를 지우고 다시 실행 |
| bot-event-test job skipped | 실행자가 저장소 소유자가 아님. 성공이 아님 |

빈 커밋 push, 검사 삭제·비활성화로 CI를 통과시키지 않는다.

## 테스트 증적

| 날짜 | 항목 | 결과 | 근거 |
|---|---|---|---|
| 2026-10-31 | 로컬 정적 검사 (통과·실패 시나리오) | 통과 시 exit 0, 실패 시나리오 9종 exit 1. **로컬 결과이며 GitHub runner 실패 검증과 다름** | PR #6 본문 |
| 2026-10-31 | bot-event-test 스크립트 로컬 모의 실행 (가짜 `gh`, 네트워크 없음) | 거부 조건 5종 exit 1, 정상 1종 exit 0. **GitHub 실행 아님** | PR #6 본문 |
| 2026-10-31 | GitHub CI PR 이벤트 통과 1회 | success. `static-checks` 7개 검사 모두 `[OK]` (event `pull_request`, `BASE_SHA` `7bac73d`) | [run 41210744385](https://github.com/MinYongUm/my-netops-platform/actions/runs/41210744385), head `1c0fdfc` |
| - | GitHub CI PR 이벤트 실패 1회 | **미검증** (사용자 승인 후 별도 실험 커밋/PR로 시험) | PR Checks 링크·head SHA |
| - | bot-event-test GitHub 실행, `github-actions[bot]` 코멘트 게시, Claude 수신·ACK | **미실행·미검증** (`main` 병합 후, 승인된 별도 open 테스트 PR에서) | 실행 기록 링크, 세션 기록 |
| - | `actionlint` 워크플로 문법 검사 | **미검증** (외부 바이너리 실행이 작업 환경 권한 정책으로 막힘. GitHub가 push 시 워크플로 파일 오류를 보고하지 않은 것만 확인) | - |
