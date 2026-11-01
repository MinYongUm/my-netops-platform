# 자동화 (GitHub Actions)

Issue #5의 자동화 구성 기록. 이 문서는 **실제로 저장소에 있는 것**만 "구현"으로 쓴다.

## 현재 상태

| 단계 | 내용 | 상태 |
|---|---|---|
| Phase 1-A | 비밀 없는 정적 검사 CI (`.github/workflows/ci.yml`) | **실제 GitHub CI 통과·실패·복구 검증 완료** (PR #7, 아래 증적) |
| Phase 1-B | 봇 코멘트 전달 테스트 (`.github/workflows/bot-event-test.yml`) | **실제 Actions 봇 코멘트 게시 및 일치하는 Claude Code ACK 확인**. 사용자 개입 없는 이벤트 수신 자체는 GitHub만으로 독립 확인 불가 |
| Phase 2 | AI PR 리뷰어 (`ai-review.yml`, `scripts/automation/ai_review.py`) | **기본 OFF 골격과 오프라인 모의 테스트만 구현** ([Issue #9](https://github.com/MinYongUm/my-netops-platform/issues/9)). 모델 호출·PR 코멘트 게시 코드 없음. **실제 AI 리뷰는 미구현·미검증**. 아래 "ai-review" |
| Phase 3 | PR → CI → AI 리뷰 → Claude Code 수정 → 재검토 E2E | 미구현 |

Phase 2·3의 전체 범위는 Issue #5, Phase 2의 세부 실행 기준은 Issue #9에 있다. 여기에 복제하지 않는다.

**아직 실제 AI 자동 리뷰(모델 호출·코멘트 게시) 및 자동 수정 루프는 없다.** Phase 1-B에서 검증된 것은 GitHub Actions 봇 코멘트 작성과 Claude Code 표시가 있는 ACK이며, 코드 수정→재검토 E2E가 아니다.

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
| automation-tests | `scripts/automation/test_*.py` 전체: Routine 전달 브리지 모의 테스트(`test_routine_bridge.py`), AI 리뷰어 모의 테스트(`test_ai_review.py`). 네트워크 연결을 막고 실행 |
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

## ai-review (Phase 2, Issue #9) — 기본 OFF

CI가 성공한 PR을 AI로 리뷰하기 위한 워크플로(`.github/workflows/ai-review.yml`)와 게이트·검증 로직(`scripts/automation/ai_review.py`). **이 버전은 리뷰를 하지 않는다.** 모델 API 호출 코드와 PR 코멘트 게시 코드가 없고, 스크립트 진입점 `main()`은 리뷰어를 연결하지 않는다. 리뷰 결과는 테스트에서만 "게시 예정 코멘트" 문자열로 만들어진다.

### 진입점과 상태

| 상황 | 결과 | 외부 호출 |
|---|---|---|
| 저장소 변수 `AI_REVIEW_ENABLED`가 정확히 `true`가 아님 (기본) | job **skipped**. 리뷰 성공 아님 | 0 |
| CI가 `pull_request`가 아님, CI 실패·미완료, fork PR | job **skipped** | 0 |
| 변수를 켬 (현재 `OPENAI_API_KEY` 미연결·리뷰어 없음) | 스크립트 `disabled` (exit 3, **빨간색**). 이벤트·GitHub API 조회 없음 | 0 |
| (테스트에서만) 켜짐 + 키 + 모의 리뷰어 | `reviewed` / `failed` / `blocked`. 코멘트는 게시하지 않음 | 가짜 객체만 |

- 트리거: `workflow_run` (`workflows: [CI]`, `types: [completed]`). `workflow_run`은 **기본 브랜치의 워크플로 파일로만** 실행되므로, 이 PR이 병합되기 전에는 동작하지 않고 PR이 이 파일을 바꿔도 그 PR의 리뷰 동작은 바뀌지 않는다.
- checkout: `workflow_run` 기본값(기본 브랜치 최신 커밋). **PR head를 checkout·실행하지 않는다.** `persist-credentials: false`.
- 권한: 워크플로 기본 `{}`, job은 `contents`·`pull-requests`·`issues`·`actions` **read**만. `pull-requests: write`, Secret 참조, `pull_request_target`, 새 외부 action 없음 (기존과 같은 `actions/checkout` SHA 고정만).
- 동시 실행: 같은 PR 브랜치는 하나만. 새 push로 시작된 실행이 오래된 실행을 취소한다.

### 켜졌을 때의 검증 순서 (코드에 있음, 모의 테스트로만 확인)

1. 이벤트: 이 저장소의 `.github/workflows/ci.yml`, `pull_request`, completed·success, 40자리 head SHA, head 저장소가 이 저장소
2. 대상 PR 확정: head SHA에 연결된 PR 중 open·base `main`·같은 저장소·같은 SHA인 것이 **정확히 1개** (이벤트에 PR 목록이 있으면 그 안에서만). 0개·여러 개면 중단, 추정하지 않음
3. PR 재조회: open, base `main`, fork 아님, 작성자 = 저장소 소유자, head SHA 일치
4. CI run 재조회: 같은 run이 여전히 success이고 같은 SHA·워크플로·이벤트인지
5. 리뷰 기록: `github-actions[bot]` 코멘트의 마커만 센다 (사람·다른 봇·PR 본문의 마커는 무시). 같은 SHA는 결과와 관계없이 다시 리뷰하지 않음. PR당 상한 기본 2회, `AI_REVIEW_MAX_ROUNDS`로 최대 3회. `failed`도 1회로 센다
6. 리뷰 입력: PR 제목·본문·변경 파일은 `untrusted_*` 필드에 **데이터로만** 담고 크기를 자른다 (본문 2만 자, 파일당 2만 자, 전체 10만 자, 파일 100개)
7. 리뷰어 1회 호출. 시간 초과·오류·형식 위반(`needs_changes`인데 지적 없음, `no_changes`인데 Blocker·Important 있음 등)은 `failed`
8. 리뷰 후 PR 재조회 (TOCTOU): head가 바뀌었거나 닫혔으면 결과를 버린다
9. 게시 예정 코멘트: 첫 줄 마커 + 결과. 리뷰어·PR에서 온 문자열의 `<!--`, `-->`, `@`는 무력화

결과·종료 코드: `reviewed` 0, `failed` 1, `blocked` 2, `disabled` 3. 키·토큰·응답 본문은 출력하지 않는다.

### 리뷰 마커 (routine-bridge와 공통 계약)

```text
<!-- ai-review:v1 head=40자리소문자SHA result=needs_changes|no_changes|failed -->
```

`needs_changes`는 수정할 지적이 있을 때만 쓴다. `routine_bridge.py`는 `needs_changes`만 전달하고 `no_changes`·`failed`는 전달하지 않는다. 두 스크립트의 정규식·리뷰어 계정이 같은지, 리뷰어가 만든 코멘트를 브리지가 위 규칙대로 판정하는지 `test_ai_review.py`가 교차 검사한다.

### 알려진 한계

- CI 성공은 **그 PR 브랜치의 `ci.yml`** 기준이다. 같은 저장소 PR은 `ci.yml`을 바꿀 수 있으므로, 작성자를 저장소 소유자로 제한하는 것이 신뢰의 근거다.
- 기록(중복·상한)은 PR 코멘트에 의존한다. 코멘트 게시가 없는 현재 버전에서는 실제로 쌓이지 않는다. 사람이 봇 코멘트를 지우면 상한이 풀린다.
- 비용 상한은 이 워크플로의 PR당 횟수 제한만으로 강제되지 않는다. 실제 활성화 전에 모델 API 계정·프로젝트의 사용량/예산 한도를 함께 설정해야 한다 (Issue #9).

### 실제 활성화 전에 필요한 것 (모두 사용자 승인, 각각 별도 PR)

1. 모델 API(OpenAI 등) 사용·과금·모델 선택과 계정 예산 한도
2. `OPENAI_API_KEY` Secret 등록과 워크플로 연결
3. 실제 리뷰어 구현 (네트워크 호출)
4. 코멘트 게시: `pull-requests: write`를 **리뷰 job과 분리된 job**에서만
5. `AI_REVIEW_ENABLED` 변수 켜기
6. Phase 3: 리뷰 → Claude 수정 → 재검토 E2E 실험

## routine-bridge (Phase 1-C, Issue #11)

AI 리뷰 결과를 Claude Code Routine API trigger(`/fire`)로 넘기는 수동 실행 워크플로(`.github/workflows/routine-bridge.yml`)와 검증 로직(`scripts/automation/routine_bridge.py`). **기본 OFF이고 Routine 토큰 Secret을 연결하지 않아 외부 호출을 하지 않는다.** 모의 테스트만 있고 실제 전달은 미검증이다. 설계, 검증 순서, 활성화·중단 절차: [ROUTINE-BRIDGE.md](ROUTINE-BRIDGE.md)

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
| Routine 생성, API trigger 토큰 발급, `CLAUDE_ROUTINE_TOKEN` Secret 등록, 워크플로에 Secret 연결, `ROUTINE_BRIDGE_ENABLED` 변수 켜기 | 사용자. 각각 별도 승인 (Phase 1-C, [ROUTINE-BRIDGE.md](ROUTINE-BRIDGE.md)) |
| AI 리뷰어: 모델 API·과금, `OPENAI_API_KEY` 연결, 실제 리뷰어·코멘트 게시 구현, `AI_REVIEW_ENABLED` 켜기 | 사용자. 각각 별도 승인 (Phase 2, 위 "ai-review") |
| PR 병합 | 사용자. 자동 병합 없음 |

## 실패와 재실행

| 상황 | 조치 |
|---|---|
| CI 빨간색 | Checks → static-checks 로그에서 `[FAIL]` 검사 이름 확인 → 로컬에서 `bash scripts/ci/static-checks.sh`로 재현 → 수정 후 push (`synchronize`로 다시 실행) |
| 러너·네트워크 오류로 중간 종료 | Actions 화면에서 Re-run 1회. 다시 실패하면 실제 실패로 본다 |
| bot-event-test "이미 테스트 코멘트가 있습니다" | 정상 차단. 재시험하려면 기존 테스트 코멘트를 지우고 다시 실행 |
| bot-event-test job skipped | 실행자가 저장소 소유자가 아님. 성공이 아님 |
| ai-review job skipped | 정상. 기본 OFF이거나 리뷰 대상 CI가 아님. **리뷰 성공이 아님** |
| ai-review `disabled` (exit 3) | 변수를 켰지만 키·리뷰어가 없음. 이 버전에서는 정상. 끄려면 `AI_REVIEW_ENABLED` 삭제 |

빈 커밋 push, 검사 삭제·비활성화로 CI를 통과시키지 않는다.

## 테스트 증적

| 날짜 | 항목 | 결과 | 근거 |
|---|---|---|---|
| 2026-10-31 | 로컬 정적 검사 (통과·실패 시나리오) | 통과 시 exit 0, 실패 시나리오 9종 exit 1. **로컬 결과이며 GitHub runner 실패 검증과 다름** | PR #6 본문 |
| 2026-10-31 | bot-event-test 스크립트 로컬 모의 실행 (가짜 `gh`, 네트워크 없음) | 거부 조건 5종 exit 1, 정상 1종 exit 0. **GitHub 실행 아님** | PR #6 본문 |
| 2026-10-31 | GitHub CI PR 이벤트 통과 1회 | success. `static-checks` 7개 검사 모두 `[OK]` (event `pull_request`, `BASE_SHA` `7bac73d`) | [run 41210744385](https://github.com/MinYongUm/my-netops-platform/actions/runs/41210744385), head `1c0fdfc` |
| 2026-11-01 KST | GitHub CI PR 이벤트 실패 감지 | **failure**. 테스트용 셸 파일의 `fi` 누락을 `shell-syntax`가 감지. `main` 미변경 | [PR #7](https://github.com/MinYongUm/my-netops-platform/pull/7), [run 41214759166](https://github.com/MinYongUm/my-netops-platform/actions/runs/41214759166), head `2afece9` |
| 2026-11-01 KST | 오류 복구 Push 후 CI 재실행 | **success**. `pull_request synchronize`로 자동 재실행 | [run 41214815996](https://github.com/MinYongUm/my-netops-platform/actions/runs/41214815996), head `dcd7a0e` |
| 2026-11-01 KST | 봇 코멘트 실험 1차 | Actions success/봇 게시 성공. **Claude가 PR #7 구독하기 전에 게시해 수신 판정 제외** | [run 41217336332](https://github.com/MinYongUm/my-netops-platform/actions/runs/41217336332); 첫 댓글은 재시험을 위해 삭제됨 |
| 2026-11-01 KST | 봇 코멘트 실험 2차 | Actions success. 구독 확인 이벤트의 **Claude 세션 보고 시각 이후**에 봇 게시 및 일치하는 Claude Code ACK 확인 (35초 차이). 무인 수신 여부는 GitHub 코멘트만으로 독립 증명 불가 | [run 41218619671](https://github.com/MinYongUm/my-netops-platform/actions/runs/41218619671), [봇 댓글](https://github.com/MinYongUm/my-netops-platform/pull/7#issuecomment-6545911195), [ACK](https://github.com/MinYongUm/my-netops-platform/pull/7#issuecomment-6545917484) |
| - | `actionlint` 워크플로 문법 검사 | **미검증** (외부 바이너리 실행이 작업 환경 권한 정책으로 막힘. GitHub가 push 시 워크플로 파일 오류를 보고하지 않은 것만 확인) | - |

### Phase 1 검증 판정 및 남은 제한 (2026-11-01 KST)

- **확정:** GitHub Actions CI는 PR 오류를 감지해 실패했고, 수정 커밋에서 정상적으로 다시 실행되어 성공했다. 봇 댓글 자동 게시와 일치하는 Claude Code ACK 댓글도 실제 GitHub 기록으로 검증했다.
- **Claude Code 이벤트 수신:** Claude 세션은 `subscription.created`를 2026-10-31 16:02:02 UTC에 수신했다고 보고했다. 재시험 봇 댓글(16:13:27 UTC)과 ACK(16:14:02 UTC)는 모두 그 이후다. 다만 GitHub 댓글 기록만으로 **별도의 사용자 메시지 없이 자동 응답했는지** 확정할 수는 없다. 필요하면 Claude 세션 이벤트 기록을 추가 증적으로 남긴다.
- **범위 밖:** Claude가 실제 **리뷰 지적사항을 수정하고 Push → 재검토**하는 E2E는 아직 시험하지 않았다. OpenAI API 리뷰 워크플로도 미구현·미활성화 상태다.
- **테스트 PR #7은 병합 금지:** 실험 파일을 운영 `main`에 넣지 않고 닫아 보존한다.
