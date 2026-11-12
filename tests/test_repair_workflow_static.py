"""복구 워크플로(repair_reverse_ledger_rewind.yml) 정적 가드 — 안전장치가 빠지지 않았는지 확인합니다.

정적 검사에 더해, 워크플로 안의 암호화 백업·요약 스크립트를 임시 폴더에서 **실제로 실행**해
(브로커·네트워크 없음) 약속한 동작을 확인합니다.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

import pytest
import yaml

_PATH = os.path.join(os.path.dirname(__file__), "..", ".github", "workflows", "repair_reverse_ledger_rewind.yml")


def _wf():
    with open(_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _text():
    with open(_PATH, encoding="utf-8") as f:
        return f.read()


def _step(job, step_id):
    return next(s for s in _wf()["jobs"][job]["steps"] if s.get("id") == step_id)


def _step_named(job, fragment):
    return next(s for s in _wf()["jobs"][job]["steps"] if fragment in s.get("name", ""))


def test_run_스크립트에_표현식을_직접_삽입하지_않는다():
    for name, job in _wf()["jobs"].items():
        for step in job["steps"]:
            run = step.get("run", "")
            assert "${{" not in run, f"{name}: run 안에 ${{{{ }}}} 직접 삽입 금지 (env 로 전달): {step.get('name')}"


def test_main_브랜치_demo_전용():
    text = _text()
    assert "github.ref == 'refs/heads/main'" in text
    assert "-demo" in text and "real" not in text.lower().replace("required", "")
    jobs = _wf()["jobs"]
    for name in ("preview", "apply"):
        assert jobs[name]["if"] == "github.ref == 'refs/heads/main'"
    assert "github.ref == 'refs/heads/main'" in jobs["backup"]["if"]


def test_시크릿은_백업_잡의_암호화_단계에만_있다():
    """봇을 실행하는 잡(preview/apply)에는 시크릿이 없고, 백업 passphrase 는 백업 잡에만 주입됩니다."""
    jobs = _wf()["jobs"]
    for name in ("preview", "approve", "apply"):
        assert "secrets." not in json.dumps(jobs[name], ensure_ascii=False), f"{name} 잡에 시크릿 금지"
    backup = json.dumps(jobs["backup"], ensure_ascii=False)
    assert re.findall(r"secrets\.[A-Za-z0-9_]+", backup) == ["secrets.STATE_BACKUP_PASSPHRASE"]
    env = _step("backup", "encrypt")["env"]
    assert env == {"PASSPHRASE": "${{ secrets.STATE_BACKUP_PASSPHRASE }}"}, "passphrase 는 암호화 단계 env 에만"
    # 워크플로 전역 env·다른 단계에는 없어야 합니다
    assert "secrets." not in json.dumps(_wf().get("env", {}))
    for step in jobs["backup"]["steps"]:
        if step.get("id") != "encrypt":
            assert "secrets." not in json.dumps(step, ensure_ascii=False), f"암호화 외 단계에 시크릿 금지: {step.get('name')}"


def test_백업_잡은_봇을_실행하지_않고_캐시도_저장하지_않는다():
    backup = _wf()["jobs"]["backup"]
    blob = json.dumps(backup, ensure_ascii=False)
    assert "trading_bot.py" not in blob and "uv " not in blob, "백업 잡에서 봇/의존성 코드를 실행하지 않습니다"
    assert "actions/cache/save" not in blob
    assert "actions/checkout" not in blob, "저장소 코드를 가져오지 않습니다 (시크릿이 있는 잡)"


def test_승인_게이트와_적용_순서():
    jobs = _wf()["jobs"]
    assert jobs["approve"]["environment"] == "state-repair"
    assert jobs["approve"]["needs"] == "preview"
    assert set(jobs["backup"]["needs"]) == {"preview", "approve"}
    # 백업 성공이 적용의 선행 조건입니다 (approve 도 함께 요구)
    assert set(jobs["apply"]["needs"]) == {"approve", "backup"}
    # 백업 잡의 기본 조건(success)을 깨는 always()/failure() 같은 조건이 apply 에 없어야 합니다
    assert jobs["apply"]["if"] == "github.ref == 'refs/heads/main'"


def test_거래와_같은_concurrency_그룹():
    jobs = _wf()["jobs"]
    for name in ("preview", "apply"):
        group = jobs[name]["concurrency"]["group"]
        assert group == "trade-${{ inputs.broker }}-demo"
        assert jobs[name]["concurrency"]["cancel-in-progress"] is False
    assert "concurrency" not in jobs["backup"], "백업 잡은 캐시를 읽기만 하므로 거래 그룹에 넣지 않습니다"


def test_캐시_저장은_독립_검증_뒤에만():
    steps = _wf()["jobs"]["apply"]["steps"]
    names = [s.get("name", "") for s in steps]
    verify_idx = next(i for i, n in enumerate(names) if "독립 검증" in n)
    save_idx = next(i for i, s in enumerate(steps) if str(s.get("uses", "")).startswith("actions/cache/save"))
    assert verify_idx < save_idx
    assert "if" not in steps[save_idx], "저장 단계에 always() 같은 조건을 달면 검증 실패 후에도 저장될 수 있습니다"
    # 미리보기 job 은 캐시를 저장하지 않음
    assert not any(str(s.get("uses", "")).startswith("actions/cache/save") for s in _wf()["jobs"]["preview"]["steps"])
    # 저장은 apply 잡의 한 단계뿐
    saves = [(j, s) for j, job in _wf()["jobs"].items() for s in job["steps"]
             if str(s.get("uses", "")).startswith("actions/cache/save")]
    assert [j for j, _ in saves] == ["apply"]
    # 저장 뒤에 새 키가 실제로 존재하는지 확인합니다 (cache/save 는 실패해도 경고만 남기고 성공할 수 있음)
    ids = [s.get("id") for s in steps]
    assert ids.index("save") < ids.index("confirm_saved")
    assert "if" not in _step("apply", "confirm_saved")
    assert "저장 결과 안내" in steps[ids.index("confirm_saved") + 1].get("name", ""), "안내는 확인 뒤에만 출력"


def test_최신_캐시_확인과_sha_확인_존재():
    text = _text()
    assert text.count("--sort created_at --order desc") == 2  # preview + apply 재확인
    assert text.count("--ref refs/heads/main") == 3  # 최신 확인 2회(preview/apply) + 저장 후 존재 확인 1회, 모두 복원과 같은 main 범위
    assert "odno" not in text.split("jobs:")[0].split("inputs:")[1].lower(), "주문번호 원문을 공개 입력으로 받지 않습니다"
    assert text.count("sha256sum -c -") >= 3
    assert "fail-on-cache-miss: true" in text
    # 백업도 apply 가 수정할 파일과 같은 SHA-256 을 확인한 뒤 암호화합니다
    assert 'sha256sum -c -' in _step("backup", "encrypt")["run"]


def test_복구_동작_선택과_워터마크_입력():
    wf = _wf()
    inputs = wf[True]["workflow_dispatch"]["inputs"]
    assert inputs["action"]["options"] == ["REVERSE_LEDGER_REWIND", "REVERSE_WATERMARK_RESTORE"]
    assert "target_last_updated" in inputs
    assert "target_last_ordno" not in inputs, "주문번호 원문을 공개 입력으로 받지 않습니다 (원장에서 도출)"
    text = _text()
    assert text.count("REPAIR_ACTION: ${{ inputs.action }}") == 2  # preview + apply 동일 동작
    assert "--mode watermark" in text


def test_evidence_run_id_입력과_검증():
    wf = _wf()
    inputs = wf[True]["workflow_dispatch"]["inputs"]
    assert inputs["evidence_run_id"]["required"] is False  # 되돌리기에서는 선택, 워터마크 복원에서만 필수(스크립트 검증)
    assert wf["env"]["IN_EVIDENCE_RUN"] == "${{ inputs.evidence_run_id }}"
    run = _step_named("preview", "입력 형식 검증")["run"]
    assert 'IN_ACTION" == "REVERSE_WATERMARK_RESTORE"' in run
    assert "IN_EVIDENCE_RUN" in run and "^[0-9]{1,20}$" in run
    assert "evidence_run_id" in run


def test_절차_문서화_두_번_실행():
    inputs = _wf()[True]["workflow_dispatch"]["inputs"]
    assert "다시 실행" in inputs["confirm"]["description"]
    text = _text()
    assert "두 번 실행" in text and "Required reviewers 가 없으면" in text
    assert "다음 거래 슬롯" in text


def test_감사_요약은_실패해도_남고_검증과_저장을_구분한다():
    steps = _wf()["jobs"]["apply"]["steps"]
    summary = steps[-1]
    assert summary["if"] == "always()"
    env = summary["env"]
    assert env["REPAIR_OUTCOME"] == "${{ steps.repair.outcome }}"
    assert env["VERIFY_OUTCOME"] == "${{ steps.verify.outcome }}"
    assert env["SAVE_OUTCOME"] == "${{ steps.save.outcome }}"
    assert env["CONFIRM_OUTCOME"] == "${{ steps.confirm_saved.outcome }}"
    assert env["AUDIT_HASH_OUTCOME"] == "${{ steps.audit_hashes.outcome }}"
    assert env["BACKUP_RESULT"] == "${{ needs.backup.result }}"
    ids = {s.get("id") for s in steps}
    assert {"repair", "verify", "save", "confirm_saved", "audit_hashes"} <= ids
    assert "GITHUB_STEP_SUMMARY" in summary["run"]
    # 원문 주문번호 값을 요약에 쓰지 않습니다 (변경 여부만)
    assert "last_processed_ordno 변경 여부" in summary["run"]


def test_백업_artifact_는_암호화본만_30일_보관():
    up = _step("backup", "upload")
    assert up["uses"].startswith("actions/upload-artifact@")
    assert up["with"]["retention-days"] == 30
    assert up["with"]["if-no-files-found"] == "error"
    assert "${{ github.run_id }}" in up["with"]["name"]
    assert "${{ github.run_attempt }}" in up["with"]["name"], "Re-run all jobs 때 artifact 이름 충돌 방지"
    paths = [p.strip() for p in up["with"]["path"].splitlines() if p.strip()]
    assert paths and all(p.startswith("repair-backup-") for p in paths)
    assert all("${{ github.run_attempt }}" in p for p in paths)
    assert "GITHUB_RUN_ATTEMPT" in _step("backup", "encrypt")["run"]
    assert all(p.endswith((".gpg", ".state.sha256", ".gpg.sha256")) for p in paths), "평문 state 는 올리지 않습니다"
    assert not any(p.endswith(".json") for p in paths)
    run = _step("backup", "encrypt")["run"]
    assert "--symmetric" in run and "cmp .state.json roundtrip.state.json" in run
    # 업로드 단계가 암호화 단계보다 뒤
    names = [s.get("id") for s in _wf()["jobs"]["backup"]["steps"]]
    assert names.index("encrypt") < names.index("upload")


# ── 동적 검사: 워크플로 스크립트를 임시 폴더에서 실제로 실행 ─────────────────────────

def _gpg_usable():
    """gpg 가 있고 (gpg-agent 포함) 임시 홈에서 대칭 암호화가 실제로 되는지 확인합니다.

    샌드박스처럼 gpg-agent 를 띄울 수 없는 환경에서는 테스트를 실패시키지 않고 건너뜁니다.
    """
    if shutil.which("gpg") is None or shutil.which("bash") is None:
        return False
    import tempfile

    with tempfile.TemporaryDirectory() as home:
        os.chmod(home, 0o700)
        probe = subprocess.run(
            ["gpg", "--batch", "--yes", "--pinentry-mode", "loopback", "--passphrase", "x", "--symmetric",
             "--output", os.path.join(home, "p.gpg"), "/dev/null"],
            env={**os.environ, "GNUPGHOME": home}, capture_output=True, timeout=30,
        )
        return probe.returncode == 0


needs_gpg = pytest.mark.skipif(not _gpg_usable(), reason="gpg/gpg-agent 를 쓸 수 없는 환경")

_STATE = {
    "TQQQ": {"T": 18.0, "net_invested": 100.0, "last_updated": "2026-11-05T19:20:00+00:00",
             "LEAKCHECK": "LEAKCHECK-7f3a91"},
    "SOXL": {"T": 14.4375, "net_invested": 200.0, "last_updated": "2026-11-06T19:20:00+00:00"},
}


def _sh(script, cwd, env):
    full = {**os.environ, **env}
    return subprocess.run(["bash", "-c", script], cwd=str(cwd), env=full, capture_output=True, text=True, timeout=60)


def _write_state(path, data=None):
    body = json.dumps(_STATE if data is None else data).encode()
    path.write_bytes(body)
    return hashlib.sha256(body).hexdigest()


def _gpg_env(tmp_path):
    home = tmp_path / "gnupg"
    home.mkdir(exist_ok=True)
    home.chmod(0o700)  # gpg 는 홈 디렉터리 권한이 느슨하면 경고합니다
    return {"GNUPGHOME": str(home)}


def _backup_env(tmp_path, sha, passphrase="test-pass"):
    return {**_gpg_env(tmp_path), "IN_FILE_SHA": sha, "IN_BROKER": "nhplug", "GITHUB_RUN_ID": "4242", "GITHUB_RUN_ATTEMPT": "2", "PASSPHRASE": passphrase}


@needs_gpg
def test_동적_백업_성공_암호문_왕복과_평문_미노출(tmp_path):
    sha = _write_state(tmp_path / ".state.json")
    script = _step("backup", "encrypt")["run"]
    r = _sh(script, tmp_path, _backup_env(tmp_path, sha))
    assert r.returncode == 0, r.stderr
    gpg = tmp_path / "repair-backup-nhplug-4242-2.gpg"
    assert gpg.exists() and gpg.stat().st_size > 0
    assert (tmp_path / "repair-backup-nhplug-4242-2.state.sha256").read_text().strip() == sha
    assert (tmp_path / "repair-backup-nhplug-4242-2.gpg.sha256").read_text().strip() == hashlib.sha256(gpg.read_bytes()).hexdigest()
    # 암호문에는 평문이 없고, 로그에도 state 값이 나오지 않습니다
    assert b"LEAKCHECK" not in gpg.read_bytes()
    assert "LEAKCHECK" not in r.stdout + r.stderr
    # 올바른 passphrase 로만 복호화됩니다
    ok = subprocess.run(
        ["gpg", "--batch", "--quiet", "--pinentry-mode", "loopback", "--passphrase", "test-pass", "-d", str(gpg)],
        capture_output=True, env={**os.environ, **_gpg_env(tmp_path)}, timeout=60,
    )
    assert ok.returncode == 0 and json.loads(ok.stdout) == _STATE
    # 평문 임시 파일(roundtrip)은 남기지 않습니다
    assert not (tmp_path / "roundtrip.state.json").exists()


@needs_gpg
def test_동적_백업은_sha_불일치_빈_passphrase_잘못된_json에서_실패하고_산출물이_없다(tmp_path):
    script = _step("backup", "encrypt")["run"]
    sha = _write_state(tmp_path / ".state.json")

    bad_sha = "0" * 64
    r = _sh(script, tmp_path, _backup_env(tmp_path, bad_sha))
    assert r.returncode != 0 and not list(tmp_path.glob("repair-backup-*"))

    r = _sh(script, tmp_path, _backup_env(tmp_path, sha, passphrase=""))
    assert r.returncode != 0 and not list(tmp_path.glob("repair-backup-*"))
    assert "백업할 수 없습니다" in r.stdout + r.stderr  # 복구를 진행하지 않는다는 안내

    (tmp_path / ".state.json").write_text("{not json")
    sha2 = hashlib.sha256((tmp_path / ".state.json").read_bytes()).hexdigest()
    r = _sh(script, tmp_path, _backup_env(tmp_path, sha2))
    assert r.returncode != 0 and not list(tmp_path.glob("repair-backup-*"))


def _backup_summary(tmp_path, encrypt, upload):
    step = _step_named("backup", "백업 결과 요약")
    summary = tmp_path / "summary.md"
    summary.unlink(missing_ok=True)  # 요약은 이어쓰기(>>)라 호출마다 비워야 이전 결과가 섞이지 않습니다
    r = _sh(step["run"], tmp_path, {
        "ENCRYPT_OUTCOME": encrypt, "UPLOAD_OUTCOME": upload, "IN_BROKER": "nhplug",
        "GITHUB_RUN_ID": "4242", "GITHUB_RUN_ATTEMPT": "2", "GITHUB_STEP_SUMMARY": str(summary),
    })
    assert r.returncode == 0, r.stderr
    return summary.read_text()


def test_동적_백업_요약_성공과_실패(tmp_path):
    ok = _backup_summary(tmp_path, "success", "success")
    assert "백업 성공" in ok and "repair-backup-nhplug-4242-2" in ok
    fail = _backup_summary(tmp_path, "failure", "skipped")
    assert "백업 실패" in fail and "복구는 진행되지 않습니다" in fail
    assert "백업 성공" not in fail
    # 업로드만 실패해도 성공으로 보고하지 않습니다
    assert "백업 실패" in _backup_summary(tmp_path, "success", "failure")


def _apply_summary(tmp_path, repair, verify, save, confirm="success", *, with_before=True, audit=None, input_sha="a" * 64):
    rt = tmp_path / "rt"
    rt.mkdir(exist_ok=True)
    before = {"SOXL": {"T": 14.4375, "net_invested": 100.0, "last_updated": "2026-11-06T19:20:00+00:00",
                       "last_processed_ordno": "RAWODNO-111"}}
    after = {"SOXL": {"T": 17.325, "net_invested": 150.5, "last_updated": "2026-11-05T19:20:00+00:00",
                      "last_processed_ordno": "RAWODNO-222"}}
    if with_before:
        (rt / "state.before.json").write_text(json.dumps(before))
    (tmp_path / ".state.json").write_text(json.dumps(after))
    audit_file = rt / "audit_hashes.json"
    audit_file.unlink(missing_ok=True)
    if audit is not None:
        audit_file.write_text(json.dumps(audit))
    summary = tmp_path / "summary.md"
    summary.unlink(missing_ok=True)
    step = _wf()["jobs"]["apply"]["steps"][-1]
    r = _sh(step["run"], tmp_path, {
        "REPAIR_OUTCOME": repair, "VERIFY_OUTCOME": verify, "SAVE_OUTCOME": save, "CONFIRM_OUTCOME": confirm,
        "GITHUB_RUN_ATTEMPT": "2", "BACKUP_RESULT": "success",
        "WORKFLOW_SHA": "abc1234", "RUNNER_TEMP": str(rt), "GITHUB_RUN_ID": "777", "GITHUB_STEP_SUMMARY": str(summary),
        "IN_BROKER": "nhplug", "IN_SYMBOL": "SOXL", "IN_ACTION": "REVERSE_WATERMARK_RESTORE",
        "IN_RESTORE_KEY": "state-nhplug-demo-1", "IN_FILE_SHA": input_sha, "IN_HASH": "b" * 64,
        "IN_GUARD_SYMBOL": "TQQQ", "IN_GUARD_HASH": "c" * 64, "IN_EVIDENCE_RUN": "42287486160",
        "AUDIT_HASH_OUTCOME": "success" if audit is not None else "failure",
    })
    assert r.returncode == 0, r.stderr
    return summary.read_text()


def test_동적_감사_요약_검증_통과와_저장_성공을_구분한다(tmp_path):
    all_ok = _apply_summary(tmp_path, "success", "success", "success", "success")
    assert "새 캐시 저장 확인" in all_ok and "state-nhplug-demo-777" in all_ok
    assert "| 독립 검증 | success |" in all_ok and "| 캐시 저장 단계 | success |" in all_ok
    assert "| 새 캐시 존재 확인 | success |" in all_ok
    assert "repair-backup-nhplug-777-2" in all_ok  # 백업 artifact 이름에 시도 번호
    assert "14.4375 → 17.325" in all_ok and "100.0 → 150.5" in all_ok
    assert "42287486160" in all_ok and "abc1234" in all_ok

    save_failed = _apply_summary(tmp_path, "success", "success", "failure", "skipped")
    assert "캐시 저장 단계가 실패" in save_failed and "저장 완료를 확인하지 못했으므로" in save_failed
    assert "캐시 목록" in save_failed
    assert "운영 캐시는 복구 전 그대로" not in save_failed, "저장 실패가 캐시 미생성을 보장하지 않으므로 단정하지 않습니다"
    assert "새 캐시 저장 확인" not in save_failed
    assert "| 독립 검증 | success |" in save_failed and "| 캐시 저장 단계 | failure |" in save_failed

    # cache/save 가 경고만 남기고 끝난 경우(step 은 success, 새 키는 없음)를 저장 성공으로 보고하지 않습니다
    unconfirmed = _apply_summary(tmp_path, "success", "success", "success", "failure")
    assert "확인하지 못했습니다" in unconfirmed and "새 캐시 저장 확인" not in unconfirmed
    assert "| 캐시 저장 단계 | success |" in unconfirmed and "| 새 캐시 존재 확인 | failure |" in unconfirmed

    verify_failed = _apply_summary(tmp_path, "success", "failure", "skipped", "skipped")
    assert "통과하지 못해" in verify_failed and "캐시 저장을 시도하지 않았습니다" in verify_failed
    assert "| 캐시 저장 단계 | skipped |" in verify_failed

    # 앞 단계에서 중단돼 outcome 이 비어 있어도 요약은 남습니다 (사본도 없을 수 있음)
    early = _apply_summary(tmp_path, "", "", "", "", with_before=False)
    assert "미실행" in early and "통과하지 못해" in early


def test_동적_감사_요약은_주문번호_원문을_쓰지_않는다(tmp_path):
    out = _apply_summary(tmp_path, "success", "success", "success")
    assert "RAWODNO" not in out
    assert "last_processed_ordno 변경 여부" in out and "| 예 |" in out


def _preview_summary(tmp_path, *, action, mode, outcome="success"):
    (tmp_path / ".state.json").write_text(json.dumps(
        {"SOXL": {"T": 14.4375, "net_invested": 100.0, "last_updated": "2026-11-06T19:20:00+00:00"}}))
    summary = tmp_path / "preview.md"
    summary.unlink(missing_ok=True)
    step = _step_named("preview", "미리보기 요약")
    r = _sh(step["run"], tmp_path, {
        "DRYRUN_OUTCOME": outcome, "MODE": mode, "GITHUB_STEP_SUMMARY": str(summary),
        "IN_BROKER": "nhplug", "IN_SYMBOL": "SOXL", "IN_ACTION": action, "IN_RESTORE_KEY": "state-nhplug-demo-1",
        "IN_T": "14.4375", "IN_TARGET_UPDATED": "2026-11-04T19:10:00+00:00", "IN_EVIDENCE_RUN": "42287486160",
    })
    assert r.returncode == 0, r.stderr
    return summary.read_text()


def test_동적_미리보기_요약_현재값과_목표값_그리고_다음_절차(tmp_path):
    wm = _preview_summary(tmp_path, action="REVERSE_WATERMARK_RESTORE", mode="preview")
    assert "2026-11-06T19:20:00+00:00" in wm and "2026-11-04T19:10:00+00:00" in wm  # 현재값·목표값 나란히
    assert "42287486160" in wm and "watermark_before" in wm and "직접 대조" in wm
    assert "미리보기만 하고 끝납니다" in wm and "confirm" in wm and "다음 거래 슬롯 전에" in wm

    applying = _preview_summary(tmp_path, action="REVERSE_LEDGER_REWIND", mode="apply")
    assert "승인 게이트 → 백업 → 적용" in applying
    assert "미리보기만 하고 끝납니다" not in applying
    assert "watermark_before" not in applying  # 되돌리기에는 워터마크 대조 안내 없음


# ── 입력 형식 검증 스크립트 실행 ───────────────────────────────────────────────

_VALID_INPUT = {
    "IN_BROKER": "nhplug", "IN_RESTORE_KEY": "state-nhplug-demo-123", "IN_FILE_SHA": "a" * 64,
    "IN_HASH": "b" * 64, "IN_GUARD_HASH": "c" * 64, "IN_SYMBOL": "SOXL", "IN_GUARD_SYMBOL": "TQQQ",
    "IN_T": "14.4375", "IN_ACTION": "REVERSE_LEDGER_REWIND", "IN_TARGET_UPDATED": "", "IN_EVIDENCE_RUN": "",
    "IN_CONFIRM": "",
}


def _validate(tmp_path, **over):
    script = _step_named("preview", "입력 형식 검증")["run"]
    r = _sh(script, tmp_path, {**_VALID_INPUT, **over})
    return r.returncode, r.stdout + r.stderr


def test_동적_입력검증_evidence_run_id_규칙(tmp_path):
    target = "2026-11-04T19:10:00+00:00"
    wm = {"IN_ACTION": "REVERSE_WATERMARK_RESTORE", "IN_TARGET_UPDATED": target}
    # 되돌리기: 선택 입력 — 없어도 통과, 있으면 숫자여야 함
    assert _validate(tmp_path)[0] == 0
    assert _validate(tmp_path, IN_EVIDENCE_RUN="42287486160")[0] == 0
    assert _validate(tmp_path, IN_EVIDENCE_RUN="abc")[0] != 0
    # 워터마크 복원: 필수 + 숫자 (1~20자리)
    rc, out = _validate(tmp_path, **wm)
    assert rc != 0 and "evidence_run_id" in out
    assert _validate(tmp_path, **wm, IN_EVIDENCE_RUN="12x")[0] != 0
    assert _validate(tmp_path, **wm, IN_EVIDENCE_RUN="1" * 21)[0] != 0
    assert _validate(tmp_path, **wm, IN_EVIDENCE_RUN="42287486160")[0] == 0
    # 기존 규칙 유지: 목표 시각 형식
    assert _validate(tmp_path, IN_ACTION="REVERSE_WATERMARK_RESTORE", IN_TARGET_UPDATED="2026-11-04",
                     IN_EVIDENCE_RUN="1")[0] != 0


# ── 백업은 조건이 안 맞으면 암호화(gpg) 호출 전에 중단합니다 ───────────────────────────

def _fake_gpg(tmp_path):
    """gpg 대신 호출 기록만 남기는 스텁 — 실패 조건에서 gpg 가 아예 불리지 않는지 확인합니다."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "gpg"
    stub.write_text(f'#!/bin/bash\ntouch "{tmp_path}/GPG_CALLED"\nexit 0\n')
    stub.chmod(0o755)
    return {"PATH": f"{bin_dir}:{os.environ['PATH']}"}


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash 없음")
def test_동적_백업은_전제조건_실패시_gpg_호출_전에_중단한다(tmp_path):
    script = _step("backup", "encrypt")["run"]
    env = {**_fake_gpg(tmp_path), "IN_BROKER": "nhplug", "GITHUB_RUN_ID": "9", "GITHUB_RUN_ATTEMPT": "1"}
    good_sha = _write_state(tmp_path / ".state.json")

    cases = [
        ("SHA 불일치", {"IN_FILE_SHA": "0" * 64, "PASSPHRASE": "x"}),
        ("빈 passphrase", {"IN_FILE_SHA": good_sha, "PASSPHRASE": ""}),
    ]
    for label, extra in cases:
        r = _sh(script, tmp_path, {**env, **extra})
        assert r.returncode != 0, label
        assert not (tmp_path / "GPG_CALLED").exists(), f"{label}: 암호화(gpg) 호출 전에 중단해야 합니다"

    (tmp_path / ".state.json").write_text("{not json")
    bad_sha = hashlib.sha256((tmp_path / ".state.json").read_bytes()).hexdigest()
    r = _sh(script, tmp_path, {**env, "IN_FILE_SHA": bad_sha, "PASSPHRASE": "x"})
    assert r.returncode != 0 and not (tmp_path / "GPG_CALLED").exists(), "잘못된 JSON"


