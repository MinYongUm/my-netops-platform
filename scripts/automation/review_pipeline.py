#!/usr/bin/env python3
"""review_pipeline.py — AI 리뷰 실행 경로 (Issue #5 Phase 2-C). 기존 모듈을 연결만 한다.

  prepare  (모델 키 없음, GitHub 읽기)   ai_review.prepare_review + 실행 기록 상한 → 전달 파일(handoff)
  review   (모델 키, GitHub 읽기)        handoff → OpenAIReviewer 1회 → ai_review.finish_review → 결과 파일
  publish  (GitHub 쓰기, 모델 키 없음)   결과 파일 → 재검증 → github_publisher.publish_review (기본 publish=False)

현재 상태 (기본 OFF):
  - 워크플로(ai-review.yml)는 prepare·review 두 step 만 있고, OPENAI_API_KEY 를 연결하지 않는다.
    AI_REVIEW_ENABLED 를 켜도 review 단계는 키가 없어 disabled 로 끝난다 (모델 호출 0회)
  - publish 단계는 워크플로에 없다. 실행해도 AI_REVIEW_PUBLISH=true 가 아니면 조회·쓰기 0회 (dry_run)
  - Secret 연결·쓰기 권한·publish job 추가는 사용자 승인 후 별도 PR

호출 상한은 두 기록으로 센다 (둘 중 하나라도 걸리면 멈춤):
  1. PR 코멘트의 ai-review 마커 (ai_review.check_ledger)
  2. Actions 실행 기록 (이 파일의 run_ledger): ai-review 워크플로에서 "Model review" step 이 시작된 실행.
     PR 코멘트를 지워도 남고, 시간 초과·실패·진행 중인 실행도 1회로 센다 (재시도 0)

종료 코드: 0 진행(ready/reviewed/posted/dry_run), 1 failed, 2 blocked/skipped, 3 disabled.
키·토큰·응답 본문은 출력하지 않는다. 표준 라이브러리만 사용한다. 운영 문서: docs/AUTOMATION.md
"""

from __future__ import annotations

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

AI_REVIEW_WORKFLOW = "ai-review.yml"
REVIEW_JOB = "review"
MODEL_STEP = "Model review"  # ai-review.yml 의 step 이름과 같아야 한다 (test_review_pipeline.py 가 검사)
RUN_NAME_RE = re.compile(r"^ai-review pr=([1-9][0-9]{0,6}) head=([0-9a-f]{40})$")
RUNS_PER_PAGE = 100
RUNS_MAX_PAGES = 10
RUNS_MAX_CANDIDATES = 30
HANDOFF_VERSION = 1
MAX_HANDOFF_BYTES = 2_000_000

EXIT = {"ready": 0, "reviewed": 0, "posted": 0, "dry_run": 0, "failed": 1, "blocked": 2, "skipped": 2, "disabled": 3}


# --- 실행 기록 상한 (삭제할 수 없는 기록) ---


def model_attempts(gh: ar.GitHub, repo: str, pr: int, current_run_id: int) -> set[str] | None:
    """이 PR 에서 모델 호출 step 이 시작된 head SHA 들. 기록을 끝까지 못 읽으면 None (안전하게 실패)."""
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
        jobs = gh.get(f"/repos/{repo}/actions/runs/{run_id}/jobs")
        items = jobs.get("jobs") if isinstance(jobs, dict) else None
        if not isinstance(items, list):
            raise ar.GitHubError()
        for job in items:
            if not isinstance(job, dict) or job.get("name") != REVIEW_JOB:
                continue
            for step in job.get("steps") or []:
                if isinstance(step, dict) and step.get("name") == MODEL_STEP and step_started(step):
                    heads.add(head)
    return heads


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
    current = _current_run_id(env)
    if current is None:
        config = ar.Config.from_env(env)
        if not config.enabled:
            return ar.Result("disabled", "flag-off"), ""
        return ar.Result("failed", "no-run-id"), ""
    prepared = ar.prepare_review(ar.Config.from_env(env), event_loader, gh_factory, repo, policy_loader,
                                 (run_ledger_check(current, repo),))
    if isinstance(prepared, ar.Result):
        return prepared, ""
    data = prepared.data
    return ar.Result("ready", "prepared", pr_number=data.pr_number, head_sha=data.head_sha), dump_prepared(prepared)


def _current_run_id(env: Mapping[str, str]) -> int | None:
    raw = env.get("GITHUB_RUN_ID", "")
    return int(raw) if re.fullmatch(r"[1-9][0-9]{0,19}", raw) else None


def stage_review(env: Mapping[str, str], handoff: str, gh_factory: Callable[[], ar.GitHub],
                 transport: orv.Transport, repo: str = ar.TRUSTED_REPO) -> tuple[ar.Result, str]:
    """(결과, 결과 파일 문자열). 키가 없으면 disabled 이고 모델·GitHub 호출 0회."""
    if not ar.Config.from_env(env).enabled:
        return ar.Result("disabled", "flag-off"), ""
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
    limit = ar.max_rounds(config)
    if limit is None:
        return gp.PublishResult("failed", "config-invalid:max-rounds")
    gh = gh_factory()
    ev = ar.Event(ci_run_id, result.head_sha, (result.pr_number,))
    try:
        stop = ar.verify_ci_run(gh, ev, repo, result.pr_number) or ar.check_protected_paths(gh, result.pr_number, repo)
    except ar.GitHubError:
        return gp.PublishResult("failed", "github-api-error")
    if stop:
        return gp.PublishResult(stop.status, stop.reason)
    return gp.publish_review(result, gh, writer_factory(), publish=True, repo=repo, limit=limit)


# --- CLI ---


def _summary(env: Mapping[str, str], stage: str, status: str, reason: str) -> None:
    path = env.get("GITHUB_STEP_SUMMARY", "")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"### ai-review {stage}\n- result: `{status}`\n- reason: `{reason}`\n")
        if status not in ("reviewed", "posted"):
            f.write("- **AI 리뷰 완료·코멘트 게시가 아니다**\n")


def _write_output(env: Mapping[str, str], key: str, value: str) -> None:
    path = env.get("GITHUB_OUTPUT", "")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{key}={value}\n")


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
        if out and env.get("AI_REVIEW_RESULT"):
            with open(env["AI_REVIEW_RESULT"], "w", encoding="utf-8") as f:
                f.write(out)
        status, reason, line = result.status, result.reason, result.line()
    elif stage == "publish":
        try:
            with open(env.get("AI_REVIEW_RESULT", ""), encoding="utf-8") as f:
                text = f.read()
        except OSError:
            text = ""
        pub = stage_publish(env, text, gh_factory, lambda: gp.LiveGitHubWriter(token))
        status, reason, line = pub.status, pub.reason, pub.line()
    else:
        print("usage: review_pipeline.py prepare|review|publish", file=sys.stderr)
        return 1
    print(line)
    _summary(env, stage, status, reason)
    return EXIT.get(status, 1)


if __name__ == "__main__":
    sys.exit(main())
