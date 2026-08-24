# 驗證

沒有 pytest，全部是手動跑的 smoke test：

```bash
uv run python -m event_bus.smoke_test        # event bus 本身：pub/sub、當機重派、NOTIFY 延遲
uv run python -m orchestrator.smoke_test     # 編排層：單步、全鏈路、needs_review 短路、當機復原、重複發布
uv run python -m workflows.parity_check      # 兩種模式對同一份輸入產出相同結果與 call_log 形狀
```

- `event_bus.smoke_test` 只需要 Postgres，不碰 LLM。
- 後兩個需要 `honcho start` 已經在跑（它們會呼叫真的 LLM 與 agent service）。
- 三個都會在 process 內自己起需要的 master/worker。
- ⚠️ **跑之前要先關掉 `honcho -f Procfile.workers start`**——那批 process 的 consumer group 跟測試同名，會搶走測試的命令，讓用假 handler 的情境失效。
- `gather_concurrency_smoke_test.py`（repo 根目錄）是唯一不需要任何 process 在跑的：純 mock，`uv run python gather_concurrency_smoke_test.py` 即可，也是目前唯一接進 CI（[../.github/workflows/ci.yml](../.github/workflows/ci.yml)）的測試。

長期記憶本身的正確性（`recall()`/`browse()`/`remember()`、status gate、稽核日誌）：

```bash
uv run python -m persistence.memory_smoke_test
```

只需要 `honcho start` 在跑（會呼叫真的 embedding），不需要 `Procfile.workers`。

## 記憶蒸餾 pipeline（P0-P5）手動試跑

[knowledge-distillation-plan.md](knowledge-distillation-plan.md) 的 episodic -> 候選 procedural 規則 -> 人工審核 -> 生效整條鏈路，依序手動跑一次的指令。只需要 `honcho start` 在跑，不需要 `Procfile.workers`（這條鏈路不經過事件驅動編排）。全部針對 `stt_exclusion_notify/check` 這個 scope。

```bash
# 1. 保單條款灌進長期記憶（check 查證據用，不做過就是空的）
uv run python -m scripts.seed_insurance_memory

# 2. 灌幾筆 episodic 範例案例，讓 P2 蒸餾器有東西可以讀
#    （production 場景本身也會透過 orchestrator/memory_writer.py 自動累積，這步只是demo/本機測試用的捷徑）
uv run python -m scripts.seed_exclusion_episodic_examples

# 3. 跑 baseline：目前 evals/check_cases.yaml 的通過率
uv run python -m evals.run_eval --repeats 3

# 4. 蒸餾：把 episodic 案例歸納成候選 procedural 規則，寫成 status="pending"（不影響任何正式判斷）
uv run python -m scripts.distill_procedural --scope stt_exclusion_notify/check

# 5. 人工審核：互動式 CLI，對每個 pending 候選印出 baseline vs candidate 的 pass rate 比較表，
#    approve 才會真的變成 active、被 exclusion judge 的 prompt 讀到；reject 直接刪除；
#    e 可以就地改規則文字重新比較一次
uv run python -m scripts.review_memory --scope stt_exclusion_notify/check --repeats 3
```

- 步驟 4/5 內部都是透過 [../persistence/memory_lifespan.py](../persistence/memory_lifespan.py) 的 `open_agent_memory()` 開 store，不需要另外手動跑遷移腳本——`status` 欄位缺失的舊資料會在開 store 時自動補齊（[../persistence/memory.py](../persistence/memory.py) 的 `backfill_missing_status()`）。真的需要在沒有任何 process 開過 store 的情況下單獨補齊，才需要：
  ```bash
  uv run python -m scripts.backfill_memory_status
  ```
- 步驟 5 的比較表跑在一個獨立、用完即丟的 `eval` tenant（[../scripts/stage_candidate_for_eval.py](../scripts/stage_candidate_for_eval.py)），不會碰到 `default` tenant 的正式記憶，重複跑/中途取消都安全。

### 拆解版：不透過 `review_memory.py`，一步一步手動跑

`review_memory.py` 把「stage + 跑 baseline/candidate 兩次評測 + 印對照表 + 問 approve/reject」全包在一個互動迴圈裡。如果想針對某一條候選規則自己一步步看、或者已經手動跑過對照不想讓 CLI 重跑一次評測，可以拆開跑——底下每一步都是獨立、非互動的指令，`stage_candidate_for_eval.py`/`evals.run_eval` 本來就是可以單獨執行的 CLI：