def test_동적_미리보기_요약은_앞단계_실패를_통과로_쓰지_않는다(tmp_path):
    skipped = _preview_summary(tmp_path, action="REVERSE_LEDGER_REWIND", mode="", outcome="skipped")
    assert "`skipped`" in skipped and "앞 단계 실패" in skipped
    assert "통과" not in skipped, "건너뛴 단계를 통과로 보고하면 안 됩니다"
    failed = _preview_summary(tmp_path, action="REVERSE_LEDGER_REWIND", mode="", outcome="failure")
    assert "`failure`" in failed and "통과" not in failed


# ── 새 캐시 존재 확인 단계 (cache/save 는 실패해도 성공할 수 있음) ───────────────────────

def _fake_gh(tmp_path, listing, exit_code=0):
    bin_dir = tmp_path / "ghbin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "gh"
    stub.write_text(f"#!/bin/bash\ncat <<'EOF'\n{listing}\nEOF\nexit {exit_code}\n")
    stub.chmod(0o755)
    return {"PATH": f"{bin_dir}:{os.environ['PATH']}"}


def _confirm(tmp_path, listing, exit_code=0):
    script = _step("apply", "confirm_saved")["run"]
    env = {**_fake_gh(tmp_path, listing, exit_code), "IN_BROKER": "nhplug", "GITHUB_RUN_ID": "777",
           "GITHUB_REPOSITORY": "o/r", "CONFIRM_TRIES": "2", "CONFIRM_SLEEP": "0"}
    return _sh(script, tmp_path, env)


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash 없음")
def test_동적_새_캐시_확인_정확한_키가_있을_때만_성공(tmp_path):
    ok = _confirm(tmp_path, "state-nhplug-demo-776\nstate-nhplug-demo-777")
    assert ok.returncode == 0 and "새 캐시 확인" in ok.stdout

    # 키는 접두어 조회 결과이므로 정확히 일치하는 줄만 인정합니다 (777 이 아닌 7770 등은 불인정)
    for listing in ("", "state-nhplug-demo-7770", "state-nhplug-demo-776", "xstate-nhplug-demo-777"):
        bad = _confirm(tmp_path, listing)
        assert bad.returncode != 0, listing
        assert "찾지 못했습니다" in bad.stdout + bad.stderr

    # 조회 자체가 실패해도 성공으로 보지 않습니다
    assert _confirm(tmp_path, "", exit_code=1).returncode != 0


