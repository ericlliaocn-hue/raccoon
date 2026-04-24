# Raccoon 路线图（0.5.x）

> 最后更新：2026-04-25  
> 当前基线：`0.5.2`（已完成）  
> 当前迭代起点：`0.5.3`

## 先决规则：版本先行（强制）

从 `0.5.3` 起，每一轮开发必须先完成版本同步，再进入功能改动。

固定同步项：

1. `pyproject.toml`
2. `src/__init__.py`
3. `src/adapters/static/index.html`（静态资源版本参数 + 页面版本文案）
4. `CHANGELOG.md` 新版本小节

未完成以上 4 项，不进入需求实现。

---

## 6 周版本映射（周更）

| 周次 | 版本 | 主目标 | 交付物 |
|---|---|---|---|
| Week 1 | `0.5.3` | 核心场景基线固化 | 离线样本包 + 真实任务样本包 + 基线报告模板 |
| Week 2 | `0.5.4` | 学习命中率专项 | 复用召回增强 + failure_code 修复模板收敛 |
| Week 3 | `0.5.5` | 浏览器长链路稳定性 | 登录态治理 + 断点恢复强化 + 证据标准化 |
| Week 4 | `0.5.6` | 候选池运营闭环 | `new/approved/rejected/implemented` 状态流 + 决策审计 |
| Week 5 | `0.5.7` | 发布门禁收口 | doctor/benchmark 联动 + 失败码集中治理 |
| Week 6 | `0.5.8` | 达标发布周 | 连续 7 天双轨验证，不达标仅发修复版 |

---

## 稳定接口（保持不变）

- `GET /learning/runs`
- `GET /learning/candidates`
- `GET /benchmarks/core-scenarios/latest`

---

## 发布门槛（保持不变）

- 学习首轮命中率 `>= 70%`
- 学习最终成功率 `>= 85%`
- 浏览器关键链路成功率 `>= 90%`
- 卡死/不可恢复 run 比例 `<= 1%`

---

## 每周门禁执行清单

```bash
python3.11 -m compileall -q src skills
ruff check
pytest -m "not integration"
raccoon benchmark core
raccoon benchmark core --json
```

发布报告必须包含：

1. 场景级命中率/成功率
2. `failure_code` TopN
3. 候选池增减与处理结果

---

## 范围边界

- 本轮不扩 Docker 战线
- 继续场景驱动，不为空生态造 Skill
- 新增 Skill 由失败聚类触发，不因单次失败直接新增
