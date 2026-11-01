---
name: jev
description: 经 OpenRouter 调用 TypeSafe 的 Jev 决策模型，对结构化问题直接返回带概率的判定：是否题、单选题、有序打分题，一次可问多道。约 1–2 秒、每次约 0.00002 美元。适合分类、分流、初筛；需要推理、计算或查证的问题不要用。
---

# jev

Jev 是只做判定的模型：给它当前状态和带说明的问题，它返回选择及概率，不生成正文。开发说明见 `docs/jev.md`。

## 适用场景

- 分类与分流：反馈归类、任务归属、是否需要人工处理。
- 是否判断、多选一，以及“紧急程度”这类有序等级。
- 后面还有人或完整推理把关的初筛。

不要用于：需要推理、计算、多步判断或查证事实的问题；结果会直接产生对外影响的决定；选项不固定的开放问题。

## 数据边界

`state` 和题目会发给 OpenRouter 与 TypeSafe。只传判定所需的最少内容；不得传入个人信息、凭证或受合规约束的数据，做不到时不要调用。请求超过 4 万字符会直接报错，不截断，先摘出相关部分。

## 用法

单选题可以用参数简写：

```bash
python3 .claude/skills/jev/scripts/jev_decide.py --question "这条反馈属于哪一类？反馈：导出 PDF 时中文变成方块。" \
  --option 功能缺陷 --option 功能建议 --option 使用咨询 [--state-file 材料.txt]
```

完整写法从标准输入传 JSON，可在一次请求里问多道题：

```bash
printf '%s' '{
  "state": {"ticket": "导出 PDF 时中文变成方块"},
  "questions": {
    "is_bug":  {"type": "noul",   "instructions": "这是软件缺陷吗？",
                "criteria": {"true": "描述了异常行为", "false": "提问或建议"}},
    "team":    {"type": "choice", "instructions": "哪个组负责？",
                "criteria": {"export": "导出与打印", "editor": "编辑器", "account": "账号"}},
    "urgency": {"type": "score",  "instructions": "紧急程度？",
                "criteria": ["下个版本再修", "本周内修", "立刻修"]}
  }
}' | python3 .claude/skills/jev/scripts/jev_decide.py --json -
```

- `state`：对象或文本，放判定依据。
- `noul` 的 `criteria` 只能有 `true`、`false` 两项；`choice` 至少两个“选项: 说明”；`score` 至少两个按顺序排列的等级。
- `--dry-run`：只校验并打印请求体，不联网，不需要凭证。

## 读结果

- `noul`：“是”的概率，0–1。接近 0.5 表示拿不准，不表示“程度中等”。
- `choice`：`choice` 为所选项，`probabilities` 为各选项概率，`confidence` 表示概率集中的程度。
- `score`：`score` 是按概率加权的位置，0 对应第一个等级；`legend` 列出各等级。
- `usage.cost`：本次费用（美元），只按输入计费。

概率没有针对你的数据校准，Jev 对明确题目常直接给 1。用于自动分流时，先用带标准答案的样本定阈值，并按出错的代价来定，不要取整数凭感觉；没达到阈值的交给完整推理或人工判断。

## 失败处理

退出码 1 并输出 `{"error": …}`：输入不完整、缺少凭证、OpenRouter 不可达，或服务端拒绝请求。只输出错误类别或 HTTP 状态，不输出可能回显凭证的服务端正文。凭证从环境变量 `OPENROUTER_API_KEY` 或 Pi 的 openrouter 登录读取；网络代理沿用 Pi 设置里的 `httpProxy`。不会自动换用其他模型。

拒绝所有 HTTP 重定向，避免将凭证转发到其他地址。响应须通过题型、选项、概率及用量结构校验，否则报错，不作成功判定。

每次成功调用的用量记入 Desk 用量库（类别 `jev`），只存数字；记账失败明确警告，不影响已返回的判定。