# ── confirm 3경로(미리보기 / BACKUP / REWIND): 게이트 출력과 잡 실행 조건 ─────────────────

def _gate(tmp_path, confirm, symbol="SOXL"):
    out = tmp_path / "gate_out.txt"
    out.write_text("")
    script = "set -e\n" + _step("preview", "gate")["run"]  # GitHub 의 기본 셸(bash -e)과 같은 조건
    r = _sh(script, tmp_path, {"IN_CONFIRM": confirm, "IN_SYMBOL": symbol, "GITHUB_OUTPUT": str(out)})
    assert r.returncode == 0, r.stderr
    return dict(line.split("=", 1) for line in out.read_text().splitlines())


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash 없음")
def test_동적_게이트_confirm_문구별_실행_범위(tmp_path):
    assert _gate(tmp_path, "") == {"mode": "preview", "want_apply": "false", "want_backup": "false"}
    assert _gate(tmp_path, "BACKUP SOXL") == {"mode": "backup", "want_apply": "false", "want_backup": "true"}
    assert _gate(tmp_path, "REWIND SOXL") == {"mode": "apply", "want_apply": "true", "want_backup": "true"}
    # 정확히 일치하지 않으면 미리보기만: 대소문자·공백·다른 종목·접두어/접미어
    for bad in ("backup SOXL", "Rewind SOXL", "BACKUP  SOXL", "BACKUP SOXL ", " REWIND SOXL", "REWIND TQQQ",
                "BACKUP TQQQ", "REWIND", "BACKUP", "REWIND SOXL; echo", "REWIND SOXLX"):
        assert _gate(tmp_path, bad)["mode"] == "preview", bad
    assert _gate(tmp_path, "BACKUP TQQQ", symbol="TQQQ")["mode"] == "backup"


