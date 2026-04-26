# Raccoon 分段压测指南（可关机/可中断）

适用场景：电脑会频繁移动、关机，无法 7x24 持续跑。  
策略：把长压测拆成多个“分段”，每段独立执行，结果自动累计。

## 1) 用脚本还是命令？

结论：**优先用脚本**。  
脚本已经帮你做了 3 件事：

1. 每次跑完自动保存一份原始 JSON。
2. 自动把本次结果追加到累计记录（`segments.jsonl`）。
3. 自动刷新累计汇总（`summary.json`）。

脚本路径：`scripts/soak_segment.sh`

## 2) 一天跑几次（推荐）

移动办公建议走“3 段制”：

1. 上午一次（出门前或开工后）  
   `stable=1 perturb=1 open_world=1`
2. 下午一次（网络环境可能变化）  
   `stable=1 perturb=1 open_world=1`
3. 晚上一次（收工前，稍重）  
   `stable=2 perturb=2 open_world=1`

如果当天时间很紧，最少跑 2 段（上午 + 晚上）。

## 3) 直接可用命令

先定一个会话 ID（建议按周）：

```bash
bash scripts/soak_segment.sh --session-id 2026w17 --stable-rounds 1 --perturb-rounds 1 --open-world-rounds 1
```

同一天/第二天继续跑，**保持同一个 `--session-id`** 即可自动累计：

```bash
bash scripts/soak_segment.sh --session-id 2026w17 --stable-rounds 2 --perturb-rounds 2 --open-world-rounds 1
```

## 4) 结果看哪里

会话目录：

`output/soak_runs/<session_id>/`

关键文件：

- `segment-YYYYmmdd-HHMMSS.json`：单次原始结果
- `segments.jsonl`：所有分段明细（可长期累计）
- `summary.json`：当前累计汇总（每次跑完自动刷新）

查看累计结果：

```bash
cat output/soak_runs/2026w17/summary.json
```

## 5) 判定是否达标（建议）

看 `summary.json` 里这些字段：

1. `segment_pass_rate`：建议 >= 0.85  
2. `avg_live_execution_success_rate`：建议 >= 0.85  
3. `avg_live_browser_chain_execution_success_rate`：建议 >= 0.90  
4. `avg_open_world_execution_success_rate`：建议 >= 0.85  
5. `avg_open_world_false_clarification_rate`：建议 <= 0.05

## 6) 只想用一条命令（不使用脚本）

也可以，但不会自动累计：

```bash
python3.11 raccoon.py benchmark all --stable-rounds 1 --perturb-rounds 1 --open-world-rounds 1 --json > output/segment-$(date +%Y%m%d-%H%M%S).json
```

---

如果本次执行中断（关机/断网/合盖），不用清理，下一次继续执行同一个 `session-id` 就行。
