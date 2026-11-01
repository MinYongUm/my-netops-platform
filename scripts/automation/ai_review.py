#!/usr/bin/env python3
"""ai_review.py — CI 가 성공한 PR 을 AI 로 리뷰하는 리뷰어의 게이트·검증 로직 (Issue #9 Phase 2, 기본 OFF).

이 PR 범위에서는 **실제 리뷰를 하지 않는다.**
  - OpenAI 등 모델 API 호출 코드가 없다. 리뷰어는 주입식(Reviewer 프로토콜)이고, 오프라인 MockReviewer 만 있다
  - PR 코멘트 게시 코드가 없다. 결과는 "게시 예정 코멘트"(planned_comment) 문자열로만 만든다
  - main() 은 리뷰어를 연결하지 않으므로, 켜져 있어도 결과는 항상 disabled 다

꺼짐 조건 (하나라도 해당하면 disabled, exit 3. GitHub API·이벤트 파일·리뷰어를 전혀 쓰지 않는다):
  - AI_REVIEW_ENABLED 가 정확히 소문자 true 가 아님
  - OPENAI_API_KEY 없음 (워크플로는 이 Secret 을 연결하지 않는다)
  - 리뷰어 구현 없음 (main() 은 항상 이 상태)

켜져 있고 리뷰어가 주입됐을 때의 순서 (테스트에서만 도달):
  1. workflow_run 이벤트 검사: 이 저장소의 CI(.github/workflows/ci.yml), pull_request 이벤트, completed·success,
     40자리 head SHA, head 저장소가 이 저장소(fork 아님)
  2. GitHub API 로 대상 PR 을 확정: head SHA 에 연결된 open·base main·같은 저장소·같은 SHA 인 PR 이 정확히 1개
     (이벤트의 pull_requests 목록이 있으면 그 안에서만). 0개·여러 개면 중단
  3. PR 재조회: open, base main, fork 아님, 작성자=저장소 소유자, head SHA 일치
  4. CI run 재조회: 같은 run 이 아직 completed·success 이고 같은 SHA·같은 워크플로인지
  5. 리뷰 기록(github-actions[bot] 코멘트의 ai-review 마커): 같은 SHA 중복 금지, PR 당 상한(기본 2, 최대 3)
  6. 리뷰 입력 구성: PR 제목·본문·변경 파일은 비신뢰 데이터로만 담는다
  7. 리뷰어 호출 1회. 시간 초과·오류·형식 위반은 failed.
     입력이 잘렸거나 patch 가 없는 파일이 있으면 no_changes 를 허용하지 않고 failed (review-input-incomplete)
  8. 리뷰 후 PR 재조회(TOCTOU): head 가 바뀌었거나 닫혔으면 결과를 버린다
  9. 게시 예정 코멘트 생성. 마커 형식은 routine_bridge.py 의 계약과 같다:
     <!-- ai-review:v1 head=<40hex> result=needs_changes|no_changes|failed -->

결과와 종료 코드: reviewed 0, failed 1, blocked 2, disabled 3. 토큰·키·응답 본문은 출력하지 않는다.
표준 라이브러리만 사용한다. 운영 문서: docs/AUTOMATION.md
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Mapping, Protocol

# --- 신뢰된 main 에 고정하는 정책 (이벤트·PR 내용으로 바꿀 수 없음) ---

TRUSTED_REPO = "MinYongUm/my-netops-platform"
BASE_BRANCH = "main"
CI_WORKFLOW_PATH = ".github/workflows/ci.yml"
REVIEWER_LOGIN = "github-actions[bot]"

DEFAULT_MAX_ROUNDS = 2
MAX_ROUNDS_LIMIT = 3
COMMENT_PER_PAGE = 100
COMMENT_MAX_PAGES = 10
FILES_PER_PAGE = 100  # 리뷰 입력에 담는 변경 파일 상한 (1페이지만 읽는다)

# 비신뢰 입력 크기 상한 (모델 비용·프롬프트 크기 통제)
MAX_TITLE_CHARS = 300
MAX_BODY_CHARS = 20_000
MAX_PATCH_CHARS_PER_FILE = 20_000
MAX_PATCH_CHARS_TOTAL = 100_000
MAX_COMMENT_CHARS = 60_000
MAX_FINDINGS = 50
GITHUB_TIMEOUT = 10

# --- 형식 ---

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
OUTCOMES = ("needs_changes", "no_changes", "failed")
SEVERITIES = ("Blocker", "Important", "Minor")
# routine_bridge.REVIEW_MARKER_RE 와 같은 계약. 바꾸면 양쪽을 함께 바꾼다 (test_ai_review.py 가 교차 검사)
MARKER_TEMPLATE = "<!-- ai-review:v1 head={head} result={result} -->"
MARKER_RE = re.compile(r"<!-- ai-review:v1 head=([0-9a-f]{40}) result=(needs_changes|no_changes|failed) -->")

EXIT_CODES = {"reviewed": 0, "failed": 1, "blocked": 2, "disabled": 3}


@dataclass(frozen=True)
class Result:
    status: str  # reviewed | failed | blocked | disabled
    reason: str
    outcome: str = ""  # needs_changes | no_changes | failed (리뷰어까지 간 경우만)
    pr_number: int = 0
    head_sha: str = ""
    # 게시 예정 코멘트. 이 PR 범위에서는 어디에도 게시하지 않는다
    planned_comment: str = field(default="", repr=False)

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.status]

    def line(self) -> str:
        out = f"ai-review result={self.status} reason={self.reason}"
        if self.outcome:
            out += f" outcome={self.outcome}"
        if self.pr_number:
            out += f" pr={self.pr_number}"
        if self.head_sha:
            out += f" head={self.head_sha}"
        return out


@dataclass(frozen=True)
class Config:
    enabled: bool
    api_key: str = field(repr=False)
    github_token: str = field(repr=False)
    max_rounds_raw: str

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Config":
        return cls(
            enabled=env.get("AI_REVIEW_ENABLED", "") == "true",
            api_key=env.get("OPENAI_API_KEY", ""),
            github_token=env.get("GITHUB_TOKEN", ""),
            max_rounds_raw=env.get("AI_REVIEW_MAX_ROUNDS", ""),
        )


@dataclass(frozen=True)
class Event:
    run_id: int
    head_sha: str
    payload_prs: tuple[int, ...]  # 이벤트의 pull_requests 목록 (fork·일부 경우 비어 있음)


@dataclass(frozen=True)
class ChangedFile:
    filename: str
    status: str
    patch: str
    truncated: bool


@dataclass(frozen=True)
class ReviewInput:
    """리뷰어에 넘기는 데이터. 아래 untrusted_* 는 PR 작성자가 바꿀 수 있는 값이다 (지시가 아니라 데이터)."""

    repo: str
    pr_number: int
    head_sha: str
    round_no: int
    max_rounds: int
    untrusted_title: str
    untrusted_body: str
    untrusted_files: tuple[ChangedFile, ...]
    files_truncated: bool  # 파일 목록이 상한에 걸렸거나, 어떤 파일의 patch 가 잘렸거나 없음
    text_truncated: bool = False  # 제목·본문이 잘림

    @property
    def incomplete(self) -> bool:
        """리뷰어가 변경 전체를 보지 못했다. 이 경우 no_changes(깨끗함) 판정을 허용하지 않는다."""
        return self.files_truncated or self.text_truncated


@dataclass(frozen=True)
class Finding:
    severity: str  # Blocker | Important | Minor
    file: str
    line: int
    text: str


@dataclass(frozen=True)
class ReviewOutcome:
    result: str  # needs_changes | no_changes
    summary: str
    findings: tuple[Finding, ...] = ()


class Reviewer(Protocol):
    def review(self, data: ReviewInput) -> ReviewOutcome: ...


class ReviewerError(Exception):
    """리뷰어(모델 API 등) 실패. 메시지는 출력하지 않는다."""


class GitHub(Protocol):
    def get(self, path: str) -> object: ...


class GitHubError(Exception):
    pass


class MockReviewer:
    """오프라인 모의 리뷰어. 정해 둔 결과를 돌려주거나 정해 둔 예외를 던진다. 네트워크를 쓰지 않는다."""

    def __init__(self, outcome: ReviewOutcome | None = None, exc: BaseException | None = None) -> None:
        self.outcome = outcome or ReviewOutcome("no_changes", "mock review: no findings")
        self.exc = exc
        self.inputs: list[ReviewInput] = []

    def review(self, data: ReviewInput) -> ReviewOutcome:
        self.inputs.append(data)
        if self.exc:
            raise self.exc
        return self.outcome


# --- 1. 설정 ---


def max_rounds(config: Config) -> int | None:
    raw = config.max_rounds_raw
    if raw == "":
        return DEFAULT_MAX_ROUNDS
    if not re.fullmatch(r"[1-9]", raw):
        return None
    value = int(raw)
    return value if value <= MAX_ROUNDS_LIMIT else None


def check_config(config: Config, reviewer: Reviewer | None) -> Result | None:
    """꺼져 있으면 disabled. 이 단계까지는 네트워크·파일을 쓰지 않는다."""
    if not config.enabled:
        return Result("disabled", "flag-off")
    if not config.api_key:
        return Result("disabled", "no-api-key")
    if reviewer is None:
        return Result("disabled", "reviewer-not-implemented")
    if max_rounds(config) is None:
        return Result("failed", "config-invalid:max-rounds")
    if not config.github_token:
        return Result("failed", "no-github-token")
    return None


# --- 2. 이벤트 ---


def parse_event(payload: object, repo: str = TRUSTED_REPO) -> Event | Result:
    if not isinstance(payload, dict):
        return Result("failed", "event-invalid")
    run = payload.get("workflow_run")
    if not isinstance(run, dict):
        return Result("failed", "event-invalid")
    if (payload.get("repository") or {}).get("full_name") != repo:
        return Result("blocked", "event-repo-mismatch")
    if run.get("path") != CI_WORKFLOW_PATH:
        return Result("blocked", "not-ci-workflow")
    if run.get("event") != "pull_request":
        return Result("blocked", "not-pull-request-event")
    if run.get("status") != "completed":
        return Result("blocked", "ci-not-completed")
    if run.get("conclusion") != "success":
        return Result("blocked", "ci-not-success")
    head_sha = run.get("head_sha")
    if not isinstance(head_sha, str) or not SHA_RE.fullmatch(head_sha):
        return Result("blocked", "invalid-event:head_sha")
    if (run.get("head_repository") or {}).get("full_name") != repo:
        return Result("blocked", "pr-from-fork")
    run_id = run.get("id")
    if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id <= 0:
        return Result("failed", "event-invalid")
    numbers = []
    for pr in run.get("pull_requests") or []:
        number = pr.get("number") if isinstance(pr, dict) else None
        if isinstance(number, int) and not isinstance(number, bool) and number > 0:
            numbers.append(number)
    return Event(run_id, head_sha, tuple(numbers))


# --- 3. 대상 PR 확정·재조회 ---


def _same_repo_pr(pull: dict, repo: str, head_sha: str) -> bool:
    head = pull.get("head") or {}
    base = pull.get("base") or {}
    return (
        pull.get("state") == "open"
        and base.get("ref") == BASE_BRANCH
        and (base.get("repo") or {}).get("full_name") == repo
        and (head.get("repo") or {}).get("full_name") == repo
        and head.get("sha") == head_sha
    )


def resolve_pull(gh: GitHub, ev: Event, repo: str) -> int | Result:
    """head SHA 에 연결된 PR 중 조건에 맞는 것이 정확히 1개일 때만 그 번호. 추정하지 않는다."""
    pulls = gh.get(f"/repos/{repo}/commits/{ev.head_sha}/pulls")
    if not isinstance(pulls, list):
        return Result("failed", "github-unexpected-response")
    found = [p for p in pulls if isinstance(p, dict) and _same_repo_pr(p, repo, ev.head_sha)]
    numbers = sorted({p.get("number") for p in found if isinstance(p.get("number"), int)})
    if not numbers:
        return Result("blocked", "pr-not-found")
    if ev.payload_prs:
        # 이벤트가 PR 을 지정했으면 그 안에서만 고른다. 겹치지 않으면 추정하지 않고 중단
        numbers = [n for n in numbers if n in ev.payload_prs]
        if not numbers:
            return Result("blocked", "pr-mismatch")
    if len(numbers) > 1:
        return Result("blocked", "pr-ambiguous")
    return numbers[0]


def verify_pull(gh: GitHub, number: int, head_sha: str, repo: str) -> dict | Result:
    pull = gh.get(f"/repos/{repo}/pulls/{number}")
    if not isinstance(pull, dict):
        return Result("failed", "github-unexpected-response")
    owner = repo.split("/", 1)[0]
    head = pull.get("head") or {}
    base = pull.get("base") or {}
    if pull.get("state") != "open" or pull.get("merged"):
        return Result("blocked", "pr-not-open")
    if (base.get("repo") or {}).get("full_name") != repo or base.get("ref") != BASE_BRANCH:
        return Result("blocked", "pr-base-not-main")
    if (head.get("repo") or {}).get("full_name") != repo:
        return Result("blocked", "pr-from-fork")
    if (pull.get("user") or {}).get("login") != owner:
        return Result("blocked", "pr-author-not-owner")
    if head.get("sha") != head_sha:
        return Result("blocked", "stale-head")
    return pull


def verify_ci_run(gh: GitHub, ev: Event, repo: str) -> Result | None:
    """이벤트 값 대신 run 을 다시 조회한다 (re-run 으로 결과가 바뀌었을 수 있다)."""
    run = gh.get(f"/repos/{repo}/actions/runs/{ev.run_id}")
    if not isinstance(run, dict):
        return Result("failed", "github-unexpected-response")
    if (
        run.get("head_sha") != ev.head_sha
        or run.get("path") != CI_WORKFLOW_PATH
        or run.get("event") != "pull_request"
        or (run.get("head_repository") or {}).get("full_name") != repo
    ):
        return Result("blocked", "ci-run-mismatch")
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        return Result("blocked", "ci-not-success")
    return None


# --- 4. 리뷰 기록 (중복·상한) ---


def load_markers(gh: GitHub, number: int, repo: str) -> list[tuple[str, str]] | None:
    """github-actions[bot] 이 쓴 코멘트의 마커만 센다. 사람·PR 본문의 마커는 무시한다 (위조 방지)."""
    markers: list[tuple[str, str]] = []
    for page in range(1, COMMENT_MAX_PAGES + 1):
        query = urllib.parse.urlencode({"per_page": COMMENT_PER_PAGE, "page": page})
        batch = gh.get(f"/repos/{repo}/issues/{number}/comments?{query}")
        if not isinstance(batch, list):
            raise GitHubError()
        for comment in batch:
            if not isinstance(comment, dict):
                continue
            user = comment.get("user") or {}
            if user.get("login") != REVIEWER_LOGIN or user.get("type") != "Bot":
                continue
            markers += MARKER_RE.findall(comment.get("body") or "")
        if len(batch) < COMMENT_PER_PAGE:
            return markers
    return None  # 기록이 너무 많아 다 읽지 못함 → 안전하게 실패로 본다


def check_ledger(gh: GitHub, number: int, head_sha: str, repo: str, limit: int) -> tuple[Result | None, int]:
    markers = load_markers(gh, number, repo)
    if markers is None:
        return Result("failed", "ledger-incomplete"), 0
    heads = {sha for sha, _ in markers}  # failed 도 1회로 센다 (비용이 이미 발생했을 수 있음)
    if head_sha in heads:
        return Result("blocked", "duplicate-sha"), len(heads)
    if len(heads) >= limit:
        return Result("blocked", "round-limit"), len(heads)
    return None, len(heads)


# --- 5. 리뷰 입력 (비신뢰 데이터) ---


def _cap(text: object, limit: int) -> tuple[str, bool]:
    value = text if isinstance(text, str) else ""
    return (value[:limit], True) if len(value) > limit else (value, False)


def build_review_input(gh: GitHub, pull: dict, number: int, head_sha: str, repo: str,
                       round_no: int, limit: int) -> ReviewInput:
    query = urllib.parse.urlencode({"per_page": FILES_PER_PAGE, "page": 1})
    raw_files = gh.get(f"/repos/{repo}/pulls/{number}/files?{query}")
    if not isinstance(raw_files, list):
        raise GitHubError()
    files = []
    total = 0
    truncated_any = len(raw_files) >= FILES_PER_PAGE  # 더 있을 수 있다
    for item in raw_files:
        if not isinstance(item, dict):
            continue
        patch, cut = _cap(item.get("patch"), MAX_PATCH_CHARS_PER_FILE)
        if not isinstance(item.get("patch"), str):
            cut = True  # GitHub 가 patch 를 생략함 (큰 diff·바이너리) → 리뷰어가 내용을 못 봄
        room = MAX_PATCH_CHARS_TOTAL - total
        if len(patch) > room:
            patch, cut = patch[:max(room, 0)], True
        total += len(patch)
        truncated_any = truncated_any or cut
        files.append(ChangedFile(_cap(item.get("filename"), 300)[0], _cap(item.get("status"), 20)[0], patch, cut))
    title, title_cut = _cap(pull.get("title"), MAX_TITLE_CHARS)
    body, body_cut = _cap(pull.get("body"), MAX_BODY_CHARS)
    return ReviewInput(repo, number, head_sha, round_no, limit, title, body, tuple(files), truncated_any,
                       title_cut or body_cut)


# --- 6. 리뷰어 결과 검사 ---


def validate_outcome(outcome: object) -> ReviewOutcome | None:
    """형식이 어긋나면 None. 리뷰어(모델) 출력도 비신뢰로 본다."""
    if not isinstance(outcome, ReviewOutcome) or outcome.result not in ("needs_changes", "no_changes"):
        return None
    if not isinstance(outcome.findings, tuple) or len(outcome.findings) > MAX_FINDINGS:
        return None
    for f in outcome.findings:
        if not isinstance(f, Finding) or f.severity not in SEVERITIES:
            return None
        if not isinstance(f.line, int) or isinstance(f.line, bool) or f.line < 0:
            return None
    serious = any(f.severity in ("Blocker", "Important") for f in outcome.findings)
    if outcome.result == "needs_changes" and not outcome.findings:
        return None  # 고칠 것이 없는데 수정 요청
    if outcome.result == "no_changes" and serious:
        return None  # Blocker·Important 가 있는데 수정 불필요
    return outcome


# --- 7. 게시 예정 코멘트 ---


def neutralize(text: object, limit: int = 2000) -> str:
    """리뷰어·PR 에서 온 문자열을 코멘트에 넣기 전에 마커 위조·멘션·HTML 주석을 무력화한다."""
    value = text if isinstance(text, str) else ""
    value = value.replace("<!--", "&lt;!--").replace("-->", "--&gt;").replace("@", "@​")
    value = " ".join(value.split())  # 줄바꿈으로 표 구조를 깨지 않게
    return value[:limit]


def render_comment(repo: str, number: int, head_sha: str, run_id: int, round_no: int, limit: int,
                   result: str, outcome: ReviewOutcome | None, reason: str = "") -> str:
    lines = [
        MARKER_TEMPLATE.format(head=head_sha, result=result),
        f"**AI review** ({result}) — round {round_no}/{limit}",
        "",
        f"- PR: #{number}, head `{head_sha}`",
        f"- CI run: https://github.com/{repo}/actions/runs/{run_id}",
    ]
    if result == "failed":
        lines += ["", f"리뷰 실패 (`{neutralize(reason, 100)}`). 리뷰 완료가 아니다."]
    elif outcome is not None:
        lines += ["", neutralize(outcome.summary)]
        if outcome.findings:
            lines += ["", "| 심각도 | 파일 | 줄 | 내용 |", "|---|---|---|---|"]
            for f in outcome.findings:
                text = neutralize(f.text).replace("|", "\\|")
                lines.append(f"| {f.severity} | `{neutralize(f.file, 300).replace('`', '')}` | {f.line} | {text} |")
    lines += ["", "_이 코멘트는 자동 리뷰 결과이며 승인·병합 결정이 아니다._"]
    body = "\n".join(lines)
    return body[:MAX_COMMENT_CHARS]


# --- 전체 흐름 ---


def run_review(
    config: Config,
    event_loader: Callable[[], object],
    gh_factory: Callable[[], GitHub],
    reviewer: Reviewer | None,
    repo: str = TRUSTED_REPO,
) -> Result:
    off = check_config(config, reviewer)
    if off:
        return off  # 이벤트 파일도 읽지 않고, GitHub 클라이언트도 만들지 않는다
    if reviewer is None:  # check_config 가 이미 막지만, 최적화 실행(-O)에서도 확실히 닫는다
        return Result("disabled", "reviewer-not-implemented")

    try:
        payload = event_loader()
    except (OSError, ValueError):
        return Result("failed", "event-unreadable")
    ev = parse_event(payload, repo)
    if isinstance(ev, Result):
        return ev

    gh = gh_factory()
    limit = max_rounds(config) or DEFAULT_MAX_ROUNDS
    try:
        number = resolve_pull(gh, ev, repo)
        if isinstance(number, Result):
            return number
        pull = verify_pull(gh, number, ev.head_sha, repo)
        if isinstance(pull, Result):
            return pull
        stop = verify_ci_run(gh, ev, repo)
        if stop:
            return stop
        stop, done = check_ledger(gh, number, ev.head_sha, repo, limit)
        if stop:
            return stop
        round_no = done + 1
        data = build_review_input(gh, pull, number, ev.head_sha, repo, round_no, limit)
    except GitHubError:
        return Result("failed", "github-api-error")

    def failed(reason: str) -> Result:
        body = render_comment(repo, number, ev.head_sha, ev.run_id, round_no, limit, "failed", None, reason)
        return Result("failed", reason, "failed", number, ev.head_sha, body)

    try:
        raw = reviewer.review(data)
    except TimeoutError:
        return failed("reviewer-timeout")
    except Exception:  # noqa: BLE001 — 리뷰어 오류는 종류와 관계없이 실패로 닫는다. 메시지는 출력하지 않는다
        return failed("reviewer-error")
    outcome = validate_outcome(raw)
    if outcome is None:
        return failed("reviewer-invalid-output")
    if data.incomplete and outcome.result == "no_changes":
        # 일부만 본 리뷰를 "수정 불필요"로 게시하지 않는다 (needs_changes 는 본 부분의 지적이므로 유지)
        return failed("review-input-incomplete")

    # 리뷰하는 동안 새 커밋 push·close 가 있었을 수 있다 (TOCTOU). 오래된 SHA 의 결과는 버린다
    try:
        again = verify_pull(gh, number, ev.head_sha, repo)
    except GitHubError:
        return Result("failed", "github-api-error", pr_number=number, head_sha=ev.head_sha)
    if isinstance(again, Result):
        return Result(again.status, f"{again.reason}-after-review", pr_number=number, head_sha=ev.head_sha)

    body = render_comment(repo, number, ev.head_sha, ev.run_id, round_no, limit, outcome.result, outcome)
    return Result("reviewed", "planned-not-posted", outcome.result, number, ev.head_sha, body)


# --- 실제 GitHub 조회 (켜져 있고 리뷰어가 있을 때만 생성. 지금 main() 에서는 도달하지 않음) ---


class LiveGitHub:
    def __init__(self, token: str, api: str = "https://api.github.com") -> None:
        self._token = token
        self._api = api

    def get(self, path: str) -> object:
        req = urllib.request.Request(
            self._api + path,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=GITHUB_TIMEOUT) as resp:
                return json.loads(resp.read())
        except (OSError, ValueError) as exc:  # HTTPError·URLError·timeout 포함. 메시지는 출력하지 않는다
            raise GitHubError() from exc


def load_event_file(path: str) -> object:
    if not path:
        raise OSError("no event path")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_summary(path: str, result: Result) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"### ai-review\n- result: `{result.status}`\n- reason: `{result.reason}`\n")
        if result.outcome:
            f.write(f"- outcome: `{result.outcome}`\n")
        if result.status == "disabled":
            f.write("- AI 리뷰를 실행하지 않았다 (모델 호출·코멘트 0회). **리뷰 성공 아님**\n")
        elif result.status != "reviewed":
            f.write("- AI 리뷰를 완료하지 않았다. **리뷰 성공 아님**\n")
        f.write("- 이 버전은 PR 코멘트를 게시하지 않는다\n")


def main(env: Mapping[str, str] | None = None) -> int:
    """워크플로 진입점. 실제 리뷰어를 연결하지 않으므로(reviewer=None) 결과는 항상 disabled 다.

    리뷰어를 주입하는 인자를 일부러 두지 않는다: 이 버전의 main() 에서는 GitHub 조회·리뷰가 일어날 수 없다.
    """
    env = os.environ if env is None else env
    result = run_review(
        Config.from_env(env),
        event_loader=lambda: load_event_file(env.get("GITHUB_EVENT_PATH", "")),
        gh_factory=lambda: LiveGitHub(env.get("GITHUB_TOKEN", "")),
        reviewer=None,  # 실제 리뷰어(OpenAI 등)는 사용자 승인 후 별도 PR 에서 연결
    )
    print(result.line())
    summary = env.get("GITHUB_STEP_SUMMARY", "")
    if summary:
        write_summary(summary, result)
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