def _eval_expr(text, ctx):
    """워크플로 `if` 식(이 파일이 쓰는 부분집합)을 파이썬으로 옮겨 평가합니다: && || == != ( ) 문자열, 컨텍스트 값."""
    text = text.strip()
    if text.startswith("${{"):
        text = text[3:-2].strip()
    text = text.replace("!cancelled()", "True").replace("always()", "True")
    text = text.replace("&&", " and ").replace("||", " or ")

    def token(m):
        t = m.group(0)
        if t.startswith("'") or t in ("True", "False", "and", "or", "not"):
            return t
        return repr(ctx.get(t, ""))

    py = re.sub(r"'[^']*'|[A-Za-z_][A-Za-z0-9_.]*", token, text)
    return bool(eval(py, {"__builtins__": {}}, {}))  # noqa: S307 — 테스트 전용, 입력은 이 저장소의 워크플로 식


def _runs(job, results, outputs, ref):
    """GitHub 의 잡 실행 규칙을 단순 모사: needs 의 결과가 하나라도 success 가 아니면(skipped 포함) 건너뜀 —
    단, `if` 에 상태 함수(!cancelled()/always())가 있으면 그 식만 따릅니다."""
    needs = job.get("needs") or []
    needs = [needs] if isinstance(needs, str) else list(needs)
    ctx = {"github.ref": ref}
    for n in needs:
        ctx[f"needs.{n}.result"] = results[n]
        for k, v in outputs.get(n, {}).items():
            ctx[f"needs.{n}.outputs.{k}"] = v
    expr = job.get("if")
    all_success = all(results[n] == "success" for n in needs)
    if expr is None:
        return all_success
    text = str(expr)
    has_status = "!cancelled()" in text or "always()" in text
    value = _eval_expr(text, ctx)
    return value if has_status else (value and all_success)


