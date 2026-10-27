"""정산 근거(Evidence) 암호화 왕복 테스트 — 워크플로의 실제 `run:` 스크립트를 그대로 실행합니다.

trade_base.yml 의 '정산 근거 암호화' 단계와 verify.yml 의 '정산 근거 복호화' 단계를
YAML 에서 꺼내 임시 디렉터리에서 bash 로 실행합니다 (GitHub 표현식은 env 로 대체).
거래 워크플로·브로커 API·네트워크는 사용하지 않습니다. 워크플로 스크립트가 바뀌면 이 테스트가 따라갑니다.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
RAW_ODNO = "0000039445"
PASSPHRASE = "test-pass-phrase-not-a-real-secret"

pytestmark = pytest.mark.skipif(
    shutil.which("gpg") is None or shutil.which("tar") is None,
    reason="gpg/tar 없음",
)


def _step_script(workflow, step_name):
    doc = yaml.safe_load((WORKFLOWS / workflow).read_text(encoding="utf-8"))
    for job in doc["jobs"].values():
        for step in job.get("steps", []):
            if step.get("name") == step_name:
                return step["run"], step.get("env", {})
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
def workspace(tmp_path):
    """거래 job 작업 디렉터리: 평문 근거 파일 2개 (원문 주문번호 포함)."""
    work = tmp_path / "work"
    work.mkdir()
    (work / "settlement-TQQQ-20261026T000000Z.json").write_text(
        json.dumps({"symbol": "TQQQ", "skipped_unverified": [{"odno": RAW_ODNO}]}),
        encoding="utf-8",
    )
    (work / "final-settlement-1.json").write_text(json.dumps({"run_id": "1"}), encoding="utf-8")
    return work


def _encrypt(work, gnupg_home, passphrase=PASSPHRASE):
    script, _env = _step_script("trade_base.yml", "정산 근거 암호화")
    return _run(script, work, passphrase, gnupg_home)


def _download(work, tmp_path):
    """verify job: download-artifact 가 evidence/ 에 암호화본을 내려받는 동작을 흉내냅니다."""
    verify = tmp_path / "verify"
    (verify / "evidence").mkdir(parents=True)
    shutil.copy(work / "evidence.tgz.gpg", verify / "evidence" / "evidence.tgz.gpg")
    return verify


def test_roundtrip_restores_exact_files_and_hides_raw_odno(workspace, gnupg_home, tmp_path):
    result = _encrypt(workspace, gnupg_home)
    assert result.returncode == 0, result.stderr
    cipher = workspace / "evidence.tgz.gpg"
    assert cipher.stat().st_size > 0
    assert RAW_ODNO.encode() not in cipher.read_bytes(), "암호문에 평문 주문번호가 보입니다"
    assert not (workspace / "evidence.tgz").exists(), "중간 평문 tgz 가 남았습니다"
    assert PASSPHRASE not in result.stdout + result.stderr

    verify = _download(workspace, tmp_path)
    script, _env = _step_script("verify.yml", "정산 근거 복호화")
    dec = _run(script, verify, PASSPHRASE, gnupg_home)
    assert dec.returncode == 0, dec.stderr
    for name in ("settlement-TQQQ-20261026T000000Z.json", "final-settlement-1.json"):
        assert (verify / "evidence" / name).read_bytes() == (workspace / name).read_bytes()
    assert PASSPHRASE not in dec.stdout + dec.stderr


def test_upload_path_only_contains_ciphertext(workspace):
    doc = yaml.safe_load((WORKFLOWS / "trade_base.yml").read_text(encoding="utf-8"))
    uploads = [
        s for job in doc["jobs"].values() for s in job.get("steps", [])
        if str(s.get("uses", "")).startswith("actions/upload-artifact")
        and str(s.get("with", {}).get("name", "")).startswith("settlement-evidence")
    ]
    assert len(uploads) == 1
    assert uploads[0]["with"]["path"].strip() == "evidence.tgz.gpg", "평문 파일이 업로드 경로에 있습니다"


def test_missing_passphrase_fails_without_ciphertext(workspace, gnupg_home):
    result = _encrypt(workspace, gnupg_home, passphrase="")
    assert result.returncode != 0
    assert not (workspace / "evidence.tgz.gpg").exists(), "패스프레이즈 없이 산출물이 만들어졌습니다"


def test_wrong_passphrase_fails_to_decrypt(workspace, gnupg_home, tmp_path):
    assert _encrypt(workspace, gnupg_home).returncode == 0
    verify = _download(workspace, tmp_path)
    script, _env = _step_script("verify.yml", "정산 근거 복호화")
    dec = _run(script, verify, "wrong-passphrase", gnupg_home)
    assert dec.returncode != 0
    assert not list((verify / "evidence").glob("settlement-*.json"))


def test_missing_decrypt_passphrase_fails_closed(workspace, gnupg_home, tmp_path):
    assert _encrypt(workspace, gnupg_home).returncode == 0
    verify = _download(workspace, tmp_path)
    script, _env = _step_script("verify.yml", "정산 근거 복호화")
    assert _run(script, verify, "", gnupg_home).returncode != 0


def test_corrupted_ciphertext_fails(workspace, gnupg_home, tmp_path):
    assert _encrypt(workspace, gnupg_home).returncode == 0
    verify = _download(workspace, tmp_path)
    cipher = verify / "evidence" / "evidence.tgz.gpg"
    data = bytearray(cipher.read_bytes())
    data[len(data) // 2] ^= 0xFF
    cipher.write_bytes(bytes(data))
    script, _env = _step_script("verify.yml", "정산 근거 복호화")
    assert _run(script, verify, PASSPHRASE, gnupg_home).returncode != 0


def test_no_evidence_files_is_warning_only(tmp_path, gnupg_home):
    empty = tmp_path / "empty"
    empty.mkdir()
    result = _encrypt(empty, gnupg_home)
    assert result.returncode == 0
    assert not (empty / "evidence.tgz.gpg").exists()
