#!/usr/bin/env python3
"""review_pipeline.py — AI 리뷰 실행 경로 (Issue #5 Phase 2-C·2-D). 기존 모듈을 연결만 한다.

  prepare  (모델 키 없음, GitHub 읽기)       ai_review.prepare_review + 실행 기록 상한 → 전달 파일(handoff)
  review   (모델 키, GitHub 읽기)            handoff → OpenAIReviewer 1회 → ai_review.finish_review → 결과
  publish  (GitHub PR 쓰기, 모델 키 없음)    결과 → 재검증 → github_publisher.publish_review → PR 코멘트 1개
  forward  (Routine 토큰, GitHub 읽기)       게시된 needs_changes 코멘트 → routine_bridge.run_bridge → Claude /fire 1회

시범 운영 (Phase 2-D): 테스트 PR #10 만, PR 당 모델 호출 1회·코멘트 1개·Claude 트리거 1회 (코드에 고정).
실제 호출은 사용자가 Secret·변수를 넣었을 때만 일어난다:
  - AI_REVIEW_ENABLED=true  (변수)   없으면 모든 단계 disabled
  - OPENAI_API_KEY          (Secret) 없으면 review 단계 disabled (모델 호출 0회)
  - AI_REVIEW_PUBLISH=true  (변수)   아니면 publish 단계 dry_run (코멘트 0개)
  - ROUTINE_BRIDGE_ENABLED=true + CLAUDE_ROUTINE_TOKEN·CLAUDE_ROUTINE_TRIGGER_ID (변수·Secret)
                                     없으면 forward 단계 disabled (Claude 트리거 0회)

호출 상한은 두 기록으로 센다 (둘 중 하나라도 걸리면 멈춤):
  1. PR 코멘트의 ai-review 마커 (ai_review.check_ledger)
  2. Actions 실행 기록 (step_attempts): ai-review 워크플로의 이전 실행에서 해당 step 이 시작된 횟수.
     PR 코멘트를 지워도 남고, 시간 초과·실패·취소·진행 중인 실행과 재실행 이전 attempt 도 센다 (재시도 0)
이 기록이 빠지는 경로는 호출 전에 막는다 (PR #16 리뷰):
  - 재실행: GITHUB_RUN_ATTEMPT 가 정확히 1 이 아니면 모든 단계 차단
  - PR 식별자: 이벤트 PR 이 정확히 1개가 아니거나 현재 run 의 실제 이름이 "ai-review pr=N head=SHA" 가 아니면 차단

종료 코드: 0 진행(ready/reviewed/posted/dry_run/delivered), 1 failed, 2 blocked/skipped, 3 disabled.
키·토큰·응답 본문은 출력하지 않는다. 표준 라이브러리만 사용한다. 운영 문서: docs/AUTOMATION.md
"""

from __future__ import annotations

import base64
import binascii
import dataclasses
import json
import os
import re
import sys
import urllib.parse
from pathlib import Path
from typing import Callable, Mapping

# 워크플로는 python3 -I(격리 모드)로 실행하므로 스크립트 디렉터리가 import 경로에 없다.
# 같은 디렉터리(워크플로가 checkout 한 기본 브랜치)의 모듈만 쓰도록 명시적으로 넣는다
sys.path.insert(0, str(Path(__file__).resolve().parent))

import ai_review as ar  # noqa: E402
import github_publisher as gp  # noqa: E402
import openai_reviewer as orv  # noqa: E402
import routine_bridge as rb  # noqa: E402

AI_REVIEW_WORKFLOW = "ai-review.yml"
REVIEW_JOB = "review"
MODEL_STEP = "Model review"  # ai-review.yml 의 step 이름과 같아야 한다 (test_review_pipeline.py 가 검사)
PUBLISH_JOB = "publish"
PUBLISH_STEP = "Publish review comment"
FORWARD_JOB = "forward"
FORWARD_STEP = "Forward to Claude Routine"