_MODES = {
    "preview": {"mode": "preview", "want_apply": "false", "want_backup": "false"},
    "backup": {"mode": "backup", "want_apply": "false", "want_backup": "true"},
    "apply": {"mode": "apply", "want_apply": "true", "want_backup": "true"},
}


def _simulate(mode, *, ref="refs/heads/main", preview="success", approve="success", backup="success", apply_="success"):
    """잡을 의존 순서대로 평가해 각 잡의 결과(success/skipped/그 잡이 낸 결과)를 돌려줍니다."""
    jobs = _wf()["jobs"]
    outcome = {"preview": preview, "approve": approve, "backup": backup, "apply": apply_, "summary": "success"}
    results, outputs = {}, {}
    for name in ("preview", "approve", "backup", "apply", "summary"):
        ran = _runs(jobs[name], results, outputs, ref)
        results[name] = outcome[name] if ran else "skipped"
        if name == "preview" and results[name] == "success":
            outputs["preview"] = _MODES[mode]
    return results


def test_잡_실행_조건_미리보기만이면_backup_apply_모두_생략():
    r = _simulate("preview")
    assert r == {"preview": "success", "approve": "skipped", "backup": "skipped", "apply": "skipped", "summary": "success"}


def test_잡_실행_조건_BACKUP_모드는_approve_없이_backup_까지만():
    r = _simulate("backup")
    assert r["approve"] == "skipped" and r["backup"] == "success"
    assert r["apply"] == "skipped", "백업 전용 실행에서 apply(복구 적용)가 돌면 안 됩니다"
    # 백업이 실패해도 apply 는 돌지 않고 요약은 남습니다
    r = _simulate("backup", backup="failure")
    assert r["backup"] == "failure" and r["apply"] == "skipped" and r["summary"] == "success"


