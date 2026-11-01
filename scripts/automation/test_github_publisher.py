#!/usr/bin/env python3
"""github_publisher.py 오프라인 테스트. 실행: python3 -B -m unittest discover -s scripts/automation -p 'test_*.py'

- 실제 GitHub 에 코멘트를 쓰지 않는다. 읽기·쓰기는 가짜 객체가 응답하고, urllib·socket 연결은 막아 둔다
- 이 테스트 통과는 실제 PR 코멘트 게시 성공이 아니다
"""

from __future__ import annotations

import io
import json
import unittest
import urllib.error
import urllib.request
from unittest import mock

import ai_review as ar
import github_publisher as gp
import routine_bridge as rb
from test_ai_review import PR, REPO, SHA, OTHER_SHA, FakeGitHub, NoNetwork, bot_comment, finding

COMMENT_URL = f"https://github.com/{REPO}/pull/{PR}#issuecomment-123"


class PublishGitHub(FakeGitHub):
    """FakeGitHub + check-runs 응답."""

    def __init__(self) -> None:
        super().__init__()
        self.check_runs = [{"name": "static-checks", "app": {"slug": "github-actions"}, "head_sha": SHA,
                            "status": "completed", "conclusion": "success"}]

    def get(self, path: str) -> object:
        if path.startswith(f"/repos/{REPO}/commits/{SHA}/check-runs?"):
            self.calls.append(path)
            if self.fail_on and self.fail_on in path:
                raise ar.GitHubError()
            return {"check_runs": self.check_runs}
        return super().get(path)


class FakeWriter:
    def __init__(self, status: int = 201, data: object = None, exc: BaseException | None = None) -> None:
        self.status = status
        self.data = {"html_url": COMMENT_URL} if data is None else data
        self.exc = exc
        self.calls: list[tuple[str, dict]] = []

    def post(self, path: str, body: dict):
        self.calls.append((path, body))
        if self.exc:
            raise self.exc
        return self.status, self.data


def reviewed(outcome: str = "needs_changes", sha: str = SHA) -> ar.Result:
    findings = (finding(),) if outcome == "needs_changes" else ()
    body = ar.render_comment(REPO, PR, sha, 1, 1, 2, outcome, ar.ReviewOutcome(outcome, "s", findings))
    return ar.Result("reviewed", "planned-not-posted", outcome, PR, sha, body)


def failed_result() -> ar.Result:
    body = ar.render_comment(REPO, PR, SHA, 1, 1, 2, "failed", None, "reviewer-timeout")
    return ar.Result("failed", "reviewer-timeout", "failed", PR, SHA, body)