```bash
# 1. 蒸餾（同上面步驟 4），印出這次產生的候選規則 key，例如 pending-<uuid>
uv run python -m scripts.distill_procedural --scope stt_exclusion_notify/check

# 2. 把其中一條候選 stage 進 eval tenant -- 會連同 default tenant 目前所有已核准的
#    active 規則一起鏡射進去（不是只放這一條），所以測的是「疊加」不是「取代」
uv run python -m scripts.stage_candidate_for_eval --key pending-<uuid> --scope stt_exclusion_notify/check

# 3. baseline：不含這條候選規則的現況（只涵蓋 evals/check_cases.yaml 的 holdout/regression，
#    不含這條候選自己的 evidence 案例 -- 見下面第 5 步）
uv run python -m evals.run_eval --tenant default --repeats 3

# 4. candidate：含這條候選規則
uv run python -m evals.run_eval --tenant eval --repeats 3

# 自己比對 3/4 兩份輸出的通過率，判準見 docs/knowledge-distillation-plan.md §4.3：
# holdout/regression 不能退步。
```

**這兩步測不到候選規則真正要修的東西。** `evals/run_eval.py` 只讀 `check_cases.yaml`，不會動態納入這條候選的 `evidence` 指到的 episodic 案例——單看 3/4 的結果，永遠沒辦法確認「這條規則有沒有讓它自己聲稱要修的那個場景改變判斷」。要看這個，把候選的 evidence 案例現場重跑一次（`scripts/review_memory.py` 互動版本身在問 approve/reject 之前就會自動做這件事；下面是想繞過互動版時單獨跑的做法）：

```bash
uv run python -c "
import asyncio
from mcp_servers.gateway import MCPGateway
from mcp_servers.policy import load_policy
from persistence.call_log import current_node_name, current_thread_id
from persistence.memory import parse_scope
from persistence.memory_lifespan import open_agent_memory
from scripts.review_memory import _load_evidence_cases, _print_evidence_diagnostic
from evals.run_eval import _run_case

async def main():
    scope = parse_scope('stt_exclusion_notify/check')
    evidence = ['seed-<episodic-key>']  # 這條候選的 evidence 清單，從蒸餾/review_memory.py 的輸出複製
    current_thread_id.set('manual-evidence-check')
    current_node_name.set('check')
    policy = load_policy('mcp_servers/policy.yaml')
    async with (
        open_agent_memory('mcp_servers/policy.yaml') as (store, memory_policy),
        MCPGateway(policy, principal='check') as gateway,
    ):
        cases = await _load_evidence_cases(store, scope, evidence)
        baseline = [await _run_case(gateway, store, memory_policy, 'default', c, 3) for c in cases]
        candidate = [await _run_case(gateway, store, memory_policy, 'eval', c, 3) for c in cases]
        _print_evidence_diagnostic(baseline, candidate)

asyncio.run(main())
"
```

**這段是診斷用，不是判準**——`expected` 欄位餵的是 episodic 記錄當初寫入的 `output`，那從來沒被人工驗證過（`docs/knowledge-distillation-plan.md` §6 #4），只能拿來看「答案有沒有變」，不能拿來自動判定過不過。真正的 approve/reject 還是人來看這張表 + 上面的 holdout/regression 表一起判斷。

approve/reject 本身一定要走 [../persistence/memory.py](../persistence/memory.py) 的 `edit()`/`forget()`（保留稽核日誌，見 [../fixed.md](../fixed.md#review-approve-reject-audit-gap)），不要直接用 `psql`/`store.aput()` 繞過去。想跳過 `review_memory.py` 重新觸發一次評測，可以直接重用它裡面的 `_approve()`/`_reject()`：

```bash
uv run python -c "
import asyncio
from persistence.call_log import current_node_name, current_thread_id
from persistence.memory import MemoryKind, build_namespace, parse_scope
from persistence.memory_lifespan import open_agent_memory
from scripts.review_memory import _approve, _print_regression_suggestions  # 或 _reject

async def main():
    scope = parse_scope('stt_exclusion_notify/check')
    key = 'pending-<uuid>'
    current_thread_id.set('manual-approve')
    current_node_name.set('memory_writer')
    async with open_agent_memory('mcp_servers/policy.yaml') as (store, policy):
        namespace = build_namespace(MemoryKind.PROCEDURAL, 'default', scope)
        item = await store.aget(namespace, key)
        await _approve(store, policy, scope, key, item.value)
        await _print_regression_suggestions(store, scope, item.value.get('evidence', []))  # 印出建議加進 check_cases.yaml 的草稿

asyncio.run(main())
"
```

approve 之後印出的 `regression` 案例草稿不會自動寫檔——`evals/check_cases.yaml` 是人工維護的檔案，`expected` 要先人工確認（或拿逐字稿實際跑一次 `judge_exclusion()` 驗證）才貼進去，貼的時候記得標 `split: regression`（範例見該檔案的 `seed-drunk_ride_ltc`）。
