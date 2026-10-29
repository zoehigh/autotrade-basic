"""주문번호 매핑 원장(odno-map) + Evidence 암호화 왕복 테스트.

공개 로그에는 `ord#…` 가명 토큰만 남습니다. 그 토큰을 원문으로 되돌리는 근거는
**비공개(암호화) Evidence** 뿐입니다:
  - `settlement-*.json` 의 `log_salt` (이번 실행 salt)
  - `odno-map-*.jsonl` 의 (토큰 → 원문) 매핑 행
워크플로의 실제 `run:` 스크립트를 그대로 실행해, 이 둘이 함께 암호화되고 복호화 후에도
모든 공개 토큰이 원문으로 해소되는지 확인합니다 (gpg/tar 없으면 skip — 기존 관례).

브로커 API·네트워크는 사용하지 않습니다.
"""
import json
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT, ROOT / "src", ROOT / "ops"):
    sys.path.insert(0, str(_path))

import log_mask  # noqa: E402
import state  # noqa: E402
import verify_run  # noqa: E402

WORKFLOWS = ROOT / ".github" / "workflows"
PASSPHRASE = "test-pass-phrase-not-a-real-secret"
RAW_ODNO = "0000039445"
RAW_COMPOSITE = "20261028:0000012390"

pytestmark = pytest.mark.skipif(
    shutil.which("gpg") is None or shutil.which("tar") is None,
    reason="gpg/tar 없음",
)


# ── 워크플로 스크립트 실행 (기존 Evidence 테스트 관례와 동일) ─────────
def _step_script(workflow, step_name):
    doc = yaml.safe_load((WORKFLOWS / workflow).read_text(encoding="utf-8"))
    for job in doc["jobs"].values():
        for step in job.get("steps", []):
            if step.get("name") == step_name:
                return step["run"]
    raise AssertionError(f"{workflow} 에 '{step_name}' 단계가 없습니다")


def _run(script, cwd, passphrase, gnupg_home):
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "HOME": str(cwd),
        "GNUPGHOME": str(gnupg_home),
        "PASSPHRASE": passphrase,
    }
    return subprocess.run(
        ["bash", "-c", script], cwd=cwd, env=env, capture_output=True, text=True, timeout=60,
    )


@pytest.fixture
def gnupg_home(tmp_path):
    home = tmp_path / "gnupg"
    home.mkdir(mode=0o700)
    return home


@pytest.fixture
def ledger_masks(monkeypatch):
    """이 테스트 전용 salt·원장으로 격리합니다 (다른 테스트의 마스킹과 섞이지 않게)."""
    monkeypatch.setattr(log_mask, "_LEDGER", {})
    return log_mask


# ═══════════════════════════════════════════════════════════════════
# 원장(ledger) 자체
# ═══════════════════════════════════════════════════════════════════
def test_empty_ledger_writes_no_file(tmp_path, ledger_masks):
    target = tmp_path / "odno-map-run1.jsonl"
    assert log_mask.flush_odno_ledger(str(target)) is None
    assert not target.exists(), "매핑이 없는데 원장 파일이 만들어졌습니다"


def test_ledger_rows_are_0600_and_keep_composite_whole(tmp_path, ledger_masks, monkeypatch):
    monkeypatch.setenv("RUN_ID", "run42")
    monkeypatch.setenv("RETRY_NUM", "2")
    monkeypatch.setenv("BROKER", "meritz")
    token_plain = log_mask.mask_odno(RAW_ODNO)
    token_composite = log_mask.mask_odno(RAW_COMPOSITE)

    target = tmp_path / "odno-map-run42.jsonl"
    assert log_mask.flush_odno_ledger(str(target)) == str(target)

    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    rows = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()]
    by_token = {row["token"]: row for row in rows}
    assert by_token[token_plain]["odno_raw"] == RAW_ODNO
    # 합성번호(YYYYMMDD:주문번호)는 쪼개지 않고 한 행으로 남깁니다
    assert by_token[token_composite]["odno_raw"] == RAW_COMPOSITE
    assert by_token[token_composite]["odno_normalized"] == RAW_COMPOSITE
    assert all(row["run_id"] == "run42" and row["retry_num"] == "2" and row["broker"] == "meritz"
               for row in rows)
    # 공개 로그에 원문이 없듯, 토큰도 원문을 포함하지 않습니다
    assert RAW_ODNO not in token_plain and RAW_COMPOSITE not in token_composite