# --- 시범 운영 제한 (신뢰된 main 코드에 고정. 변수로 바꿀 수 없다) ---
PILOT_PR_NUMBERS = (10,)  # 테스트 PR #10 만
PILOT_MAX_MODEL_CALLS = 1  # PR 당 모델 호출
PILOT_MAX_COMMENTS = 1  # PR 당 리뷰 코멘트
PILOT_MAX_TRIGGERS = 1  # PR 당 Claude Routine 트리거
RUN_NAME_RE = re.compile(r"^ai-review pr=([1-9][0-9]{0,6}) head=([0-9a-f]{40})$")
RUNS_PER_PAGE = 100
RUNS_MAX_PAGES = 10
RUNS_MAX_CANDIDATES = 30
HANDOFF_VERSION = 1
MAX_HANDOFF_BYTES = 2_000_000

EXIT = {"ready": 0, "reviewed": 0, "posted": 0, "dry_run": 0, "delivered": 0, "failed": 1, "blocked": 2, "skipped": 2,
        "disabled": 3}


# --- 실행 기록 상한 (삭제할 수 없는 기록) ---


def step_attempts(gh: ar.GitHub, repo: str, pr: int, current_run_id: int, job_name: str,
                  step_name: str) -> set[str] | None:
    """이 PR 의 이전 ai-review 실행에서 job_name 의 step_name 이 시작된 head SHA 들.

    재실행 이전 attempt 의 job 도 보도록 filter=all 로 조회한다 (기본값 latest 는 마지막 attempt 만 준다).
    기록을 끝까지 못 읽으면 None (안전하게 실패).
    """
    candidates: list[tuple[int, str]] = []
    for page in range(1, RUNS_MAX_PAGES + 1):
        query = urllib.parse.urlencode({"per_page": RUNS_PER_PAGE, "page": page})
        data = gh.get(f"/repos/{repo}/actions/workflows/{AI_REVIEW_WORKFLOW}/runs?{query}")
        runs = data.get("workflow_runs") if isinstance(data, dict) else None
        if not isinstance(runs, list):
            raise ar.GitHubError()
        for run in runs:
            if not isinstance(run, dict) or run.get("id") == current_run_id:
                continue
            m = RUN_NAME_RE.fullmatch(run.get("display_title") or "")
            if m and int(m.group(1)) == pr and isinstance(run.get("id"), int):
                candidates.append((run["id"], m.group(2)))
        if len(runs) < RUNS_PER_PAGE:
            break
    else:
        return None
    if len(candidates) > RUNS_MAX_CANDIDATES:
        return None
    heads = set()
    for run_id, head in candidates:
        jobs = gh.get(f"/repos/{repo}/actions/runs/{run_id}/jobs?filter=all&per_page=100")
        items = jobs.get("jobs") if isinstance(jobs, dict) else None
        if not isinstance(items, list):
            raise ar.GitHubError()
        for job in items:
            if not isinstance(job, dict) or job.get("name") != job_name:
                continue
            for step in job.get("steps") or []:
                if isinstance(step, dict) and step.get("name") == step_name and step_started(step):
                    heads.add(head)
    return heads


def model_attempts(gh: ar.GitHub, repo: str, pr: int, current_run_id: int) -> set[str] | None:
    return step_attempts(gh, repo, pr, current_run_id, REVIEW_JOB, MODEL_STEP)


def pilot_check(gh: ar.GitHub, ev: ar.Event, pr: int, limit: int) -> ar.Result | None:
    return None if pr in PILOT_PR_NUMBERS else ar.Result("blocked", "pr-not-in-pilot")


def step_started(step: dict) -> bool:
    """시작된 step 은 결과와 관계없이 1회 (호출 직후 timeout·실패·취소도 비용이 났을 수 있다)."""
    if step.get("status") == "in_progress":
        return True
    return step.get("status") == "completed" and step.get("conclusion") not in ("skipped", None)


def run_ledger_check(current_run_id: int, repo: str = ar.TRUSTED_REPO) -> ar.ExtraCheck:
    def check(gh: ar.GitHub, ev: ar.Event, pr: int, limit: int) -> ar.Result | None:
        heads = model_attempts(gh, repo, pr, current_run_id)
        if heads is None:
            return ar.Result("failed", "run-ledger-incomplete")
        if ev.head_sha in heads:
            return ar.Result("blocked", "duplicate-sha-run")
        if len(heads) >= limit:
            return ar.Result("blocked", "round-limit-run")
        return None

    return check


# --- 전달 파일 (같은 신뢰된 main 코드가 쓰고 읽지만, 형식은 엄격히 검사한다) ---


