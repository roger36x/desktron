# Jev 判定

Desk 通过 `.claude/skills/jev` 接入 TypeSafe 的 Jev 决策模型。Jev 不生成正文：输入当前状态和带说明的问题，直接返回选择及概率。

## 调用

脚本 `jev_decide.py` 向 `https://openrouter.ai/api/alpha/decisions` 发送 `{model, state, questions}`，模型固定为 `typesafe/jev-1.13`，不跟随 `~typesafe/jev-latest`。题型有三种：

| 类型 | criteria | 返回 |
|---|---|---|
| `noul` | `{"true": 说明, "false": 说明}` | “是”的概率 |
| `choice` | `{选项: 说明, …}`，至少 2 项 | 所选项、各项概率、confidence |
| `score` | 按顺序排列的等级说明，至少 2 项 | 概率加权位置、各等级概率、legend |

脚本在本地检查结构完整与请求长度（4 万字符上限）。响应需校验答案键、题型、有效选项、有限概率值、打分等级及用量；异常返回不作成功判定。所有重定向均拒绝；服务端拒绝时只输出错误类别和 HTTP 状态，不输出可能包含凭证的原始正文。凭证取自 `OPENROUTER_API_KEY` 或 Pi 的 openrouter 登录，代理沿用 Pi 的 `httpProxy`。用量写入用量库 `sidecar` 表，类别 `jev`。

接口路径为 alpha、模型为 beta，字段可能变化；变化时先改脚本校验和测试。响应契约依据：[OpenRouter Jev Tutorial](https://openrouter.ai/docs/guides/community/jev-tutorial)，页面标题 `Jev Tutorial - Make Your First Decision Call on OpenRouter`，核对时间 2026-10-06T03:39:45.457Z。该文档不能替代本机真实调用验收。

## 选型记录

2026-10-06 用 10 道有标准答案的选择题，对比 Jev 与自建方案（OpenRouter `qwen/qwen3.8-27b` 关闭推理、取首 token logprobs、正序倒序各一次）：

| | Jev | Qwen 自建方案 |
|---|---|---|
| 正确 | 10/10 | 10/10 |
| 延迟中位 / 最大 | 1.22 秒 / 2.16 秒 | 4.03 秒 / 6.75 秒 |
| 10 题费用 | $0.000150 | $0.000271 |

Jev 输入单价 $0.042/百万 token，输出免费；Qwen 费用随供应商在 4 倍范围内波动。已排除自建方案：更慢、更贵，每轮由不同供应商和量化精度执行，结果不稳定。随之移除了自建方案用到的 logits 判定内核（`desktron/lib/system_one`，提交 `f7fb643` 引入）。

Jev 对明确题目常直接给 1，概率只保留两位小数；用于自动分流前需用带标准答案的数据定阈值。
