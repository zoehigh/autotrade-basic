# Actions Mock 검증 — 휴장·마감 후 SKIP

`trade_base.yml` 의 **스텝 스크립트와 `if:` 조건을 그대로 복사한** 격리 워크플로를 GitHub 에서 실행해,
로컬에서 확인할 수 없는 동작을 관측합니다. **push 하기 전에는 아무것도 실행되지 않습니다. 승인 후에만 push 하세요.**

## 격리 조건 (`tests/test_mock_actions_workflow.py` 가 확인)

- secrets·repository variables·environment 를 참조하지 않음, `permissions: contents: read`, hosted 러너만
- 봇 실행 스텝은 가짜 `uv` 만 사용하고 `trading_bot.py` 는 실행 불가 파일로 덮어씀 → 브로커·텔레그램 호출 없음
- state 캐시는 키(`mock-skip-…`)와 저장 경로(`.mock-cache/…`) 모두 Mock 전용, restore-keys 없음.
  uv 의 Actions 캐시는 끔. 캐시는 브랜치 범위라 main 실행이 복원할 수 없음
- concurrency 그룹이 운영과 분리, matrix 병렬 4개 제한, 생성 파일은 main/PR 에 커밋하지 않음

## 실행 (승인 필요)

```bash
git switch -c mock-verify/pr31 fix/pipeline-session-skip
uv run python ops/mock_actions/build.py --out .github/workflows   # pyyaml 이 없으면 uv run --with pyyaml
git add .github/workflows/mock_session_*.yml
git commit -m "ci(mock): 세션 SKIP 검증용 임시 워크플로"
git push origin mock-verify/pr31                                  # 시나리오 14개 실행
```

- 판정: 각 시나리오의 **`assert` job 이 모두 성공**이면 통과입니다. 실패를 재현하는 시나리오가 있으므로
  **run 전체는 빨간색**입니다.
- `gh run view <run_id> --json jobs --jq '.jobs[]|select(.name|endswith("assert"))|[.name,.conclusion]|@tsv'`

취소 시나리오(별도 승인): 위 run 이 끝난 뒤 **같은 브랜치**에 커밋 메시지에 `[mock-cancel]` 이 든 빈 커밋을
올립니다. 취소 시나리오만 실행되며, 봇 스텝이 `EXECUTE` 에서 대기하는 동안(봇 스텝 시작 1~2분 뒤)
`gh run cancel <run_id>` 로 취소합니다.

```bash
git commit --allow-empty -m "ci(mock): [mock-cancel] 취소 시나리오"
git push origin mock-verify/pr31
```

## 시나리오와 기대값 (`scenarios.py`)

| 시나리오 | run_outcome | 새 캐시 | 정산 검증 |
|---|---|---|---|
| orders / partial | ORDERS_EXECUTED / PARTIAL_FAILURE | 저장 | 실행 |
| holiday-settle | SETTLE_ONLY | 저장 | 실행 |
| skip / dry-holiday | SKIPPED | 없음 | **skipped** |
| skip-late-failure | SKIPPED (job 실패) | 없음 | 실행(실패) |
| hold-blocks-skip / gate-error / wait-budget / real-review-gate | NOT_STARTED | 없음 | 실패 |
| evidence-missing / settle-failure | SETTLE_ONLY / FAILED | 저장 | **실패** (근거 누락) |
| killed / step-timeout | PARTIAL_FAILURE 또는 빈 값(관측) | **저장** | 실행 |
| cancel (수동) | PARTIAL_FAILURE 또는 빈 값(관측) | **저장** | 관측 |

`killed`·`step-timeout`·`cancel` 에서 표식이 비어 있어도 설계상 안전합니다(캐시 저장·검증 수행). 어느 쪽이었는지는
`assert` job 요약의 "관측" 줄에 남습니다.

## 덮지 않는 것

- 실제 봇·브로커·텔레그램 동작 (로컬 pytest 의 Mock E2E 범위)
- self-hosted 러너, 단독 `EXECUTE` 단계 (hosted 에서는 단독 단계가 실행 전에 차단됨)
- 정산 검증의 **성공** 경로 (Mock 근거라 실행 여부와 근거 누락 실패만 확인)

## 정리 (승인 필요)

원격 `mock-verify*` 브랜치 삭제. Mock 캐시·artifact 는 삭제하지 않아도 자동 만료됩니다(캐시 7일 미사용, artifact 14일).