class HandoffError(Exception):
    pass


def dump_prepared(prepared: ar.Prepared) -> str:
    return json.dumps({"version": HANDOFF_VERSION, "kind": "prepared", "run_id": prepared.run_id,
                       "round_no": prepared.round_no, "limit": prepared.limit,
                       "input": dataclasses.asdict(prepared.data)}, ensure_ascii=False)


def _load(text: str, kind: str) -> dict:
    if len(text.encode()) > MAX_HANDOFF_BYTES:
        raise HandoffError()
    try:
        doc = json.loads(text)
    except ValueError:
        raise HandoffError() from None
    if not isinstance(doc, dict) or doc.get("version") != HANDOFF_VERSION or doc.get("kind") != kind:
        raise HandoffError()
    return doc


def _int(value: object, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise HandoffError()
    return value


def _str(value: object) -> str:
    if not isinstance(value, str):
        raise HandoffError()
    return value


def load_prepared(text: str, repo: str = ar.TRUSTED_REPO) -> ar.Prepared:
    doc = _load(text, "prepared")
    raw = doc.get("input")
    fields = {f.name for f in dataclasses.fields(ar.ReviewInput)}
    if not isinstance(raw, dict) or set(raw) != fields:
        raise HandoffError()
    if raw["repo"] != repo or not ar.SHA_RE.fullmatch(_str(raw["head_sha"])):
        raise HandoffError()
    files = []
    for f in raw["untrusted_files"] if isinstance(raw["untrusted_files"], list) else _raise():
        if not isinstance(f, dict) or set(f) != {"filename", "status", "patch", "truncated"}:
            raise HandoffError()
        files.append(ar.ChangedFile(_str(f["filename"]), _str(f["status"]), _str(f["patch"]), _bool(f["truncated"])))
    policy = []
    for item in raw["trusted_policy"] if isinstance(raw["trusted_policy"], list) else _raise():
        if not isinstance(item, list) or len(item) != 2:
            raise HandoffError()
        policy.append((_str(item[0]), _str(item[1])))
    gaps = raw["context_gaps"] if isinstance(raw["context_gaps"], list) else _raise()
    data = ar.ReviewInput(
        repo=raw["repo"], pr_number=_int(raw["pr_number"], 1), head_sha=raw["head_sha"],
        round_no=_int(raw["round_no"], 1), max_rounds=_int(raw["max_rounds"], 1),
        untrusted_title=_str(raw["untrusted_title"]), untrusted_body=_str(raw["untrusted_body"]),
        untrusted_files=tuple(files), files_truncated=_bool(raw["files_truncated"]),
        text_truncated=_bool(raw["text_truncated"]), ci_run_url=_str(raw["ci_run_url"]),
        ci_conclusion=_str(raw["ci_conclusion"]), trusted_policy=tuple(policy),
        issue_number=_int(raw["issue_number"]), untrusted_issue_title=_str(raw["untrusted_issue_title"]),
        untrusted_issue_body=_str(raw["untrusted_issue_body"]), context_gaps=tuple(_str(g) for g in gaps),
    )
    prepared = ar.Prepared(data, _int(doc.get("run_id"), 1), _int(doc.get("round_no"), 1), _int(doc.get("limit"), 1))
    if prepared.round_no != data.round_no or prepared.limit != data.max_rounds:
        raise HandoffError()
    return prepared


def _bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise HandoffError()
    return value


def _raise():
    raise HandoffError()


def dump_result(result: ar.Result, ci_run_id: int) -> str:
    return json.dumps({"version": HANDOFF_VERSION, "kind": "result", "ci_run_id": ci_run_id,
                       "status": result.status, "reason": result.reason, "outcome": result.outcome,
                       "pr_number": result.pr_number, "head_sha": result.head_sha,
                       "planned_comment": result.planned_comment}, ensure_ascii=False)


def load_result(text: str) -> tuple[ar.Result, int]:
    doc = _load(text, "result")
    keys = {"version", "kind", "ci_run_id", "status", "reason", "outcome", "pr_number", "head_sha", "planned_comment"}
    if set(doc) != keys:
        raise HandoffError()
    result = ar.Result(_str(doc["status"]), _str(doc["reason"]), _str(doc["outcome"]), _int(doc["pr_number"]),
                       _str(doc["head_sha"]), _str(doc["planned_comment"]))
    return result, _int(doc["ci_run_id"], 1)


# --- 단계 ---


def stage_prepare(env: Mapping[str, str], event_loader: Callable[[], object],
                  gh_factory: Callable[[], ar.GitHub], policy_loader: ar.PolicyLoader = ar.load_trusted_policy,
                  repo: str = ar.TRUSTED_REPO) -> tuple[ar.Result, str]:
    """(결과, handoff 문자열). 통과하면 결과 status=ready."""
    if not ar.Config.from_env(env).enabled:
        return ar.Result("disabled", "flag-off"), ""
    stop = check_run_attempt(env)
    if stop:
        return stop, ""  # 이벤트·GitHub 조회 전에 멈춘다
    current = _current_run_id(env)
    if current is None:
        return ar.Result("failed", "no-run-id"), ""
    # 시범: PR 당 모델 호출 상한을 고정한다 (AI_REVIEW_MAX_ROUNDS 로 늘릴 수 없음)
    config = dataclasses.replace(ar.Config.from_env(env), max_rounds_raw=str(PILOT_MAX_MODEL_CALLS))
    # 순서: 시범 대상 PR 인지 → 이 실행이 기록될 수 있는지(run-name) → 실행 기록 상한
    prepared = ar.prepare_review(config, event_loader, gh_factory, repo, policy_loader,
                                 (pilot_check, run_identity_check(current, repo), run_ledger_check(current, repo)))
    if isinstance(prepared, ar.Result):
        return prepared, ""
    data = prepared.data
    return ar.Result("ready", "prepared", pr_number=data.pr_number, head_sha=data.head_sha), dump_prepared(prepared)


def check_run_attempt(env: Mapping[str, str]) -> ar.Result | None:
    """재실행(run_attempt 2 이상)은 자동 리뷰하지 않는다. 값이 없거나 이상해도 막는다."""
    raw = env.get("GITHUB_RUN_ATTEMPT", "")
    if raw == "1":
        return None
    if re.fullmatch(r"[1-9][0-9]{0,5}", raw):
        return ar.Result("blocked", "rerun-not-allowed")
    return ar.Result("failed", "run-attempt-invalid")


def run_identity_check(current_run_id: int, repo: str = ar.TRUSTED_REPO) -> ar.ExtraCheck:
    """이 실행이 실행 기록에 올바른 PR·SHA 로 남는지 모델 호출 전에 확인한다."""

    def check(gh: ar.GitHub, ev: ar.Event, pr: int, limit: int) -> ar.Result | None:
        # run-name 은 pull_requests[0] 을 쓴다. 정확히 1개이고 확정된 PR 과 같아야 한다
        if ev.payload_prs != (pr,):
            return ar.Result("blocked", "run-name-pr-untracked")
        run = gh.get(f"/repos/{repo}/actions/runs/{current_run_id}")
        if not isinstance(run, dict):
            return ar.Result("failed", "github-unexpected-response")
        if run.get("display_title") != f"ai-review pr={pr} head={ev.head_sha}":
            return ar.Result("blocked", "run-name-pr-untracked")
        if run.get("run_attempt") != 1:
            return ar.Result("blocked", "rerun-not-allowed")
        return None

    return check


def _current_run_id(env: Mapping[str, str]) -> int | None:
    raw = env.get("GITHUB_RUN_ID", "")
    return int(raw) if re.fullmatch(r"[1-9][0-9]{0,19}", raw) else None


def stage_review(env: Mapping[str, str], handoff: str, gh_factory: Callable[[], ar.GitHub],
                 transport: orv.Transport, repo: str = ar.TRUSTED_REPO) -> tuple[ar.Result, str]:
    """(결과, 결과 파일 문자열). 키가 없으면 disabled 이고 모델·GitHub 호출 0회."""
    if not ar.Config.from_env(env).enabled:
        return ar.Result("disabled", "flag-off"), ""
    stop = check_run_attempt(env)
    if stop:
        return stop, ""  # 재실행이면 키·전달 파일을 보기 전에 멈춘다
    try:
        oconfig = orv.OpenAIConfig.from_env(env)
    except ar.ReviewerError as exc:
        if exc.code == "config-no-api-key":
            return ar.Result("disabled", "no-api-key"), ""
        return ar.Result("failed", f"config-invalid:{exc.code}"), ""
    try:
        prepared = load_prepared(handoff, repo)
    except HandoffError:
        return ar.Result("failed", "handoff-invalid"), ""
    if prepared.data.pr_number not in PILOT_PR_NUMBERS or prepared.limit != PILOT_MAX_MODEL_CALLS:
        return ar.Result("blocked", "pr-not-in-pilot"), ""
    reviewer = orv.OpenAIReviewer(oconfig, transport)
    result = ar.finish_review(prepared, reviewer, gh_factory(), repo)
    return result, dump_result(result, prepared.run_id) if result.planned_comment else ""


def stage_publish(env: Mapping[str, str], result_text: str, gh_factory: Callable[[], ar.GitHub],
                  writer_factory: Callable[[], gp.Writer], repo: str = ar.TRUSTED_REPO) -> gp.PublishResult:
    """받은 결과를 믿지 않고 게시 직전에 다시 검사한다. AI_REVIEW_PUBLISH=true 가 아니면 조회·쓰기 0회."""
    try:
        result, ci_run_id = load_result(result_text)
    except HandoffError:
        return gp.PublishResult("failed", "handoff-invalid")
    stop = gp.publishable(result)
    if stop:
        return stop
    if env.get("AI_REVIEW_PUBLISH", "") != "true":
        return gp.PublishResult("dry_run", "publish-disabled")
    config = ar.Config.from_env(env)
    if not config.enabled:
        return gp.PublishResult("skipped", "flag-off")
    attempt = check_run_attempt(env)
    if attempt:
        return gp.PublishResult(attempt.status, attempt.reason)
    current = _current_run_id(env)
    if current is None:
        return gp.PublishResult("failed", "no-run-id")
    if result.pr_number not in PILOT_PR_NUMBERS:
        return gp.PublishResult("blocked", "pr-not-in-pilot")
    gh = gh_factory()
    ev = ar.Event(ci_run_id, result.head_sha, (result.pr_number,))
    try:
        stop = ar.verify_ci_run(gh, ev, repo, result.pr_number) or ar.check_protected_paths(gh, result.pr_number, repo)
        if stop:
            return gp.PublishResult(stop.status, stop.reason)
        posted = step_attempts(gh, repo, result.pr_number, current, PUBLISH_JOB, PUBLISH_STEP)
    except ar.GitHubError:
        return gp.PublishResult("failed", "github-api-error")
    if posted is None:
        return gp.PublishResult("failed", "run-ledger-incomplete")
    if len(posted) >= PILOT_MAX_COMMENTS:
        return gp.PublishResult("blocked", "comment-limit-run")
    return gp.publish_review(result, gh, writer_factory(), publish=True, repo=repo, limit=PILOT_MAX_COMMENTS)


def stage_forward(env: Mapping[str, str], gh_factory: Callable[[], ar.GitHub],
                  bridge_gh_factory: Callable[[], rb.GitHub], transport: rb.Transport,
                  repo: str = ar.TRUSTED_REPO) -> rb.Result:
    """게시된 needs_changes 코멘트를 기존 routine_bridge 로 Claude Routine 에 1회 넘긴다.

    검증(PR·CI·코멘트 작성자·마커·직전 재확인)은 routine_bridge.run_bridge 가 한다. 여기서는 시범 제한과
    ai-review 실행 기록(트리거 1회)만 더한다. 토큰·트리거 ID 가 없으면 GitHub 조회 없이 disabled.
    """
    if not ar.Config.from_env(env).enabled:
        return rb.Result("disabled", "flag-off")
    attempt = check_run_attempt(env)
    if attempt:
        return rb.Result(attempt.status, attempt.reason)
    bridge_env = {**env, "ROUTINE_BRIDGE_MAX_ROUNDS": str(PILOT_MAX_TRIGGERS)}
    config = rb.Config.from_env(bridge_env)
    off = rb.check_config(config)
    if off:
        return off
    current = _current_run_id(env)
    if current is None:
        return rb.Result("failed", "no-run-id")
    req = rb.parse_request(env.get("BRIDGE_PR_NUMBER", ""), env.get("BRIDGE_HEAD_SHA", ""),
                           env.get("BRIDGE_REVIEW_URL", ""), repo)
    if isinstance(req, rb.Result):
        return req
    if req.pr_number not in PILOT_PR_NUMBERS:
        return rb.Result("blocked", "pr-not-in-pilot")
    try:
        fired = step_attempts(gh_factory(), repo, req.pr_number, current, FORWARD_JOB, FORWARD_STEP)
    except ar.GitHubError:
        return rb.Result("failed", "github-api-error")
    if fired is None:
        return rb.Result("failed", "run-ledger-incomplete")
    if len(fired) >= PILOT_MAX_TRIGGERS:
        return rb.Result("blocked", "trigger-limit-run")
    return rb.run_bridge(config, str(req.pr_number), req.head_sha, req.review_url, bridge_gh_factory, transport, repo)


# --- CLI ---


def _summary(env: Mapping[str, str], stage: str, status: str, reason: str) -> None:
    path = env.get("GITHUB_STEP_SUMMARY", "")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"### ai-review {stage}\n- result: `{status}`\n- reason: `{reason}`\n")
        if status not in ("reviewed", "posted", "delivered"):
            f.write("- **AI 리뷰 완료·코멘트 게시·Claude 전달이 아니다**\n")


