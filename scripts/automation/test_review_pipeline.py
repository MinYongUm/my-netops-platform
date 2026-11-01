#!/usr/bin/env python3
"""review_pipeline.py 통합 테스트 (오프라인). 실행: python3 -B -m unittest discover -s scripts/automation -p 'test_*.py'

prepare → review → publish 를 가짜 GitHub·가짜 OpenAI HTTP·가짜 게시 writer 로 끝까지 흘린다.
- 실제 네트워크를 쓰지 않는다 (urllib·socket 연결을 막아 둔다)
- 이 테스트 통과는 실제 AI 리뷰·PR 코멘트 게시·Claude 자동 수정 성공이 아니다
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import ai_review as ar
import review_pipeline as rp
import routine_bridge as rb
from test_ai_review import INJECTION, ISSUE, OTHER_SHA, POLICY, PR, REPO, RUN_ID, SHA, NoNetwork, event
from test_github_publisher import COMMENT_URL, FakeWriter, PublishGitHub
from test_openai_reviewer import FakeTransport, response, review_doc
from test_routine_bridge import FakeTransport as FireTransport

CURRENT_RUN = 9_000_000_001
WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ai-review.yml"
FAKE_KEY = "fake-openai-key-for-tests-only"

GATE_ENV = {"AI_REVIEW_ENABLED": "true", "GITHUB_TOKEN": "fake-gh", "GITHUB_RUN_ID": str(CURRENT_RUN),
            "GITHUB_RUN_ATTEMPT": "1"}
MODEL_ENV = {**GATE_ENV, "OPENAI_API_KEY": FAKE_KEY, "AI_REVIEW_MODEL": "gpt-4.1-mini"}


class PipelineGitHub(PublishGitHub):
    """PublishGitHub + ai-review 워크플로 실행 기록."""

    def __init__(self) -> None:
        super().__init__()
        self.ai_runs: list[dict] = []
        self.ai_jobs: dict[int, list[dict]] = {}
        # 지금 실행 중인 ai-review run (run-name 이 이 PR·SHA 로 기록되는지 확인용)
        self.current_run = {"id": CURRENT_RUN, "display_title": f"ai-review pr={PR} head={SHA}", "run_attempt": 1}

    def add_attempt(self, run_id: int, sha: str, pr: int = PR, step_status: str = "completed",
                    conclusion: str | None = "success", title: str | None = None) -> None:
        self.ai_runs.append({"id": run_id, "display_title": title or f"ai-review pr={pr} head={sha}"})
        self.ai_jobs[run_id] = [{"name": "review", "steps": [
            {"name": "Prepare (gates, no model key)", "status": "completed", "conclusion": "success"},
            {"name": "Model review", "status": step_status, "conclusion": conclusion},
        ]}]

    def get(self, path: str) -> object:
        if path == f"/repos/{REPO}/actions/runs/{CURRENT_RUN}":
            self.calls.append(path)
            if self.fail_on and self.fail_on in path:
                raise ar.GitHubError()
            return self.current_run
        if path.startswith(f"/repos/{REPO}/actions/workflows/ai-review.yml/runs?"):
            self.calls.append(path)
            if self.fail_on and self.fail_on in path:
                raise ar.GitHubError()
            page = int(path.rsplit("page=", 1)[1])
            start = (page - 1) * rp.RUNS_PER_PAGE
            return {"workflow_runs": self.ai_runs[start:start + rp.RUNS_PER_PAGE]}
        if path.startswith(f"/repos/{REPO}/actions/runs/") and "/jobs?" in path:
            self.calls.append(path)
            if self.fail_on and self.fail_on in path:
                raise ar.GitHubError()
            self.assertion_filter_all = "filter=all" in path
            run_id = int(path.split("/jobs?", 1)[0].rsplit("/", 1)[1])
            return {"jobs": self.ai_jobs.get(run_id, [])}
        return super().get(path)


class PipelineCase(NoNetwork):
    def setUp(self) -> None:
        super().setUp()
        self.gh = PipelineGitHub()
        self.gh_created = 0

    def factory(self):
        self.gh_created += 1
        return self.gh

    def prepare(self, env=None, payload=None, policy=None):
        body = event() if payload is None else payload
        loaded = (POLICY, ()) if policy is None else policy
        return rp.stage_prepare(GATE_ENV if env is None else env, lambda: body, self.factory, lambda: loaded)

    def review(self, handoff, env=None, transport=None):
        self.transport = transport or FakeTransport()
        return rp.stage_review(MODEL_ENV if env is None else env, handoff, self.factory, self.transport)

    def publish(self, result_text, env=None, writer=None):
        self.writer = writer or FakeWriter()
        return rp.stage_publish({**MODEL_ENV, "AI_REVIEW_PUBLISH": "true"} if env is None else env,
                                result_text, self.factory, lambda: self.writer)

    def full(self, transport=None):
        prep, handoff = self.prepare()
        self.assertEqual(prep.status, "ready", prep.reason)
        result, out = self.review(handoff, transport=transport)
        return result, out


# --- 끝까지 (오프라인 E2E) ---


class EndToEndTest(PipelineCase):
    def test_needs_changes_flows_to_published_comment_accepted_by_bridge(self) -> None:
        result, out = self.full()
        self.assertEqual((result.status, result.outcome), ("reviewed", "needs_changes"))
        pub = self.publish(out)
        self.assertEqual((pub.status, pub.comment_url), ("posted", COMMENT_URL))
        body = self.writer.calls[0][1]["body"]
        self.assertEqual(body, result.planned_comment)
        self.assertEqual(rb.REVIEW_MARKER_RE.findall(body), [(SHA, "needs_changes")])
        self.assertEqual(len(self.transport.calls), 1)

    def test_no_changes_and_failed_flow(self) -> None:
        cases = {
            "no_changes": FakeTransport(body=response(json.dumps(review_doc("no_changes")))),
            "failed": FakeTransport(status=429, body=b""),
        }
        for outcome, transport in cases.items():
            with self.subTest(outcome=outcome):
                self.gh = PipelineGitHub()
                result, out = self.full(transport)
                self.assertEqual(result.outcome, outcome)
                pub = self.publish(out)
                self.assertEqual(pub.status, "posted")
                self.assertEqual(rb.REVIEW_MARKER_RE.findall(self.writer.calls[0][1]["body"]), [(SHA, outcome)])

    def test_default_publish_is_dry_run_with_zero_calls(self) -> None:
        _, out = self.full()
        calls_before = len(self.gh.calls)
        created_before = self.gh_created
        pub = self.publish(out, env=MODEL_ENV)  # AI_REVIEW_PUBLISH 없음
        self.assertEqual((pub.status, pub.reason), ("dry_run", "publish-disabled"))
        self.assertEqual((self.writer.calls, len(self.gh.calls), self.gh_created),
                         ([], calls_before, created_before))

    def test_handoff_round_trip_preserves_trust_separation(self) -> None:
        _, handoff = self.prepare()
        prepared = rp.load_prepared(handoff)
        self.assertEqual(prepared.data.trusted_policy, POLICY)
        self.assertEqual(prepared.data.issue_number, ISSUE)
        self.assertIn("Ignore all previous instructions", prepared.data.untrusted_body)
        self.assertEqual((prepared.run_id, prepared.round_no, prepared.limit), (RUN_ID, 1, rp.PILOT_MAX_MODEL_CALLS))
        self.review(handoff)
        payload = json.loads(self.transport.request["input"][0]["content"][0]["text"])
        self.assertNotIn("Ignore all previous instructions", json.dumps(payload["trusted"]))


# --- 완전 OFF ---


class OffTest(PipelineCase):
    def test_everything_off_by_default(self) -> None:
        prep, handoff = self.prepare(env={})
        self.assertEqual((prep.status, prep.reason, handoff), ("disabled", "flag-off", ""))
        result, out = self.review("", env={})
        self.assertEqual((result.status, out), ("disabled", ""))
        self.assertEqual((self.gh_created, self.transport.calls), (0, []))

    def test_workflow_equivalent_env_has_no_model_call(self) -> None:
        """현재 ai-review.yml 과 같은 조건: 변수는 켜도 OPENAI_API_KEY 미연결."""
        prep, handoff = self.prepare()
        self.assertEqual(prep.status, "ready")
        created = self.gh_created
        result, _ = self.review(handoff, env={**GATE_ENV, "AI_REVIEW_MODEL": "gpt-4.1"})
        self.assertEqual((result.status, result.reason), ("disabled", "no-api-key"))
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(self.gh_created, created)  # review 단계는 GitHub 도 만들지 않음

    def test_review_config_errors_before_any_call(self) -> None:
        for over, reason in (({"AI_REVIEW_MODEL": ""}, "config-invalid:config-model-missing"),
                             ({"AI_REVIEW_MODEL": "gpt-3.5-turbo"}, "config-invalid:config-model-not-allowed")):
            with self.subTest(reason=reason):
                result, _ = self.review('{"x":1}', env={**MODEL_ENV, **over})
                self.assertEqual((result.status, result.reason), ("failed", reason))
                self.assertEqual(self.transport.calls, [])

    def test_prepare_needs_run_id_when_enabled(self) -> None:
        env = dict(GATE_ENV)
        del env["GITHUB_RUN_ID"]
        prep, _ = self.prepare(env=env)
        self.assertEqual((prep.status, prep.reason), ("failed", "no-run-id"))
        self.assertEqual(self.gh_created, 0)


# --- 신뢰된 CI 출처·보호 경로 ---


class TrustBoundaryTest(PipelineCase):
    def blocked(self, reason: str) -> None:
        prep, handoff = self.prepare()
        self.assertEqual((prep.status, prep.reason, handoff), ("blocked", reason, ""))

    def test_protected_paths_require_manual_review(self) -> None:
        for path in (".github/workflows/ci.yml", ".github/workflows/new.yml", "scripts/ci/static-checks.sh",
                     "scripts/automation/ai_review.py"):
            with self.subTest(path=path):
                self.gh = PipelineGitHub()
                self.gh.files = [self.gh.files[0], {"filename": path, "status": "modified", "patch": "+x"}]
                self.blocked("manual-review-required:protected-path")

    def test_rename_out_of_protected_path(self) -> None:
        self.gh.files = [{"filename": "docs/x.sh", "previous_filename": "scripts/ci/x.sh", "status": "renamed"}]
        self.blocked("manual-review-required:protected-path")

    def test_protected_path_on_later_page(self) -> None:
        filler = [{"filename": f"docs/f{i}.md", "status": "added", "patch": "+"} for i in range(ar.FILES_PER_PAGE)]
        self.gh.files = filler + [{"filename": ".github/workflows/evil.yml", "status": "added", "patch": "+"}]
        self.blocked("manual-review-required:protected-path")

    def test_too_many_files_unverifiable(self) -> None:
        self.gh.files = [{"filename": f"docs/f{i}.md", "status": "added", "patch": "+"}
                         for i in range(ar.FILES_PER_PAGE * ar.PROTECTED_FILES_MAX_PAGES)]
        self.blocked("files-unverifiable")

    def test_ci_run_must_be_trusted_workflow_and_linked(self) -> None:
        self.gh.run["workflow_id"] = 111
        self.blocked("ci-workflow-not-trusted")
        self.gh = PipelineGitHub()
        self.gh.run["pull_requests"] = [{"number": 999}]
        self.blocked("ci-run-pr-unlinked")
        self.gh = PipelineGitHub()
        self.gh.run["pull_requests"] = []
        self.blocked("ci-run-pr-unlinked")

    def test_workflow_lookup_unexpected(self) -> None:
        self.gh.workflow = {"id": 1, "path": ".github/workflows/other.yml"}
        prep, _ = self.prepare()
        self.assertEqual((prep.status, prep.reason), ("failed", "github-unexpected-response"))

    def test_publish_rechecks_ci_and_paths(self) -> None:
        _, out = self.full()

        def ci_rerun_failed(g):
            g.run["conclusion"] = "failure"

        def protected_added(g):
            g.files.append({"filename": ".github/workflows/ci.yml", "status": "modified", "patch": "+"})

        def workflow_swapped(g):
            g.run["workflow_id"] = 7

        for change, reason in ((ci_rerun_failed, "ci-not-success"), (protected_added, "manual-review-required:protected-path"),
                               (workflow_swapped, "ci-workflow-not-trusted")):
            with self.subTest(reason=reason):
                change(self.gh)
                pub = self.publish(out)
                self.assertEqual((pub.status, pub.reason), ("blocked", reason))
                self.assertEqual(self.writer.calls, [])
                self.gh = PipelineGitHub()


# --- 실행 기록 상한 (삭제할 수 없는 기록) ---


class RunLedgerTest(PipelineCase):
    def check(self, status: str, reason: str) -> None:
        prep, handoff = self.prepare()
        self.assertEqual((prep.status, prep.reason), (status, reason))
        self.assertEqual(handoff, "")

    def test_same_sha_attempt_blocks_even_without_comment(self) -> None:
        self.gh.add_attempt(1, SHA)  # 리뷰 코멘트는 지워졌다 (gh.comments 비어 있음)
        self.check("blocked", "duplicate-sha-run")

    def test_failed_timeout_cancelled_and_running_attempts_count(self) -> None:
        for status, conclusion in (("completed", "failure"), ("completed", "timed_out"),
                                   ("completed", "cancelled"), ("in_progress", None)):
            with self.subTest(conclusion=conclusion):
                self.gh = PipelineGitHub()
                self.gh.add_attempt(1, SHA, step_status=status, conclusion=conclusion)
                self.check("blocked", "duplicate-sha-run")

    def test_skipped_or_not_started_model_step_does_not_count(self) -> None:
        self.gh.add_attempt(1, SHA, conclusion="skipped")
        self.gh.add_attempt(2, SHA, step_status="queued", conclusion=None)
        prep, _ = self.prepare()
        self.assertEqual(prep.status, "ready")

    def test_round_limit_from_runs_is_pilot_one(self) -> None:
        """시범: 다른 SHA 라도 이전 모델 호출이 1회 있으면 막는다. AI_REVIEW_MAX_ROUNDS 로 늘릴 수 없다."""
        self.gh.add_attempt(1, "1" * 40)
        self.check("blocked", "round-limit-run")
        for raw in ("2", "3"):
            with self.subTest(max_rounds=raw):
                prep, _ = self.prepare(env={**GATE_ENV, "AI_REVIEW_MAX_ROUNDS": raw})
                self.assertEqual((prep.status, prep.reason), ("blocked", "round-limit-run"))

    def test_current_run_other_pr_and_bad_titles_ignored(self) -> None:
        self.gh.add_attempt(CURRENT_RUN, SHA)  # 지금 실행 중인 자신
        self.gh.add_attempt(3, SHA, pr=PR + 1)  # 다른 PR
        self.gh.add_attempt(4, SHA, title=f"ai-review pr= head={SHA}")
        self.gh.add_attempt(5, SHA, title=f"ai-review pr={PR} head={SHA} {INJECTION}")
        prep, _ = self.prepare()
        self.assertEqual(prep.status, "ready")

    def test_incomplete_or_broken_ledger_fails_closed(self) -> None:
        self.gh.ai_runs = [{"id": 10 + i, "display_title": "other"} for i in range(rp.RUNS_PER_PAGE * rp.RUNS_MAX_PAGES)]
        self.check("failed", "run-ledger-incomplete")
        self.gh = PipelineGitHub()
        for i in range(rp.RUNS_MAX_CANDIDATES + 1):
            self.gh.add_attempt(100 + i, f"{i:040x}", conclusion="skipped")
        self.check("failed", "run-ledger-incomplete")
        for part in ("/ai-review.yml/runs?", "/jobs"):
            with self.subTest(part=part):
                self.gh = PipelineGitHub()
                self.gh.add_attempt(1, OTHER_SHA)
                self.gh.fail_on = part
                self.check("failed", "github-api-error")

    def test_comment_ledger_still_applies(self) -> None:
        from test_ai_review import bot_comment

        self.gh.comments = [bot_comment(SHA)]
        self.check("blocked", "duplicate-sha")

    def test_retry_zero_one_call_even_on_429(self) -> None:
        result, _ = self.full(FakeTransport(status=429, body=b""))
        self.assertEqual(result.reason, "reviewer-error:http-429-rate-limited")
        self.assertEqual(len(self.transport.calls), 1)


# --- 재실행·PR 식별자 (PR #16 리뷰) ---


class RerunAndIdentityTest(PipelineCase):
    """재실행·PR 식별자 누락으로 실행 기록이 빠지는 경로는 모델 호출 전에 막는다."""

    def test_rerun_blocked_before_event_and_github(self) -> None:
        for raw, status, reason in (("2", "blocked", "rerun-not-allowed"), ("17", "blocked", "rerun-not-allowed"),
                                    ("", "failed", "run-attempt-invalid"), ("01", "failed", "run-attempt-invalid"),
                                    ("abc", "failed", "run-attempt-invalid"), (" 1", "failed", "run-attempt-invalid")):
            with self.subTest(attempt=raw):
                loaded = []
                env = {**GATE_ENV, "GITHUB_RUN_ATTEMPT": raw}
                result, handoff = rp.stage_prepare(env, lambda: loaded.append(1) or event(), self.factory,
                                                   lambda: (POLICY, ()))
                self.assertEqual((result.status, result.reason, handoff), (status, reason, ""))
                self.assertEqual((loaded, self.gh_created), ([], 0))
        env = dict(GATE_ENV)
        del env["GITHUB_RUN_ATTEMPT"]
        self.assertEqual(self.prepare(env=env)[0].reason, "run-attempt-invalid")

    def test_rerun_blocked_at_review_stage_before_model(self) -> None:
        _, handoff = self.prepare()
        created = self.gh_created
        for raw, reason in (("2", "rerun-not-allowed"), ("", "run-attempt-invalid")):
            with self.subTest(attempt=raw):
                result, out = self.review(handoff, env={**MODEL_ENV, "GITHUB_RUN_ATTEMPT": raw})
                self.assertEqual((result.reason, out), (reason, ""))
                self.assertEqual(self.transport.calls, [])
                self.assertEqual(self.gh_created, created)

    def test_rerun_of_run_whose_first_attempt_called_model(self) -> None:
        """리뷰 지적 시나리오: 첫 시도에서 모델이 호출된 같은 run 을 재실행 → 모델 호출 0회."""
        self.gh.current_run["run_attempt"] = 2
        self.gh.ai_jobs[CURRENT_RUN] = [{"name": "review", "steps": [
            {"name": "Model review", "status": "completed", "conclusion": "failure"}]}]
        result, _ = self.prepare(env={**GATE_ENV, "GITHUB_RUN_ATTEMPT": "2"})
        self.assertEqual(result.reason, "rerun-not-allowed")
        # 환경 변수가 1 이라고 주장해도 API 의 run_attempt 가 2 면 막는다
        result, handoff = self.prepare()
        self.assertEqual((result.status, result.reason, handoff), ("blocked", "rerun-not-allowed", ""))

    def test_first_attempt_still_works(self) -> None:
        result, out = self.full()
        self.assertEqual(result.status, "reviewed")
        self.assertEqual(len(self.transport.calls), 1)

    def test_event_pr_identifier_must_be_exactly_the_resolved_pr(self) -> None:
        """이벤트 PR 목록이 정확히 [확정된 PR] 이 아니면 run-name 에 그 PR 이 기록된다고 볼 수 없다."""
        cases = {
            "empty": [],  # API 로는 PR 을 찾을 수 있지만 추측하지 않는다
            "extra-after": [{"number": PR}, {"number": PR + 1}],
            "extra-before": [{"number": PR + 1}, {"number": PR}],  # run-name 은 [0] = 다른 PR
        }
        for name, prs in cases.items():
            with self.subTest(case=name):
                self.gh = PipelineGitHub()  # API 로 확정되는 PR 은 PR 하나뿐
                result, handoff = self.prepare(payload=event(pull_requests=prs))
                self.assertEqual((result.status, result.reason, handoff), ("blocked", "run-name-pr-untracked", ""))

    def test_current_run_name_must_record_pr_and_sha(self) -> None:
        for title in (f"ai-review pr= head={SHA}", f"ai-review pr={PR + 1} head={SHA}",
                      f"ai-review pr={PR} head={OTHER_SHA}", "ai-review", None):
            with self.subTest(title=title):
                self.gh = PipelineGitHub()
                self.gh.current_run["display_title"] = title
                result, handoff = self.prepare()
                self.assertEqual((result.status, result.reason, handoff), ("blocked", "run-name-pr-untracked", ""))

    def test_current_run_lookup_errors_fail_closed(self) -> None:
        self.gh.fail_on = f"/actions/runs/{CURRENT_RUN}"
        self.assertEqual(self.prepare()[0].reason, "github-api-error")
        self.gh = PipelineGitHub()
        self.gh.current_run = ["not", "a", "dict"]
        self.assertEqual(self.prepare()[0].reason, "github-unexpected-response")

    def test_off_still_wins(self) -> None:
        result, _ = self.prepare(env={"GITHUB_RUN_ATTEMPT": "2"})
        self.assertEqual((result.status, result.reason), ("disabled", "flag-off"))


# --- 시범 운영 제한 (Phase 2-D) ---

FIRE_ENV = {**GATE_ENV, "ROUTINE_BRIDGE_ENABLED": "true", "CLAUDE_ROUTINE_TOKEN": "fake-routine-token-for-tests-only",
            "CLAUDE_ROUTINE_TRIGGER_ID": "trig_01TESTONLY"}


class BridgeGitHub:
    """routine_bridge 가 보는 GitHub. PR·check-run 은 PipelineGitHub 에, 게시된 코멘트는 writer 기록에서 응답한다."""

    def __init__(self, gh: PipelineGitHub, posted_body: str, comment_id: int = 123) -> None:
        self.gh = gh
        self.body = posted_body
        self.comment_id = comment_id
        self.calls: list[str] = []

    def get(self, path: str) -> object:
        self.calls.append(path)
        if path == f"/repos/{REPO}/issues/comments/{self.comment_id}":
            return {"issue_url": f"https://api.github.com/repos/{REPO}/issues/{PR}",
                    "user": {"login": "github-actions[bot]", "type": "Bot"}, "body": self.body}
        if path.startswith(f"/repos/{REPO}/actions/workflows/routine-bridge.yml/runs?"):
            return {"workflow_runs": []}
        return self.gh.get(path)


class PilotTest(PipelineCase):
    def test_constants(self) -> None:
        self.assertEqual((rp.PILOT_PR_NUMBERS, rp.PILOT_MAX_MODEL_CALLS, rp.PILOT_MAX_COMMENTS, rp.PILOT_MAX_TRIGGERS),
                         ((10,), 1, 1, 1))
        self.assertEqual(PR, 10)

    def test_non_pilot_pr_blocked_before_ledgers_and_model(self) -> None:
        from unittest import mock

        with mock.patch.object(rp, "PILOT_PR_NUMBERS", (99,)):  # 이 테스트에서는 PR 10 이 시범 대상이 아님
            result, handoff = self.prepare()
        self.assertEqual((result.status, result.reason, handoff), ("blocked", "pr-not-in-pilot", ""))
        self.assertFalse(any("ai-review.yml/runs" in c for c in self.gh.calls))  # 실행 기록 조회 전에 멈춤
        self.assertFalse(any(f"/actions/runs/{CURRENT_RUN}" in c for c in self.gh.calls))

    def test_review_stage_rechecks_pilot_and_limit(self) -> None:
        _, handoff = self.prepare()
        doc = json.loads(handoff)
        for tampered in (dict(doc, input=dict(doc["input"], pr_number=PR + 1)),
                         dict(doc, limit=2, input=dict(doc["input"], max_rounds=2))):
            with self.subTest():
                result, out = self.review(json.dumps(tampered))
                self.assertEqual((result.status, result.reason, out), ("blocked", "pr-not-in-pilot", ""))
                self.assertEqual(self.transport.calls, [])

    def test_publish_stage_pilot_attempt_and_run_id(self) -> None:
        _, out = self.full()
        doc = json.loads(out)
        other = json.dumps(dict(doc, pr_number=PR + 1, planned_comment=doc["planned_comment"].replace(f"#{PR},", f"#{PR + 1},")))
        pub = self.publish(other)
        self.assertEqual((pub.status, pub.reason), ("blocked", "pr-not-in-pilot"))
        pub_env = {**MODEL_ENV, "AI_REVIEW_PUBLISH": "true"}
        self.assertEqual(self.publish(out, env={**pub_env, "GITHUB_RUN_ATTEMPT": "2"}).reason, "rerun-not-allowed")
        no_id = dict(pub_env)
        del no_id["GITHUB_RUN_ID"]
        self.assertEqual(self.publish(out, env=no_id).reason, "no-run-id")
        self.assertEqual(self.writer.calls, [])

    def test_one_comment_per_pr(self) -> None:
        _, out = self.full()
        self.gh.ai_runs.append({"id": 5, "display_title": f"ai-review pr={PR} head={OTHER_SHA}"})
        self.gh.ai_jobs[5] = [{"name": "publish", "steps": [
            {"name": "Publish review comment", "status": "completed", "conclusion": "failure"}]}]
        pub = self.publish(out)
        self.assertEqual((pub.status, pub.reason), ("blocked", "comment-limit-run"))
        self.assertEqual(self.writer.calls, [])

    def test_comment_marker_limit_is_one(self) -> None:
        from test_ai_review import bot_comment

        _, out = self.full()
        self.gh.comments = [bot_comment(OTHER_SHA)]  # 다른 SHA 의 리뷰 코멘트가 이미 1개
        pub = self.publish(out)
        self.assertEqual((pub.status, pub.reason), ("blocked", "round-limit"))
        self.assertEqual(self.writer.calls, [])


class ForwardTest(PipelineCase):
    def end_to_end(self, transport=None):
        result, out = self.full(transport)
        pub = self.publish(out)
        self.assertEqual(pub.status, "posted")
        body = self.writer.calls[0][1]["body"]
        bridge_gh = BridgeGitHub(self.gh, body)
        return result, pub, bridge_gh

    def forward(self, bridge_gh, env=None, fire=None, url=None):
        self.fire = fire or FireTransport()
        env = {**FIRE_ENV, "BRIDGE_PR_NUMBER": str(PR), "BRIDGE_HEAD_SHA": SHA,
               "BRIDGE_REVIEW_URL": url or f"https://github.com/{REPO}/pull/{PR}#issuecomment-123", **(env or {})}
        return rp.stage_forward(env, self.factory, lambda: bridge_gh, self.fire)

    def test_full_pipeline_review_comment_to_claude_once(self) -> None:
        result, pub, bridge_gh = self.end_to_end()
        self.assertEqual(result.outcome, "needs_changes")
        res = self.forward(bridge_gh)
        self.assertEqual((res.status, res.reason), ("delivered", "fired"))
        self.assertEqual(len(self.transport.calls), 1)  # 모델 1회
        self.assertEqual(len(self.writer.calls), 1)  # 코멘트 1개
        self.assertEqual(len(self.fire.calls), 1)  # Claude 트리거 1회
        url, headers, body, _ = self.fire.calls[0]
        text = json.loads(body)["text"]
        self.assertIn(f"pull_request: https://github.com/{REPO}/pull/{PR}", text)
        self.assertIn(f"head_sha: {SHA}", text)
        self.assertNotIn("fake-routine-token", text)
        self.assertNotIn(INJECTION, text)  # PR·코멘트 내용은 넣지 않는다

    def test_pilot_head_branch_is_forwarded(self) -> None:
        self.gh.pull["head"]["ref"] = "test/routine-pr-open-20261101"
        _, _, bridge_gh = self.end_to_end()
        self.assertEqual(self.forward(bridge_gh).status, "delivered")

    def test_off_and_missing_secrets_make_no_calls(self) -> None:
        _, _, bridge_gh = self.end_to_end()
        before = len(bridge_gh.calls)
        cases = {
            ("disabled", "flag-off"): {"AI_REVIEW_ENABLED": ""},
            ("disabled", "flag-off "): {"ROUTINE_BRIDGE_ENABLED": ""},
            ("disabled", "no-token"): {"CLAUDE_ROUTINE_TOKEN": ""},
            ("disabled", "no-trigger-id"): {"CLAUDE_ROUTINE_TRIGGER_ID": ""},
            ("blocked", "rerun-not-allowed"): {"GITHUB_RUN_ATTEMPT": "2"},
        }
        for (status, reason), over in cases.items():
            with self.subTest(reason=reason):
                res = self.forward(bridge_gh, env=over)
                self.assertEqual((res.status, res.reason), (status, reason.strip()))
                self.assertEqual(self.fire.calls, [])
        self.assertEqual(len(bridge_gh.calls), before)

    def test_trigger_limit_one(self) -> None:
        _, _, bridge_gh = self.end_to_end()
        self.gh.ai_runs.append({"id": 6, "display_title": f"ai-review pr={PR} head={OTHER_SHA}"})
        self.gh.ai_jobs[6] = [{"name": "forward", "steps": [
            {"name": "Forward to Claude Routine", "status": "completed", "conclusion": "success"}]}]
        res = self.forward(bridge_gh)
        self.assertEqual((res.status, res.reason), ("blocked", "trigger-limit-run"))
        self.assertEqual(self.fire.calls, [])

    def test_non_pilot_and_bad_inputs(self) -> None:
        _, _, bridge_gh = self.end_to_end()
        res = self.forward(bridge_gh, env={"BRIDGE_PR_NUMBER": str(PR + 1)},
                           url=f"https://github.com/{REPO}/pull/{PR + 1}#issuecomment-123")
        self.assertEqual((res.status, res.reason), ("blocked", "pr-not-in-pilot"))
        res = self.forward(bridge_gh, url=f"https://github.com/{REPO}/pull/{PR + 1}#issuecomment-123")
        self.assertEqual(res.reason, "invalid-input:review_url-pr-mismatch")
        self.assertEqual(self.fire.calls, [])

    def test_bridge_still_rejects_non_needs_changes(self) -> None:
        result, out = self.full(FakeTransport(body=response(json.dumps(review_doc("no_changes")))))
        self.publish(out)
        bridge_gh = BridgeGitHub(self.gh, self.writer.calls[0][1]["body"])
        res = self.forward(bridge_gh)
        self.assertEqual((res.status, res.reason), ("blocked", "review-no-changes"))
        self.assertEqual(self.fire.calls, [])

    def test_fire_errors_are_failed_no_retry(self) -> None:
        _, _, bridge_gh = self.end_to_end()
        res = self.forward(bridge_gh, fire=FireTransport(status=429, body=b""))
        self.assertEqual(res.status, "failed")
        self.assertEqual(len(self.fire.calls), 1)


class AttemptHistoryTest(PipelineCase):
    def test_earlier_attempt_of_rerun_run_is_counted(self) -> None:
        """재실행된 run: 마지막 attempt 는 skipped 여도 첫 attempt 의 모델 호출을 센다 (jobs filter=all)."""
        self.gh.ai_runs.append({"id": 7, "display_title": f"ai-review pr={PR} head={OTHER_SHA}"})
        self.gh.ai_jobs[7] = [
            {"name": "review", "run_attempt": 1, "steps": [{"name": "Model review", "status": "completed", "conclusion": "success"}]},
            {"name": "review", "run_attempt": 2, "steps": [{"name": "Model review", "status": "completed", "conclusion": "skipped"}]},
        ]
        result, _ = self.prepare()
        self.assertEqual((result.status, result.reason), ("blocked", "round-limit-run"))
        self.assertTrue(self.gh.assertion_filter_all)


class StageOutputTest(NoNetwork):
    def test_publish_cli_reads_job_output_b64(self) -> None:
        import base64

        body = ar.render_comment(REPO, PR, SHA, RUN_ID, 1, 1, "needs_changes",
                                 ar.ReviewOutcome("needs_changes", "s", (ar.Finding("Minor", "f", 1, "t"),)))
        text = rp.dump_result(ar.Result("reviewed", "planned-not-posted", "needs_changes", PR, SHA, body), RUN_ID)
        b64 = base64.b64encode(text.encode()).decode()
        self.assertEqual(rp._decode_b64(b64), text)
        self.assertEqual(rp._decode_b64("@@not-base64@@"), "")
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = rp.main(["publish"], {"AI_REVIEW_RESULT_B64": b64, "GITHUB_TOKEN": "fake"})
        self.assertEqual((code, "result=dry_run" in buf.getvalue()), (0, True))
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = rp.main(["publish"], {"AI_REVIEW_RESULT_B64": "@@bad@@", "GITHUB_TOKEN": "fake"})
        self.assertEqual(code, 1)
        self.assertIn("reason=handoff-invalid", buf.getvalue())


# --- 전달 파일 변조 ---


class HandoffTest(PipelineCase):
    def test_prepared_handoff_tampering(self) -> None:
        _, good = self.prepare()
        doc = json.loads(good)
        bad_docs = [
            "not json",
            json.dumps(dict(doc, version=2)),
            json.dumps(dict(doc, kind="result")),
            json.dumps(dict(doc, input=dict(doc["input"], extra=1))),
            json.dumps(dict(doc, input=dict(doc["input"], head_sha="X" * 40))),
            json.dumps(dict(doc, input=dict(doc["input"], repo="attacker/repo"))),
            json.dumps(dict(doc, input=dict(doc["input"], pr_number="21"))),
            json.dumps(dict(doc, input=dict(doc["input"], files_truncated="no"))),
            json.dumps(dict(doc, input=dict(doc["input"], untrusted_files=[{"filename": "x"}]))),
            json.dumps(dict(doc, round_no=5)),
            json.dumps(dict(doc, run_id=0)),
        ]
        for text in bad_docs:
            with self.subTest(text=text[:40]):
                result, out = self.review(text)
                self.assertEqual((result.status, result.reason, out), ("failed", "handoff-invalid", ""))
                self.assertEqual(self.transport.calls, [])

    def test_result_file_tampering(self) -> None:
        _, out = self.full()
        doc = json.loads(out)
        cases = {
            ("failed", "handoff-invalid"): ["{", json.dumps(dict(doc, extra=1)), json.dumps(dict(doc, ci_run_id="1"))],
            ("blocked", "marker-mismatch"): [
                json.dumps(dict(doc, planned_comment=doc["planned_comment"].replace("result=needs_changes", "result=no_changes"))),
                json.dumps(dict(doc, outcome="no_changes")),
                json.dumps(dict(doc, head_sha=OTHER_SHA)),
            ],
            ("skipped", "not-publishable:blocked"): [json.dumps(dict(doc, status="blocked"))],
        }
        for (status, reason), texts in cases.items():
            for text in texts:
                with self.subTest(reason=reason):
                    pub = self.publish(text)
                    self.assertEqual((pub.status, pub.reason), (status, reason))
                    self.assertEqual(self.writer.calls, [])

    def test_result_for_other_pr_head_is_blocked_at_publish(self) -> None:
        _, out = self.full()
        self.gh.pull["head"]["sha"] = OTHER_SHA  # 리뷰 뒤 새 커밋 push
        pub = self.publish(out)
        self.assertEqual(pub.status, "blocked")
        self.assertEqual(self.writer.calls, [])

    def test_stale_after_review_has_no_result_file(self) -> None:
        prep, handoff = self.prepare()
        self.gh.pull_fetches = 0
        self.gh.change_on_recheck = None

        def push_during_review(g):
            g.pull["head"]["sha"] = OTHER_SHA

        original = self.gh.get

        def get(path):
            if path == f"/repos/{REPO}/pulls/{PR}":
                push_during_review(self.gh)
            return original(path)

        self.gh.get = get  # review 단계의 재조회 시점에 이미 바뀜
        result, out = self.review(handoff)
        self.assertEqual((result.status, result.reason, out), ("blocked", "stale-head-after-review", ""))


# --- CLI (네트워크 없는 경로만) ---


class CliTest(NoNetwork):
    def run_main(self, argv, env) -> tuple[int, str]:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = rp.main(argv, env)
        return code, buf.getvalue()

    def test_off_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.txt"
            summary = Path(tmp) / "summary.md"
            env = {"GITHUB_OUTPUT": str(out), "GITHUB_STEP_SUMMARY": str(summary),
                   "AI_REVIEW_HANDOFF": str(Path(tmp) / "h.json")}
            code, text = self.run_main(["prepare"], env)
            self.assertEqual(code, 3)
            self.assertIn("result=disabled reason=flag-off", text)
            self.assertFalse(out.exists())
            self.assertFalse((Path(tmp) / "h.json").exists())
            self.assertIn("AI 리뷰 완료·코멘트 게시·Claude 전달이 아니다", summary.read_text(encoding="utf-8"))

    def test_review_without_key_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            handoff = Path(tmp) / "h.json"
            handoff.write_text("{}", encoding="utf-8")
            env = {**GATE_ENV, "AI_REVIEW_HANDOFF": str(handoff), "AI_REVIEW_RESULT": str(Path(tmp) / "r.json")}
            code, text = self.run_main(["review"], env)
        self.assertEqual(code, 3)
        self.assertIn("reason=no-api-key", text)

    def test_publish_cli_default_dry_run(self) -> None:
        body = ar.render_comment(REPO, PR, SHA, RUN_ID, 1, 2, "needs_changes",
                                 ar.ReviewOutcome("needs_changes", "s", (ar.Finding("Minor", "f", 1, "t"),)))
        result = ar.Result("reviewed", "planned-not-posted", "needs_changes", PR, SHA, body)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "r.json"
            path.write_text(rp.dump_result(result, RUN_ID), encoding="utf-8")
            code, text = self.run_main(["publish"], {"AI_REVIEW_RESULT": str(path), "GITHUB_TOKEN": "fake"})
        self.assertEqual(code, 0)
        self.assertIn("result=dry_run reason=publish-disabled", text)

    def test_isolated_mode_subprocess_like_workflow(self) -> None:
        """워크플로와 같은 python3 -I -B 실행에서 import 가 되는지 (단위 테스트만으로는 못 잡는다)."""
        import os
        import subprocess
        import sys

        script = Path(__file__).resolve().parent / "review_pipeline.py"
        base = {"PATH": os.environ.get("PATH", "")}
        for argv, env, code, text in (
            (["prepare"], base, 3, "result=disabled reason=flag-off"),
            (["review"], {**base, "AI_REVIEW_ENABLED": "true", "GITHUB_RUN_ATTEMPT": "1"}, 3, "reason=no-api-key"),
            (["review"], {**base, "AI_REVIEW_ENABLED": "true", "GITHUB_RUN_ATTEMPT": "2"}, 2, "reason=rerun-not-allowed"),
            (["forward"], {**base, "AI_REVIEW_ENABLED": "true", "GITHUB_RUN_ATTEMPT": "1"}, 3, "routine-bridge result=disabled reason=flag-off"),
        ):
            with self.subTest(argv=argv):
                proc = subprocess.run([sys.executable, "-I", "-B", str(script), *argv], env=env,
                                      capture_output=True, text=True, timeout=30, cwd="/")
                self.assertEqual(proc.returncode, code, proc.stderr)
                self.assertIn(text, proc.stdout)
                self.assertNotIn("Traceback", proc.stderr)

    def test_unknown_stage(self) -> None:
        self.assertEqual(rp.main(["deploy"], {}), 1)


# --- 워크플로와의 계약 ---


class WorkflowContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = WORKFLOW.read_text(encoding="utf-8")

    def test_model_step_name_and_gate(self) -> None:
        self.assertIn(f"      - name: {rp.MODEL_STEP}\n        id: model\n        if: steps.prepare.outputs.ready == 'true'",
                      self.text)
        self.assertIn(f"  {rp.REVIEW_JOB}:\n    name: {rp.REVIEW_JOB}", self.text)

    def test_run_name_matches_ledger_regex(self) -> None:
        line = next(l for l in self.text.splitlines() if l.startswith("run-name:"))
        rendered = (line.split(": ", 1)[1]
                    .replace("${{ github.event.workflow_run.pull_requests[0].number }}", str(PR))
                    .replace("${{ github.event.workflow_run.head_sha }}", SHA))
        self.assertEqual(rp.RUN_NAME_RE.fullmatch(rendered).groups(), (str(PR), SHA))

    def test_stage_steps_and_job_outputs(self) -> None:
        self.assertIn(f"  {rp.PUBLISH_JOB}:\n    name: {rp.PUBLISH_JOB}", self.text)
        self.assertIn(f"  {rp.FORWARD_JOB}:\n    name: {rp.FORWARD_JOB}", self.text)
        self.assertIn(f"      - name: {rp.PUBLISH_STEP}\n        id: post", self.text)
        self.assertIn(f"      - name: {rp.FORWARD_STEP}\n", self.text)
        self.assertIn("result_b64: ${{ steps.model.outputs.result_b64 }}", self.text)
        self.assertIn("AI_REVIEW_RESULT_B64: ${{ needs.review.outputs.result_b64 }}", self.text)
        for key in ("comment_url", "outcome", "pr_number", "head_sha"):
            self.assertIn(f"{key}: ${{{{ steps.post.outputs.{key} }}}}", self.text)
        self.assertIn("BRIDGE_REVIEW_URL: ${{ needs.publish.outputs.comment_url }}", self.text)
        self.assertIn(f"pull_requests[0].number == {rp.PILOT_PR_NUMBERS[0]}", self.text)

    def test_handoff_paths_shared_between_steps(self) -> None:
        self.assertEqual(self.text.count("AI_REVIEW_HANDOFF: ${{ runner.temp }}/ai-review-input.json"), 2)


if __name__ == "__main__":
    unittest.main()