def test_잡_실행_조건_REWIND_모드는_승인과_백업이_모두_성공해야_apply():
    assert _simulate("apply") == {"preview": "success", "approve": "success", "backup": "success",
                                  "apply": "success", "summary": "success"}
    # 승인 실패·반려: 백업도 하지 않고 apply 도 돌지 않음
    r = _simulate("apply", approve="failure")
    assert r["backup"] == "skipped" and r["apply"] == "skipped" and r["summary"] == "success"
    # 백업 실패: apply 생략 (이 경우에도 최종 요약 잡은 실행됨)
    r = _simulate("apply", backup="failure")
    assert r["approve"] == "success" and r["backup"] == "failure" and r["apply"] == "skipped" and r["summary"] == "success"
    # apply 실패해도 최종 요약은 실행
    assert _simulate("apply", apply_="failure")["summary"] == "success"


def test_잡_실행_조건_미리보기_실패나_main_아님이면_이후_잡_모두_생략():
    r = _simulate("apply", preview="failure")
    assert (r["approve"], r["backup"], r["apply"]) == ("skipped", "skipped", "skipped") and r["summary"] == "success"
    r = _simulate("apply", ref="refs/heads/feature")
    assert r == {"preview": "skipped", "approve": "skipped", "backup": "skipped", "apply": "skipped", "summary": "success"}


