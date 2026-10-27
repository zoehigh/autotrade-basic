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


def _run(script, cwd, passphrase, gnupg_home, shim=None):
    env = {
        "PATH": (f"{shim}:" if shim else "") + "/usr/bin:/bin:/usr/local/bin",
        "HOME": str(cwd),
        "GNUPGHOME": str(gnupg_home),
        "PASSPHRASE": passphrase,
    }
    return subprocess.run(
        ["bash", "-c", script], cwd=cwd, env=env, capture_output=True, text=True, timeout=60,
    )



def _path_without_gpg(tmp_path):
    """gpg 만 없는 PATH — rm/tar/mv 등 나머지 도구는 있어야 '진짜 gpg 부재'만 재현됩니다."""
    bin_dir = tmp_path / "no-gpg-bin"
    bin_dir.mkdir(exist_ok=True)
    for tool in ("rm", "tar", "mv", "cat", "gzip", "mktemp", "ls", "dirname", "env"):
        found = shutil.which(tool)
        if found and not (bin_dir / tool).exists():
            (bin_dir / tool).symlink_to(found)
    assert not (bin_dir / "gpg").exists()
    return bin_dir


def _run_without_gpg(script, cwd, tmp_path):
    env = {"PATH": str(_path_without_gpg(tmp_path)), "HOME": str(cwd), "PASSPHRASE": PASSPHRASE}
    return subprocess.run(
        ["/bin/bash", "-c", script], cwd=cwd, env=env, capture_output=True, text=True, timeout=60,
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


# ═══════════════════════════════════════════════════════════════════
# 실패 안전성: 중간 평문 tgz 없음(스트리밍) + 실패 시 부분 암호문·평문 압축파일 잔존 없음
# ═══════════════════════════════════════════════════════════════════
def _shim(tmp_path, name, body):
    """PATH 앞에 두는 가짜 명령 (실제 명령을 감싸 동작을 관찰하거나 일부러 실패시킵니다)."""
    bin_dir = tmp_path / "shim"
    bin_dir.mkdir(exist_ok=True)
    path = bin_dir / name
    path.write_text("#!/bin/bash\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return bin_dir


def _leftovers(work):
    return sorted(
        p.name for p in work.iterdir()
        if p.name.startswith("evidence.tgz")
    )


def test_encrypt_streams_without_plaintext_tgz(workspace, gnupg_home, tmp_path):
    """gpg 가 실행되는 순간에도 디스크에 평문 evidence.tgz 가 없어야 합니다 (tar → gpg 스트리밍)."""
    seen = tmp_path / "seen.txt"
    shim = _shim(
        tmp_path, "gpg",
        f'ls -A "$PWD" > "{seen}"\nexec /usr/bin/gpg "$@"\n',
    )
    script, _env = _step_script("trade_base.yml", "정산 근거 암호화")
    result = _run(script, workspace, PASSPHRASE, gnupg_home, shim=shim)
    assert result.returncode == 0, result.stderr
    assert "evidence.tgz" not in seen.read_text().split(), "중간 평문 tgz 가 디스크에 생성됐습니다"
    assert _leftovers(workspace) == ["evidence.tgz.gpg"]


def test_encrypt_failure_leaves_no_partial_or_plain_archive(workspace, gnupg_home, tmp_path):
    """gpg 가 일부를 쓰고 실패해도 부분 암호문(.partial)·최종 산출물·평문 tgz 가 남지 않습니다."""
    shim = _shim(
        tmp_path, "gpg",
        'out=""\nwhile [[ $# -gt 0 ]]; do [[ "$1" == "--output" ]] && out="$2"; shift; done\n'
        'cat > /dev/null\nprintf partial-garbage > "$out"\nexit 2\n',
    )
    script, _env = _step_script("trade_base.yml", "정산 근거 암호화")
    result = _run(script, workspace, PASSPHRASE, gnupg_home, shim=shim)
    assert result.returncode != 0
    assert _leftovers(workspace) == [], _leftovers(workspace)


def test_encrypt_tar_failure_leaves_nothing(workspace, gnupg_home, tmp_path):
    shim = _shim(tmp_path, "tar", 'echo "tar: simulated failure" >&2\nexit 2\n')
    script, _env = _step_script("trade_base.yml", "정산 근거 암호화")
    result = _run(script, workspace, PASSPHRASE, gnupg_home, shim=shim)
    assert result.returncode != 0
    assert _leftovers(workspace) == [], _leftovers(workspace)


def test_encrypt_failure_removes_stale_previous_ciphertext(workspace, gnupg_home, tmp_path):
    """이전 시도의 오래된 암호문이 남아 있어도 이번 실패 후 업로드 대상이 되지 않아야 합니다."""
    (workspace / "evidence.tgz.gpg").write_bytes(b"stale-ciphertext")
    (workspace / "evidence.tgz").write_bytes(b"stale-plain")
    shim = _shim(tmp_path, "gpg", "cat > /dev/null\nexit 2\n")
    script, _env = _step_script("trade_base.yml", "정산 근거 암호화")
    assert _run(script, workspace, PASSPHRASE, gnupg_home, shim=shim).returncode != 0
    assert _leftovers(workspace) == []


def test_encrypt_without_gpg_fails_closed(workspace, tmp_path):
    """gpg 가 없으면(PATH 에서 제거) 평문을 올리지 않고 **gpg 부재 사유로** 실패합니다."""
    script, _env = _step_script("trade_base.yml", "정산 근거 암호화")
    result = _run_without_gpg(script, workspace, tmp_path)
    assert result.returncode != 0
    assert "gpg" in result.stdout + result.stderr, result.stdout + result.stderr
    assert _leftovers(workspace) == []


def test_decrypt_truncated_ciphertext_extracts_nothing(workspace, gnupg_home, tmp_path):
    """잘린 암호문은 복호화 단계가 실패하고 evidence/ 에 일부 파일도 풀리지 않습니다 (원자적 반영)."""
    assert _encrypt(workspace, gnupg_home).returncode == 0
    verify = _download(workspace, tmp_path)
    cipher = verify / "evidence" / "evidence.tgz.gpg"
    cipher.write_bytes(cipher.read_bytes()[: -40])
    script, _env = _step_script("verify.yml", "정산 근거 복호화")
    assert _run(script, verify, PASSPHRASE, gnupg_home).returncode != 0
    assert not list((verify / "evidence").glob("settlement-*.json"))
    assert not list((verify / "evidence").glob("final-settlement-*.json"))


def test_decrypt_failure_leaves_no_staging_dir(workspace, gnupg_home, tmp_path):
    assert _encrypt(workspace, gnupg_home).returncode == 0
    verify = _download(workspace, tmp_path)
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    cipher = verify / "evidence" / "evidence.tgz.gpg"
    cipher.write_bytes(b"not-a-gpg-file")
    script, _env = _step_script("verify.yml", "정산 근거 복호화")
    env_script = f"export TMPDIR='{tmpdir}'\n" + script
    assert _run(env_script, verify, PASSPHRASE, gnupg_home).returncode != 0
    assert list(tmpdir.iterdir()) == [], "임시 스테이징 디렉터리가 남았습니다"


# ═══════════════════════════════════════════════════════════════════
# 이전 실행의 오래된 암호문(self-hosted runner 작업 디렉터리 재사용) 업로드 차단
# ═══════════════════════════════════════════════════════════════════
def _plant_stale(work):
    (work / "evidence.tgz.gpg").write_bytes(b"STALE-CIPHERTEXT-FROM-PREVIOUS-RUN")
    (work / "evidence.tgz.gpg.partial").write_bytes(b"stale-partial")
    (work / "evidence.tgz").write_bytes(b"stale-plain-archive")


def test_stale_ciphertext_removed_when_no_evidence_json(tmp_path, gnupg_home):
    """이번 실행의 Evidence JSON 이 없고 이전 암호문만 남은 경우: 정리 후 업로드 대상이 없어야 합니다."""
    work = tmp_path / "stale-work"
    work.mkdir()
    _plant_stale(work)
    result = _encrypt(work, gnupg_home)
    assert result.returncode == 0, result.stderr
    assert _leftovers(work) == [], f"오래된 암호문이 남았습니다: {_leftovers(work)}"
    # 검증 불가 상태가 명시적으로 보고돼야 합니다 (조용히 넘어가지 않음)
    assert "::warning::" in result.stdout and "검증 불가" in result.stdout, result.stdout


def test_stale_ciphertext_never_matches_upload_path_without_evidence(tmp_path, gnupg_home):
    """업로드 단계가 읽는 경로(evidence.tgz.gpg)에 이전 내용이 절대 남지 않습니다."""
    work = tmp_path / "stale-upload"
    work.mkdir()
    _plant_stale(work)
    assert _encrypt(work, gnupg_home).returncode == 0
    assert not (work / "evidence.tgz.gpg").exists()


def test_stale_ciphertext_removed_even_when_passphrase_missing(workspace, gnupg_home):
    """Evidence 는 있으나 패스프레이즈가 없어 실패하는 경우에도 이전 암호문은 먼저 제거됩니다."""
    _plant_stale(workspace)
    result = _encrypt(workspace, gnupg_home, passphrase="")
    assert result.returncode != 0
    assert _leftovers(workspace) == []


def test_stale_ciphertext_removed_when_gpg_missing(workspace, tmp_path):
    _plant_stale(workspace)
    script, _env = _step_script("trade_base.yml", "정산 근거 암호화")
    result = _run_without_gpg(script, workspace, tmp_path)
    assert result.returncode != 0
    assert "gpg" in result.stdout + result.stderr, result.stdout + result.stderr
    assert _leftovers(workspace) == []


def test_stale_ciphertext_replaced_by_fresh_one_on_success(workspace, gnupg_home, tmp_path):
    """정상 실행에서는 이전 암호문이 이번 실행의 암호문으로 완전히 교체됩니다."""
    _plant_stale(workspace)
    assert _encrypt(workspace, gnupg_home).returncode == 0
    assert _leftovers(workspace) == ["evidence.tgz.gpg"]
    assert b"STALE-CIPHERTEXT" not in (workspace / "evidence.tgz.gpg").read_bytes()
    verify = _download(workspace, tmp_path)
    script, _env = _step_script("verify.yml", "정산 근거 복호화")
    assert _run(script, verify, PASSPHRASE, gnupg_home).returncode == 0
    assert (verify / "evidence" / "final-settlement-1.json").exists()
