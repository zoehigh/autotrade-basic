"""복구 워크플로(repair_reverse_ledger_rewind.yml) 정적 가드 — 안전장치가 빠지지 않았는지 확인합니다."""
import os

import yaml

_PATH = os.path.join(os.path.dirname(__file__), "..", ".github", "workflows", "repair_reverse_ledger_rewind.yml")


def _wf():
    with open(_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_run_스크립트에_표현식을_직접_삽입하지_않는다():
    for name, job in _wf()["jobs"].items():
        for step in job["steps"]:
            run = step.get("run", "")
            assert "${{" not in run, f"{name}: run 안에 ${{{{ }}}} 직접 삽입 금지 (env 로 전달): {step.get('name')}"


def test_main_브랜치_demo_전용_인증정보_미주입():
    text = open(_PATH, encoding="utf-8").read()
    assert "github.ref == 'refs/heads/main'" in text
    assert "secrets." not in text, "복구 워크플로는 브로커/텔레그램 시크릿을 주입하지 않습니다"
    assert "-demo" in text and "real" not in text.lower().replace("required", "")


def test_승인_게이트와_적용_순서():
    jobs = _wf()["jobs"]
    assert jobs["approve"]["environment"] == "state-repair"
    assert jobs["approve"]["needs"] == "preview"
    assert jobs["apply"]["needs"] == "approve"


def test_거래와_같은_concurrency_그룹():
    jobs = _wf()["jobs"]
    for name in ("preview", "apply"):
        group = jobs[name]["concurrency"]["group"]
        assert group == "trade-${{ inputs.broker }}-demo"
        assert jobs[name]["concurrency"]["cancel-in-progress"] is False


def test_캐시_저장은_독립_검증_뒤에만():
    steps = _wf()["jobs"]["apply"]["steps"]
    names = [s.get("name", "") for s in steps]
    verify_idx = next(i for i, n in enumerate(names) if "독립 검증" in n)
    save_idx = next(i for i, s in enumerate(steps) if str(s.get("uses", "")).startswith("actions/cache/save"))
    assert verify_idx < save_idx
    assert "if" not in steps[save_idx], "저장 단계에 always() 같은 조건을 달면 검증 실패 후에도 저장될 수 있습니다"
    # 미리보기 job 은 캐시를 저장하지 않음
    assert not any(str(s.get("uses", "")).startswith("actions/cache/save") for s in _wf()["jobs"]["preview"]["steps"])


def test_최신_캐시_확인과_sha_확인_존재():
    text = open(_PATH, encoding="utf-8").read()
    assert text.count("--sort created_at --order desc") == 2  # preview + apply 재확인
    assert text.count("--ref refs/heads/main") == 2  # 복원과 같은 브랜치 범위(main)로 조회
    assert "odno" not in text.split("jobs:")[0].split("inputs:")[1].lower(), "주문번호 원문을 공개 입력으로 받지 않습니다"
    assert text.count("sha256sum -c -") >= 3
    assert "fail-on-cache-miss: true" in text