def test_최종_요약_잡_구성():
    job = _wf()["jobs"]["summary"]
    assert set(job["needs"]) == {"preview", "approve", "backup", "apply"}
    assert "!cancelled()" in job["if"] or "always()" in job["if"]
    assert "secrets." not in json.dumps(job)
    env = job["steps"][0]["env"]
    for key, expected in {"MODE": "needs.preview.outputs.mode", "PREVIEW_RESULT": "needs.preview.result",
                          "APPROVE_RESULT": "needs.approve.result", "BACKUP_RESULT": "needs.backup.result",
                          "APPLY_RESULT": "needs.apply.result"}.items():
        assert expected in env[key]


def _final_summary(tmp_path, mode, **results):
    summary = tmp_path / "final.md"
    summary.unlink(missing_ok=True)
    env = {"MODE": mode, "GITHUB_STEP_SUMMARY": str(summary), "WORKFLOW_SHA": "abc", "GITHUB_RUN_ID": "9",
           "IN_BROKER": "nhplug", "IN_SYMBOL": "SOXL", "IN_ACTION": "REVERSE_LEDGER_REWIND",
           "PREVIEW_RESULT": "success", "APPROVE_RESULT": "success", "BACKUP_RESULT": "success", "APPLY_RESULT": "success"}
    env.update({k.upper() + "_RESULT": v for k, v in results.items()})
    r = _sh(_wf()["jobs"]["summary"]["steps"][0]["run"], tmp_path, env)
    assert r.returncode == 0, r.stderr
    return summary.read_text()


def test_동적_최종_요약_모든_경로의_판정(tmp_path):
    assert "미리보기만 실행" in _final_summary(tmp_path, "preview", approve="skipped", backup="skipped", apply="skipped")

    ok = _final_summary(tmp_path, "backup", approve="skipped", apply="skipped")
    assert "백업 전용 실행이 완료" in ok and "state 는 바뀌지 않았습니다" in ok and "저장 경로는 이 실행의 확인 대상이 아닙니다" in ok
    bad = _final_summary(tmp_path, "backup", approve="skipped", backup="failure", apply="skipped")
    assert "백업이 성공하지 못했습니다" in bad and "복구는 적용되지 않았고" in bad

    full = _final_summary(tmp_path, "apply")
    assert "적용 잡이 성공" in full
    # 백업 실패로 apply 가 생략되면 그 사실이 요약에 남습니다 (apply 잡 요약은 만들어지지 않음)
    skipped_apply = _final_summary(tmp_path, "apply", backup="failure", apply="skipped")
    assert "백업이 성공하지 못해(failure) 복구를 진행하지 않았습니다" in skipped_apply
    assert "| 적용 | skipped |" in skipped_apply and "| 암호화 백업 | failure |" in skipped_apply
    assert "승인 게이트에서 중단" in _final_summary(tmp_path, "apply", approve="failure", backup="skipped", apply="skipped")
    assert "적용 잡이 성공하지 못했습니다" in _final_summary(tmp_path, "apply", apply="failure")
    assert "미리보기(입력 검증 포함)가 성공하지 못했습니다" in _final_summary(
        tmp_path, "", preview="failure", approve="skipped", backup="skipped", apply="skipped")


def test_동적_미리보기_요약_BACKUP_모드_안내(tmp_path):
    out = _preview_summary(tmp_path, action="REVERSE_LEDGER_REWIND", mode="backup")
    assert "백업만" in out and "state 도 바꾸지 않습니다" in out
    assert "승인 게이트 → 백업 → 적용" not in out
    # 비운 미리보기는 백업 경로가 확인되지 않는다는 점과 BACKUP 모드를 안내합니다
    pv = _preview_summary(tmp_path, action="REVERSE_LEDGER_REWIND", mode="preview")
    assert "백업 경로는 확인되지 않습니다" in pv and "BACKUP <종목>" in pv


def test_절차_문서화_세_가지_실행_범위():
    inputs = _wf()[True]["workflow_dispatch"]["inputs"]
    desc = inputs["confirm"]["description"]
    assert "BACKUP <종목>" in desc and "REWIND <종목>" in desc and "state 변경 없음" in desc
    text = _text()
    assert "처음 병합한 뒤에는 실제 복구 전에" in text and "BACKUP" in text.split("jobs:")[0]
    assert "preview → backup" in text and "preview → approve → backup → apply" in text


# ── 감사용 해시: 별도 단계(continue-on-error)이며 복구·검증 결과를 가리지 않는다 ───────────────

def test_감사용_해시_단계는_별도이고_실패해도_잡을_실패시키지_않는다():
    steps = _wf()["jobs"]["apply"]["steps"]
    ids = [s.get("id") for s in steps]
    audit = _step("apply", "audit_hashes")
    assert audit["continue-on-error"] is True and audit["if"] == "always()"
    assert "ops/repair_audit_hashes.py" in audit["run"]
    assert "save_state" not in audit["run"], "감사용 계산은 state 를 쓰지 않습니다"
    # 복구·검증·저장·존재 확인 단계에는 continue-on-error 를 달지 않습니다 (실패가 가려지면 안 됨)
    for sid in ("repair", "verify", "save", "confirm_saved"):
        assert "continue-on-error" not in _step("apply", sid), sid
    # 저장·확인 뒤에 계산하고, 요약은 그 다음
    assert ids.index("confirm_saved") < ids.index("audit_hashes") < len(steps) - 1
    assert steps[-1]["env"]["AUDIT_HASH_OUTCOME"] == "${{ steps.audit_hashes.outcome }}"


_REC = {"T": 1.0, "last_updated": "2026-11-05T19:20:00+00:00", "effective_seed": 100.0, "net_invested": 50.0,
        "net_invested_status": "valid", "orders_meta": {"1": {"total_qty": 1}}}


