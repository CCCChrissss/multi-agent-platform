# 驗證

專案主要使用可直接執行的 smoke test，而不是 pytest。以下先列 Windows / PowerShell 的目前驗證路徑；服務與模型現況見 [current-windows-status.md](current-windows-status.md)。

> [!IMPORTANT]
> commit `39d6449` 的 GitHub Actions 中，`windows-static-compatibility` 與 `mcp-server-smoke-tests` 成功，`gather-concurrency-smoke-test` 失敗。失敗原因是測試仍預期不通知時回傳 `["no notification needed"]`，目前實作回傳 `[]`。這是已重現的測試預期落差，本次文件階段不修改程式或測試。

## 服務停止時先跑的檢查

在 repository 根目錄：

```powershell
.\.venv\Scripts\python.exe scripts/static_compat_check.py
.\.venv\Scripts\python.exe -m services.stt.temp_audio_smoke_test
```

第一項檢查 Python 語法、編碼、設定引用與 Markdown 連結；第二項檢查 Windows 暫存音檔行為。它們不會啟動服務，也不會驗證資料庫、模型或完整 workflow。

## 單一 MCP server（秒級回饋）

新增/修改一個 MCP server 時最先該跑的一層——直接用 `mcp_servers/base_client.py` 的
`MCPClient` 連上該 server 的 stdio 子行程，**不碰 LLM、不碰 agent、不碰 gateway**：

```powershell
.\.venv\Scripts\python.exe -m mcp_servers.stt.smoke_test
.\.venv\Scripts\python.exe -m mcp_servers.format_check.smoke_test
.\.venv\Scripts\python.exe -m mcp_servers.lookup.smoke_test
.\.venv\Scripts\python.exe -m mcp_servers.notified.smoke_test
.\.venv\Scripts\python.exe -m mcp_servers.calc.smoke_test
.\.venv\Scripts\python.exe -m mcp_servers.memory.smoke_test
```

每份至少驗三件事：(1) 工具清單與參數 schema 符合預期、(2) 正常輸入回正確結構、(3) 壞輸入
回的是分類過的錯誤（`ToolInputError`/`ToolDependencyError`，見
[../mcp_servers/tool_errors.py](../mcp_servers/tool_errors.py)）或工具自己設計的「明確的空」，
不是 raw traceback。

- `stt`/`format_check`/`lookup`/`notified` 四份完全獨立，不需要 `honcho start`——如果剛好有
  跑（例如本機開發時），會多驗一條「依賴真的可達時回傳正確結構」的分支，但不依賴它。
  `stt` 的真實轉錄需要呼叫 LiteLLM gateway，屬於秒級以上的模型推論，不適合這一層，所以用短
  timeout 探測：gateway 沒起來就驗證乾淨的 `ToolDependencyError`，起了但還在推論就直接跳過
  （不等它跑完）。四份合計應該在 10 秒內跑完，這也是它跟下面端到端 smoke test 分工的理由：
  慢了就沒人會跑。已接進 CI（[../.github/workflows/ci.yml](../.github/workflows/ci.yml)）。
- `memory` 需要 Postgres（server 一開就開真的長期記憶 store），CI 沒有 Postgres 所以沒接進去，
  本機手動跑即可；不需要 Ollama/LiteLLM，因為呼叫時都不帶 `query`，不會走進 embedding
  路徑。額外驗證 `MCP_CALLING_PRINCIPAL` 未設或設成無授權 principal 時 fail-closed（`recall`/
  `browse` 回空，不是回資料）——判斷方式是查 `call_log` 的 `denied` 欄位，因為「被拒絕」和
  「單純沒資料」從回傳內容本身看不出差別。

## 端到端（分鐘級，燒真的 LLM）

```powershell
.\.venv\Scripts\python.exe -m event_bus.smoke_test
.\.venv\Scripts\python.exe -m orchestrator.smoke_test
.\.venv\Scripts\python.exe -m workflows.parity_check
```

- `event_bus.smoke_test` 只需要 Postgres，不碰 LLM。
- 後兩個需要 `honcho start` 已經在跑（它們會呼叫真的 LLM 與 agent service）。
- 三個都會在 process 內自己起需要的 master/worker。
- ⚠️ **跑之前要先關掉 `honcho -f Procfile.workers start`**——那批 process 的 consumer group 跟測試同名，會搶走測試的命令，讓用假 handler 的情境失效。
- `gather_concurrency_smoke_test.py`（repo 根目錄）不需要任何 process：`.\.venv\Scripts\python.exe -B gather_concurrency_smoke_test.py`。目前會在 notified scenario 因上述預期值落差失敗；不要把它記成已通過。

寫一個新工具卻要跑完整條 agent 鏈路才知道對不對，回饋迴路太長，而且工具本身的問題（回傳結構
錯、壞輸入漏 traceback）會被 LLM 的不確定性蓋掉——這是上面那層單一 MCP server smoke test存在
的理由，兩層不是互相取代，而是分工：新工具先過第一層，再由端到端這層驗證它接進真實鏈路後行
為不變。

長期記憶本身的正確性（`recall()`/`browse()`/`remember()`、status gate、稽核日誌）：

```powershell
.\.venv\Scripts\python.exe -m persistence.memory_smoke_test
```

只需要 `honcho start` 在跑（會呼叫真的 embedding），不需要 `Procfile.workers`。

## 記憶蒸餾 pipeline（P0-P5，上游流程；Windows 尚未重新驗證）

[knowledge-distillation-plan.md](knowledge-distillation-plan.md) 的 episodic -> 候選 procedural 規則 -> 人工審核 -> 生效整條鏈路。以下保留原作者的完整步驟與 Bash 寫法，尚未在目前 Windows / 無 Anthropic / Gemini key 的環境重新驗證。`stt_exclusion_notify` 仍依賴雲端 provider，因此現在不要把這一節當成本機可執行的 happy path。

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