def test_ledger_flush_failure_prints_counts_only(tmp_path, ledger_masks, capsys):
    log_mask.mask_odno(RAW_ODNO)
    # 디렉터리를 경로로 주면 파일 생성이 실패합니다 (권한/경로 오류 경로)
    assert log_mask.flush_odno_ledger(str(tmp_path)) is None
    out = capsys.readouterr().out
    assert RAW_ODNO not in out
    assert "원장 기록 실패" in out and "건" in out


# ═══════════════════════════════════════════════════════════════════
# Evidence 암호화 왕복 — 공개 토큰 해소
# ═══════════════════════════════════════════════════════════════════
@pytest.fixture
def workspace(tmp_path, ledger_masks, monkeypatch):
    """거래 job 작업 디렉터리: 정산 근거 + 주문번호 매핑 원장(평문)."""
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv("RUN_ID", "run42")

    token = log_mask.mask_odno(RAW_ODNO)
    composite_token = log_mask.mask_odno(RAW_COMPOSITE)

    settlement = {
        "symbol": "TQQQ",
        "run_id": "run42",
        "log_salt": state.log_salt_hex(),
        # 공개 로그에 남는 값(가명 토큰) 과 비공개 Evidence 원문이 함께 있습니다
        "skipped_unverified": [
            {"odno": RAW_ODNO, "odno_hash": token, "reason": "marker missing"},
        ],
        "reverse_order_ids_masked": [composite_token],
        # 공개 Actions 로그에 그대로 찍히는 형태의 메시지 (검증기가 마스킹할 대상)
        "problems": [f"체결금액 미확정: odno={RAW_ODNO}"],
    }
    (work / "settlement-TQQQ-20261028T000000Z.json").write_text(
        json.dumps(settlement, ensure_ascii=False), encoding="utf-8"
    )
    assert log_mask.flush_odno_ledger(str(work / "odno-map-run42.jsonl")) is not None
    return work


def _encrypt(work, gnupg_home):
    return _run(_step_script("trade_base.yml", "정산 근거 암호화"), work, PASSPHRASE, gnupg_home)


def _download(work, tmp_path):
    verify = tmp_path / "verify"
    (verify / "evidence").mkdir(parents=True)
    shutil.copy(work / "evidence.tgz.gpg", verify / "evidence" / "evidence.tgz.gpg")
    return verify


