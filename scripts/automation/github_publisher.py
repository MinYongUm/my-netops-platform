#!/usr/bin/env python3
"""github_publisher.py — ai_review 결과(planned_comment)를 PR 코멘트로 게시하는 어댑터 (Issue #5 Phase 2-B).

**이 파일은 아직 어디에도 연결되어 있지 않다.** ai_review.main() 과 워크플로는 이 모듈을 호출하지 않고,
워크플로에는 pull-requests: write 권한이 없다. 기본값 publish=False 이면 GitHub 조회·쓰기를 한 번도 하지 않는다.
테스트는 GitHub 읽기·쓰기를 가짜 객체로 바꿔 오프라인으로만 실행한다.

publish=True 일 때의 순서 (모두 fail-closed):
  1. 게시할 수 있는 결과인지: reviewed(needs_changes|no_changes) 또는 리뷰어 실패(failed + result=failed 마커)
     본문의 ai-review 마커가 정확히 1개이고 결과의 head SHA·outcome 과 같은지
  2. PR 재조회: open·base main·fork 아님·작성자=소유자·head SHA 일치 (ai_review.verify_pull)
  3. CI 재조회: 그 SHA 의 static-checks(github-actions) check run 이 모두 success
  4. 기록 재확인: 같은 SHA 마커 중복 금지, PR 당 상한 (ai_review.check_ledger)
  5. POST /repos/{repo}/issues/{pr}/comments 1회. 재시도하지 않는다
     시간 초과는 게시 여부를 알 수 없으므로 failed(timeout-unknown-state). 다음 실행의 4번이 중복을 막는다

향후 활성화 설계 (이 PR 에서는 하지 않음): 리뷰(모델 키 보유) job 과 분리된 게시 job 에서만
pull-requests: write 를 주고, 게시 job 은 PR 코드·모델 키 없이 이 모듈만 실행한다. docs/AUTOMATION.md 참고.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Protocol

import ai_review as ar

CI_CHECK_NAME = "static-checks"
CI_APP_SLUG = "github-actions"
WRITE_TIMEOUT = 15
COMMENT_URL_RE = re.compile(r"^https://github\.com/[\w.-]+/[\w.-]+/pull/[1-9][0-9]*#issuecomment-[1-9][0-9]*$")


@dataclass(frozen=True)
class PublishResult:
    status: str  # dry_run | posted | skipped | blocked | failed
    reason: str
    comment_url: str = ""

    def line(self) -> str:
        out = f"ai-review-publish result={self.status} reason={self.reason}"
        return out + (f" url={self.comment_url}" if self.comment_url else "")


class Writer(Protocol):
    def post(self, path: str, body: dict) -> tuple[int, object]: ...


# --- 게시 가능 여부 ---


def publishable(result: ar.Result) -> PublishResult | None:
    if result.status == "reviewed" and result.outcome in ("needs_changes", "no_changes"):
        expected = result.outcome
    elif result.status == "failed" and result.outcome == "failed":
        expected = "failed"
    else:
        return PublishResult("skipped", f"not-publishable:{result.status}")
    if not result.planned_comment or not ar.SHA_RE.fullmatch(result.head_sha) or result.pr_number <= 0:
        return PublishResult("skipped", "no-planned-comment")
    markers = ar.MARKER_RE.findall(result.planned_comment)
    if markers != [(result.head_sha, expected)]:
        return PublishResult("blocked", "marker-mismatch")
    return None


def verify_ci_checks(gh: ar.GitHub, head_sha: str, repo: str) -> PublishResult | None:
    query = urllib.parse.urlencode({"check_name": CI_CHECK_NAME, "filter": "latest"})
    data = gh.get(f"/repos/{repo}/commits/{head_sha}/check-runs?{query}")
    runs = data.get("check_runs") if isinstance(data, dict) else None
    if not isinstance(runs, list):
        return PublishResult("failed", "github-unexpected-response")
    ours = [
        r for r in runs
        if isinstance(r, dict)
        and r.get("name") == CI_CHECK_NAME
        and (r.get("app") or {}).get("slug") == CI_APP_SLUG
        and r.get("head_sha") == head_sha
    ]
    if not ours:
        return PublishResult("blocked", "ci-missing")
    if any(r.get("status") != "completed" or r.get("conclusion") != "success" for r in ours):
        return PublishResult("blocked", "ci-not-success")
    return None


def classify_write(status: int, data: object) -> PublishResult:
    if status == 201:
        url = data.get("html_url") if isinstance(data, dict) else None
        if isinstance(url, str) and COMMENT_URL_RE.fullmatch(url):
            return PublishResult("posted", "created", url)
        return PublishResult("failed", "posted-unexpected-response")  # 게시는 됐을 수 있다 → 다음 실행의 기록 검사가 막는다
    names = {401: "auth", 403: "permission", 404: "not-found", 410: "gone", 422: "validation", 429: "rate-limited"}
    if status in names:
        return PublishResult("failed", f"http-{status}-{names[status]}")
    if 500 <= status <= 599:
        return PublishResult("failed", f"http-{status}-server-error")
    return PublishResult("failed", f"http-{status}-unexpected")


# --- 게시 ---


def publish_review(
    result: ar.Result,
    gh: ar.GitHub,
    writer: Writer,
    publish: bool = False,
    repo: str = ar.TRUSTED_REPO,
    limit: int = ar.DEFAULT_MAX_ROUNDS,
) -> PublishResult:
    stop = publishable(result)
    if stop:
        return stop
    if not publish:
        return PublishResult("dry_run", "publish-disabled")  # 조회·쓰기 0회

    try:
        pull = ar.verify_pull(gh, result.pr_number, result.head_sha, repo)
        if isinstance(pull, ar.Result):
            return PublishResult(pull.status, pull.reason)
        stop = verify_ci_checks(gh, result.head_sha, repo)
        if stop:
            return stop
        ledger_stop, _ = ar.check_ledger(gh, result.pr_number, result.head_sha, repo, limit)
        if ledger_stop:
            reason = "already-posted" if ledger_stop.reason == "duplicate-sha" else ledger_stop.reason
            return PublishResult("skipped" if reason == "already-posted" else ledger_stop.status, reason)
    except ar.GitHubError:
        return PublishResult("failed", "github-api-error")

    try:
        status, data = writer.post(f"/repos/{repo}/issues/{result.pr_number}/comments",
                                   {"body": result.planned_comment})
    except TimeoutError:
        return PublishResult("failed", "timeout-unknown-state")
    except OSError:
        return PublishResult("failed", "network-error")
    return classify_write(status, data)


class LiveGitHubWriter:
    """실제 POST. 이 PR 에서는 어디에서도 만들지 않는다 (연결은 사용자 승인 후)."""

    def __init__(self, token: str, api: str = "https://api.github.com") -> None:
        self._token = token
        self._api = api

    def post(self, path: str, body: dict) -> tuple[int, object]:
        req = urllib.request.Request(
            self._api + path,
            data=json.dumps(body).encode(),
            method="POST",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=WRITE_TIMEOUT) as resp:
                raw = resp.read()
                try:
                    return resp.status, json.loads(raw)
                except ValueError:
                    return resp.status, None
        except urllib.error.HTTPError as exc:
            return exc.code, None  # 오류 본문은 읽지 않는다
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise TimeoutError() from None
            raise OSError() from None