class PublishTest(NoNetwork):
    def publish(self, result=None, gh=None, writer=None, publish=True, limit=ar.DEFAULT_MAX_ROUNDS):
        self.gh = gh or PublishGitHub()
        self.writer = writer or FakeWriter()
        return gp.publish_review(result or reviewed(), self.gh, self.writer, publish=publish, limit=limit)

    def test_default_is_dry_run_with_zero_calls(self) -> None:
        gh, writer = PublishGitHub(), FakeWriter()
        r = gp.publish_review(reviewed(), gh, writer)  # publish 기본값
        self.assertEqual((r.status, r.reason), ("dry_run", "publish-disabled"))
        self.assertEqual((gh.calls, writer.calls), ([], []))

    def test_posts_exact_planned_comment(self) -> None:
        for result in (reviewed("needs_changes"), reviewed("no_changes"), failed_result()):
            with self.subTest(outcome=result.outcome):
                r = self.publish(result)
                self.assertEqual((r.status, r.comment_url), ("posted", COMMENT_URL))
                self.assertEqual(self.writer.calls, [(f"/repos/{REPO}/issues/{PR}/comments",
                                                      {"body": result.planned_comment})])

    def test_posted_comment_is_accepted_by_bridge_only_for_needs_changes(self) -> None:
        for outcome, expected in (("needs_changes", None), ("no_changes", "review-no-changes")):
            body = reviewed(outcome).planned_comment

            class BridgeGH:
                def get(self, path):
                    return {"issue_url": f"https://api.github.com/repos/{REPO}/issues/{PR}",
                            "user": {"login": "github-actions[bot]", "type": "Bot"}, "body": body}

            verdict = rb.verify_review(BridgeGH(), rb.Request(PR, SHA, f"https://github.com/{REPO}/pull/{PR}#issuecomment-1", 1), REPO)
            self.assertEqual(verdict.reason if verdict else None, expected)

    def test_not_publishable(self) -> None:
        cases = [
            ar.Result("disabled", "flag-off"),
            ar.Result("blocked", "duplicate-sha"),
            ar.Result("failed", "github-api-error"),  # 리뷰어까지 가지 않은 실패: 게시할 내용 없음
            ar.Result("reviewed", "planned-not-posted", "needs_changes", PR, SHA, ""),
        ]
        for result in cases:
            with self.subTest(result=result.reason):
                r = self.publish(result)
                self.assertEqual(r.status, "skipped")
                self.assertEqual(self.writer.calls, [])
                self.assertEqual(self.gh.calls, [])

    def test_marker_mismatch_blocked(self) -> None:
        good = reviewed()
        cases = [
            ar.Result("reviewed", "x", "needs_changes", PR, SHA, good.planned_comment.replace(SHA, OTHER_SHA)),
            ar.Result("reviewed", "x", "no_changes", PR, SHA, good.planned_comment),  # outcome 불일치
            ar.Result("reviewed", "x", "needs_changes", PR, SHA, good.planned_comment + "\n" + good.planned_comment),
        ]
        for result in cases:
            with self.subTest():
                r = self.publish(result)
                self.assertEqual((r.status, r.reason), ("blocked", "marker-mismatch"))
                self.assertEqual(self.writer.calls, [])

    def test_recheck_before_post(self) -> None:
        def stale(g):
            g.pull["head"]["sha"] = OTHER_SHA

        def closed(g):
            g.pull["state"] = "closed"

        def ci_failed(g):
            g.check_runs[0]["conclusion"] = "failure"

        def ci_missing(g):
            g.check_runs = [dict(g.check_runs[0], app={"slug": "evil-app"})]

        for change, reason in ((stale, "stale-head"), (closed, "pr-not-open"),
                               (ci_failed, "ci-not-success"), (ci_missing, "ci-missing")):
            with self.subTest(reason=reason):
                gh = PublishGitHub()
                change(gh)
                r = self.publish(gh=gh)
                self.assertEqual((r.status, r.reason), ("blocked", reason))
                self.assertEqual(self.writer.calls, [])

    def test_duplicate_and_round_limit(self) -> None:
        gh = PublishGitHub()
        gh.comments = [bot_comment(SHA)]
        r = self.publish(gh=gh)
        self.assertEqual((r.status, r.reason), ("skipped", "already-posted"))
        self.assertEqual(self.writer.calls, [])
        gh = PublishGitHub()
        gh.comments = [bot_comment("1" * 40), bot_comment("2" * 40)]
        r = self.publish(gh=gh)
        self.assertEqual((r.status, r.reason), ("blocked", "round-limit"))
        self.assertEqual(self.writer.calls, [])

    def test_read_errors_fail_closed(self) -> None:
        for part in (f"/pulls/{PR}", "/check-runs?", "/comments?"):
            with self.subTest(part=part):
                gh = PublishGitHub()
                gh.fail_on = part
                r = self.publish(gh=gh)
                self.assertEqual((r.status, r.reason), ("failed", "github-api-error"))
                self.assertEqual(self.writer.calls, [])

    def test_write_errors(self) -> None:
        cases = {
            "http-403-permission": FakeWriter(status=403, data=None),
            "http-404-not-found": FakeWriter(status=404),
            "http-422-validation": FakeWriter(status=422),
            "http-429-rate-limited": FakeWriter(status=429),
            "http-502-server-error": FakeWriter(status=502),
            "http-200-unexpected": FakeWriter(status=200),
            "posted-unexpected-response": FakeWriter(status=201, data={"html_url": "https://evil.example/x"}),
            "timeout-unknown-state": FakeWriter(exc=TimeoutError()),
            "network-error": FakeWriter(exc=ConnectionResetError()),
        }
        for reason, writer in cases.items():
            with self.subTest(reason=reason):
                r = self.publish(writer=writer)
                self.assertEqual((r.status, r.reason), ("failed", reason))
                self.assertEqual(len(writer.calls), 1)  # 재시도 없음


class LiveWriterTest(NoNetwork):
    """실제 urllib 쓰기 어댑터. urlopen 을 가짜로 바꿔 확인한다 (네트워크 없음)."""

    def test_request_shape_and_success(self) -> None:
        class Resp(io.BytesIO):
            status = 201

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        seen = []

        def fake(req, timeout):
            seen.append((req, timeout))
            return Resp(json.dumps({"html_url": COMMENT_URL}).encode())

        with mock.patch.object(urllib.request, "urlopen", side_effect=fake):
            status, data = gp.LiveGitHubWriter("fake-token").post("/repos/x/y/issues/1/comments", {"body": "b"})
        self.assertEqual((status, data), (201, {"html_url": COMMENT_URL}))
        req, timeout = seen[0]
        self.assertEqual((req.get_method(), req.full_url, timeout),
                         ("POST", "https://api.github.com/repos/x/y/issues/1/comments", gp.WRITE_TIMEOUT))
        self.assertEqual(json.loads(req.data), {"body": "b"})

    def test_errors(self) -> None:
        with mock.patch.object(urllib.request, "urlopen",
                               side_effect=urllib.error.HTTPError("u", 403, "Forbidden", {}, None)):
            self.assertEqual(gp.LiveGitHubWriter("t").post("/x", {}), (403, None))
        with mock.patch.object(urllib.request, "urlopen", side_effect=urllib.error.URLError(TimeoutError())):
            with self.assertRaises(TimeoutError):
                gp.LiveGitHubWriter("t").post("/x", {})


if __name__ == "__main__":
    unittest.main()
