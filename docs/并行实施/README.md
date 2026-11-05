# 并行实施 · 共享地托管流程（W4 单写，2026-11-03 交棒）

本目录是七个窗口唯一的**提交待合并内容**的地方。本仓库不是 git 仓库，两个窗口写同一个文件
就是后写的静默覆盖先写的，所以这里的规则只有一条：**共享地只有一个写入者，别人交分片。**

分工与卡号见 [多窗口并行实施分工](../多窗口并行实施分工.md)；接缝的实际形状见
[交棒清单](交棒清单.md)。

## 1. 单一写入者

| 共享地 | 唯一写入者 | 别人怎么改 |
| --- | --- | --- |
| `docs/缺陷与实现计划-任我行.md`（含 §1.1 矩阵行、口径句、状态明细、文档版本行） | W4 | 写 `docs/并行实施/<卡号>.md` 分片 |
| `engine/requirements.txt` / `engine/requirements.lock.txt` / `scripts/stage-engine.mjs` | W4 | 分片里提依赖请求（包名 + 精确版本 + 为什么必须随包） |
| `engine/side_effect_registry.py`（四张副作用视图） | W4 | 分片里写「新工具名进哪几张视图 + 为什么」 |
| `src/shared/step-replay.ts` 的 `SIDE_EFFECT_TOOLS` / `SIDE_EFFECT_KEYWORDS` | W4 | 同上（渲染层与 Python 侧由 `step-replay.test.ts` 机器钉同源） |
| `engine/server.py` / `src-tauri/src/lib.rs` / `src/shared/contracts.ts` / `src/shared/types.ts` 的**注册面** | W4（#164 已收口，之后各窗口不该再需要打开它们） | 走车道文件：`engine/routes_<lane>.py`、`engine/toolkit_<lane>.py`、`src-tauri/src/ipc/<lane>.rs`、`src/shared/contracts_<lane>.ts` |
| `docs/并行实施/README.md` 与本目录的 `交棒清单.md` | W4 | 分片里指出清单与现实不符之处 |

## 2. 分片怎么写

文件名 `docs/并行实施/<卡号>.md`（例：`137.md`），四段固定标题，缺哪段就写「本刀无」：

1. **矩阵行** — 这条缺陷在 §1.1 计数矩阵与 §2 总表里要不要动状态（待处理 / 进行中 / 待验证 / 已关闭）。
   只有本地可完整验收且文档同步了才够得上「待验证」；只写了引擎半边就照写「进行中 + 未做的半边」。
2. **口径句** — 供「更新N」条目使用的一段正文。**编号不要自己写**，W4 合并时以 `grep -o '更新[^，（]*'`
   实测当前最大号 +1 为准（历史上已经出现过同号被两批连用的情况）。
3. **状态明细** — 各 `### D-xx` 小节里要落的「状态 / 进展 / 剩余」文字。
4. **未验收与边界** — 需要真机、真实凭据、外部条件的所有条目，以及自测覆盖不到的路径。

W4 每日合并一次，合并后跑 `npx vitest run tests/unit/docs/defect-ledger-contract.test.ts`
（矩阵行列与状态明细必须自洽，台账任何行都不得出现「工具链」三字）。**合并时数字以本机复跑为准**，
分片里写的数字如果复跑对不上，以复跑值落台账并在口径句里点名差异，不照抄。

## 3. 三条硬口径（历次真机/自测换来的，别再退回去）

- **新能力一律条件注册，不进常驻清单。** `tests/unit/extensions/builtin-tools-contract.test.ts`
  要求常驻清单里每个 id 都在 `src-tauri/src/harness/tools.rs` 有实现；把 Rust 没实现的能力写进
  常驻清单，界面上就是一条空头承诺。开关表在 `engine/toolkit_lanes.SWITCH_BOARD`。
- **台账回填不许超出实测。** 自测绿不等于实机绿；真机没跑就写「未验收」。
- **假用量不许写进真实台账。** 引擎自测必须隔离 `TONGTIANWORK_HOME`（历史事故：未隔离版本把
  合成用量行写进了 `~/.tongtianwork/usage/model-usage.jsonl`）。

## 4. 复跑配方（判定只看 `^[PASS]`，别信回显）

```bash
cd tongtian-tauri
./vendor/python/python.exe -X utf8 tests/integration/<你的自测>.py > /tmp/x.log 2>&1
grep -c '^\[PASS\]' /tmp/x.log          # 文档族必须用 vendor：.venv 缺 docx 会整族 SKIP 且 rc=0
npx tsc --noEmit
npx vitest run tests/unit/<你的目录>/
bash scripts/run-rust-tests.sh <模块>::  # 裸 cargo test --lib 会 0xc0000139
```

vendor 解释器的 `time.monotonic` 是 GetTickCount64（粒度 15.6 ms），耗时断言不许贴着睡眠值。
`src-tauri/staged-engine/` 是 `npm run build` 生成的引擎拷贝，改 `engine/` 后必须重建才进桌面包。