def test_roundtrip_resolves_every_public_token(workspace, gnupg_home, tmp_path):
    result = _encrypt(workspace, gnupg_home)
    assert result.returncode == 0, result.stderr
    cipher = workspace / "evidence.tgz.gpg"
    assert cipher.stat().st_size > 0
    blob = cipher.read_bytes()
    assert RAW_ODNO.encode() not in blob, "암호문에 평문 주문번호가 보입니다"
    assert RAW_COMPOSITE.encode() not in blob
    # 평문 원장은 암호화 후 남기지 않습니다
    assert not list(workspace.glob("odno-map-*.jsonl")), "평문 매핑 원장이 남았습니다"
    assert not (workspace / "evidence.tgz").exists()

    verify = _download(workspace, tmp_path)
    dec = _run(_step_script("verify.yml", "정산 근거 복호화"), verify, PASSPHRASE, gnupg_home)
    assert dec.returncode == 0, dec.stderr

    evidence_dir = verify / "evidence"
    settlement = json.loads(
        (evidence_dir / "settlement-TQQQ-20261028T000000Z.json").read_text(encoding="utf-8")
    )
    salt = settlement["log_salt"]
    rows = [
        json.loads(line)
        for line in (evidence_dir / "odno-map-run42.jsonl").read_text(encoding="utf-8").splitlines()
    ]

    # 1) 원장의 모든 행이 salt 로 재계산한 값과 일치 (= 공개 로그 토큰 → 원문 해소 가능)
    assert rows, "복호화한 Evidence 에 매핑 원장이 없습니다"
    for row in rows:
        assert verify_run._independent_odno_hash(row["odno_raw"], salt) == row["token"], row

    # 2) 공개 로그/근거에 남는 모든 토큰이 해소됩니다
    public_tokens = {settlement["skipped_unverified"][0]["odno_hash"],
                     *settlement["reverse_order_ids_masked"]}
    resolvable = {row["token"]: row["odno_raw"] for row in rows}
    assert public_tokens <= set(resolvable), public_tokens - set(resolvable)
    assert resolvable[settlement["skipped_unverified"][0]["odno_hash"]] == RAW_ODNO

    # 3) 검증기 출력(공개 로그)은 원문 대신 가명 토큰을 씁니다
    masked = verify_run._mask_problem_messages(settlement["problems"], settlement)
    assert RAW_ODNO not in masked[0]
    assert verify_run._independent_odno_hash(RAW_ODNO, salt) in masked[0]

    # 4) 마스킹 모듈의 토큰과 검증기(독립 구현) 토큰이 같은 salt 에서 일치합니다
    assert log_mask.mask_odno(RAW_ODNO) == verify_run._independent_odno_hash(RAW_ODNO, state.log_salt_hex())


def test_local_fallback_filename_is_unique_per_process(tmp_path):
    """RUN_ID 없으면 프로세스마다 다른 원장 파일 — 연속 진단 덮어쓰기 방지."""
    script = (
        "import sys; sys.path.insert(0, %r); "
        "import log_mask; "
        "print(log_mask.mask_odno(sys.argv[1])); "
        "print(log_mask.flush_odno_ledger())"
    ) % str(ROOT / "src")
    tokens = {}
    for raw in ("first-order", "second-order"):
        env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(ROOT / "src")}
        proc = subprocess.run(
            [sys.executable, "-c", script, raw],
            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60,
        )
        assert proc.returncode == 0, proc.stderr
        lines = proc.stdout.splitlines()
        tokens[raw] = (lines[0], lines[-1])
    files = sorted(tmp_path.glob("odno-map-local-*.jsonl"))
    assert len(files) == 2, f"두 프로세스의 원장이 같은 파일에 덮어써졌습니다: {files}"
    for raw, (token, path) in tokens.items():
        # 서브프로세스가 출력한 상대경로는 그 프로세스의 CWD(tmp_path) 기준입니다.
        rows = [json.loads(line) for line in (tmp_path / Path(path).name).read_text(encoding="utf-8").splitlines()]
        assert rows and all(row["odno_raw"] == raw for row in rows)
        assert rows[0]["token"] == token


def test_same_process_flush_reuses_filename(tmp_path, ledger_masks, monkeypatch):
    """같은 프로세스의 반복 flush는 같은 파일에 누적합니다."""
    monkeypatch.delenv("RUN_ID", raising=False)
    monkeypatch.chdir(tmp_path)
    log_mask.mask_odno("aaa-order")
    first = log_mask.flush_odno_ledger()
    log_mask.mask_odno("bbb-order")
    second = log_mask.flush_odno_ledger()
    assert first == second and first is not None
    assert len(list(tmp_path.glob("odno-map-*.jsonl"))) == 1
    content = Path(first).read_text(encoding="utf-8")
    assert "aaa-order" in content and "bbb-order" in content