def _hash_run(tmp_path, before, after, *, symbol="SOXL", guard="TQQQ"):
    (tmp_path / "before.json").write_text(json.dumps(before))
    (tmp_path / "after.json").write_text(json.dumps(after))
    out = tmp_path / "out.json"
    out.unlink(missing_ok=True)
    script = os.path.join(os.path.dirname(__file__), "..", "ops", "repair_audit_hashes.py")
    r = subprocess.run(
        [sys.executable, script, "--before", str(tmp_path / "before.json"), "--after", str(tmp_path / "after.json"),
         "--symbol", symbol, "--guard", guard, "--out", str(out)],
        capture_output=True, text=True, timeout=60, cwd=os.path.dirname(script),
        env={**os.environ, "TQQQ_SEED": "10000", "SOXL_SEED": "5000"},
    )
    return r, out


def test_감사용_해시_계산_대상은_바뀌고_가드는_그대로(tmp_path):
    before = {"SOXL": dict(_REC), "TQQQ": {**_REC, "T": 9.0}}
    after = {"SOXL": {**_REC, "T": 2.0}, "TQQQ": {**_REC, "T": 9.0}}
    r, out = _hash_run(tmp_path, before, after)
    assert r.returncode == 0, r.stderr
    data = json.loads(out.read_text())
    can = data["canonical_hash"]
    assert can["before"]["SOXL"] != can["after"]["SOXL"], "대상 종목 값이 바뀌면 canonical hash 도 달라져야 합니다"
    assert can["before"]["TQQQ"] == can["after"]["TQQQ"], "가드 종목은 그대로"
    assert re.fullmatch(r"[0-9a-f]{64}", can["before"]["SOXL"])
    assert data["file_sha256"]["before"] == hashlib.sha256((tmp_path / "before.json").read_bytes()).hexdigest()
    assert data["file_sha256"]["after"] == hashlib.sha256((tmp_path / "after.json").read_bytes()).hexdigest()
    # 공개 로그에는 해시 값이 나오지 않고 state 파일은 바뀌지 않았습니다
    assert can["before"]["SOXL"] not in r.stdout + r.stderr
    assert json.loads((tmp_path / "before.json").read_text()) == before
    # 가드 종목이 바뀌면 달라집니다
    after_bad = {"SOXL": {**_REC, "T": 2.0}, "TQQQ": {**_REC, "T": 9.5}}
    r2, out2 = _hash_run(tmp_path, before, after_bad)
    c2 = json.loads(out2.read_text())["canonical_hash"]
    assert c2["before"]["TQQQ"] != c2["after"]["TQQQ"]


def test_감사용_해시_계산은_실패하면_출력파일을_만들지_않는다(tmp_path):
    r, out = _hash_run(tmp_path, {"SOXL": _REC, "TQQQ": _REC}, {"SOXL": _REC, "TQQQ": _REC})
    assert r.returncode == 0 and out.exists()
    # 상태를 읽을 수 없는 파일(잘못된 JSON)
    (tmp_path / "after.json").write_text("{broken")
    script = os.path.join(os.path.dirname(__file__), "..", "ops", "repair_audit_hashes.py")
    out.unlink()
    r = subprocess.run(
        [sys.executable, script, "--before", str(tmp_path / "before.json"), "--after", str(tmp_path / "after.json"),
         "--symbol", "SOXL", "--guard", "TQQQ", "--out", str(out)],
        capture_output=True, text=True, timeout=60, env={**os.environ, "TQQQ_SEED": "10000", "SOXL_SEED": "5000"},
    )
    assert r.returncode == 1 and not out.exists()
    assert "복구·검증 결과와 무관" in r.stderr
    # 입력 파일이 없을 때도 같은 방식으로 실패
    r = subprocess.run(
        [sys.executable, script, "--before", str(tmp_path / "nope.json"), "--after", str(tmp_path / "after.json"),
         "--symbol", "SOXL", "--guard", "TQQQ", "--out", str(out)],
        capture_output=True, text=True, timeout=60, env={**os.environ, "TQQQ_SEED": "10000", "SOXL_SEED": "5000"},
    )
    assert r.returncode == 1 and not out.exists()


_AUDIT = {"canonical_hash": {"before": {"SOXL": "1" * 64, "TQQQ": "3" * 64}, "after": {"SOXL": "2" * 64, "TQQQ": "3" * 64}}}


def test_동적_감사_요약_입력값과_계산값을_구분해_기록한다(tmp_path):
    out = _apply_summary(tmp_path, "success", "success", "success", "success", audit=_AUDIT)
    assert "복원본 파일 SHA-256 — 입력값" in out and "복구 전 사본 계산값" in out and "러너 안 계산값" in out
    assert "입력값과 다름" in out  # 기본 입력값("a"*64)은 사본의 실제 SHA 와 다름
    assert "1" * 64 in out and "2" * 64 in out  # 대상 종목 복구 전/후 canonical hash
    assert "| 가드 종목 불변 | 예 |" in out
    # 입력값이 사본의 실제 SHA 와 같으면 일치로 표시
    sha_before = hashlib.sha256((tmp_path / "rt" / "state.before.json").read_bytes()).hexdigest()
    out2 = _apply_summary(tmp_path, "success", "success", "success", "success", audit=_AUDIT, input_sha=sha_before)
    assert "입력값과 일치" in out2 and "입력값과 다름" not in out2
    # 가드 종목 hash 가 달라지면 눈에 띄게 표시
    bad = {"canonical_hash": {"before": {"SOXL": "1" * 64, "TQQQ": "3" * 64}, "after": {"SOXL": "2" * 64, "TQQQ": "4" * 64}}}
    assert "| 가드 종목 불변 | **아니오** |" in _apply_summary(tmp_path, "success", "success", "success", "success", audit=bad)


def test_동적_감사_요약은_해시_계산_실패가_판정을_바꾸지_않는다(tmp_path):
    ok = _apply_summary(tmp_path, "success", "success", "success", "success", audit=None)
    assert "(계산 실패)" in ok and "| 가드 종목 불변 | 확인 불가 |" in ok
    assert "새 캐시 저장 확인" in ok and "✅" in ok, "해시 계산 실패가 복구·검증·저장 결과 판정을 가리면 안 됩니다"
    assert "감사용 해시 계산 단계: `failure`" in ok
    # 반대로 해시가 정상이어도 검증 실패 판정은 그대로
    failed = _apply_summary(tmp_path, "success", "failure", "skipped", "skipped", audit=_AUDIT)
    assert "통과하지 못해" in failed and "✅" not in failed