def _write_output(env: Mapping[str, str], key: str, value: str) -> None:
    path = env.get("GITHUB_OUTPUT", "")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{key}={value}\n")


def _decode_b64(value: str) -> str:
    try:
        return base64.b64decode(value.encode(), validate=True).decode("utf-8") if value else ""
    except (binascii.Error, UnicodeDecodeError):
        return ""


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    argv = sys.argv[1:] if argv is None else argv
    stage = argv[0] if argv else ""
    token = env.get("GITHUB_TOKEN", "")
    gh_factory = lambda: ar.LiveGitHub(token)  # noqa: E731
    if stage == "prepare":
        result, handoff = stage_prepare(env, lambda: ar.load_event_file(env.get("GITHUB_EVENT_PATH", "")), gh_factory)
        if result.status == "ready":
            path = env.get("AI_REVIEW_HANDOFF", "")
            if not path:
                result = ar.Result("failed", "no-handoff-path")
            else:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(handoff)
                _write_output(env, "ready", "true")
        status, reason, line = result.status, result.reason, result.line()
    elif stage == "review":
        try:
            with open(env.get("AI_REVIEW_HANDOFF", ""), encoding="utf-8") as f:
                handoff = f.read()
        except OSError:
            handoff = ""
        result, out = stage_review(env, handoff, gh_factory, orv.live_transport)
        if out:
            if env.get("AI_REVIEW_RESULT"):
                with open(env["AI_REVIEW_RESULT"], "w", encoding="utf-8") as f:
                    f.write(out)
            # publish job 으로 넘기는 값. 한 줄 base64 (job output). publish 단계가 다시 검증한다
            _write_output(env, "result_b64", base64.b64encode(out.encode()).decode())
        status, reason, line = result.status, result.reason, result.line()
    elif stage == "publish":
        text = _decode_b64(env.get("AI_REVIEW_RESULT_B64", ""))
        if not text and env.get("AI_REVIEW_RESULT"):
            try:
                with open(env["AI_REVIEW_RESULT"], encoding="utf-8") as f:
                    text = f.read()
            except OSError:
                text = ""
        pub = stage_publish(env, text, gh_factory, lambda: gp.LiveGitHubWriter(token))
        if pub.status == "posted":
            posted, _ = load_result(text)
            # forward job 으로 넘기는 값. forward 단계(routine_bridge)가 GitHub 에서 다시 검증한다
            for key, value in (("comment_url", pub.comment_url), ("outcome", posted.outcome),
                               ("pr_number", str(posted.pr_number)), ("head_sha", posted.head_sha)):
                _write_output(env, key, value)
        status, reason, line = pub.status, pub.reason, pub.line()
    elif stage == "forward":
        res = stage_forward(env, gh_factory, lambda: rb.LiveGitHub(token), rb.live_transport)
        status, reason, line = res.status, res.reason, res.line()
    else:
        print("usage: review_pipeline.py prepare|review|publish|forward", file=sys.stderr)
        return 1
    print(line)
    _summary(env, stage, status, reason)
    return EXIT.get(status, 1)


if __name__ == "__main__":
    sys.exit(main())
