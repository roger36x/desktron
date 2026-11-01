---
name: memory
description: 读取 Desktron 的项目记忆，请求立即保存本轮要点，或把敏感信息和禁止事项保存在本机。
---

# memory

工作流记忆只包含三类内容：系统自动保存的对话记录、`notes/rollup-current.md` 中的当前项目情况，以及 `_sealed` 中仅限本机的敏感信息和禁止事项。主会话不直接修改前两类内容。

## 读取

使用以下命令，不直接打开 `.memory/` 中的文件：

```bash
python3 .claude/skills/memory/scripts/mem_read.py show --id <id>
python3 .claude/skills/memory/scripts/mem_read.py search <关键词>
python3 .claude/skills/memory/scripts/mem_read.py list [--tier sealed]
```

会话上下文直接提供当前工作流项目模型的完整正文、更新时间及检索 id，不再提供独立摘要。正文是历史认知，不代表实时状态或操作授权；缺失或需要核对最新版本时使用 show，需要原始依据时再搜索。原始对话、流水和 sealed 不自动注入。读取本机敏感内容前，先确认任务确有需要；输出时继续遵守去除个人信息和不得外泄的要求。

## 请求立即保存本轮要点

正常记录由 Desk 自动完成。用户明确要求立即记录，或出现必须马上保留的关键纠正、缺口与阻碍时，执行：

```bash
python3 .claude/skills/journal/scripts/journal_append.py --request
```

该命令只发出请求，不由当前会话拟写记录正文。

## 保存本机敏感内容

只有敏感信息或禁止事项需要长期留在本机时，才使用以下命令：

```bash
python3 .claude/skills/memory/scripts/mem_add.py \
  --tier sealed --id <kebab-slug> --type user|feedback|project|reference \
  [--source "出处"] [--gaps "仍缺什么"] \
  --summary "一句话" --body "正文"

python3 .claude/skills/memory/scripts/mem_update.py \
  --id <id> [--summary "一句话"] [--source "出处"] [--body "正文"|--body-stdin]

python3 .claude/skills/memory/scripts/mem_lint.py
```

同一事项使用稳定 id；已经存在时更新原条目，不重复新建。正文只写可核实结论，不写分析过程、维护过程或无关个人信息。

## 边界

- 项目进展、决定和用户纠正由系统自动记录，再更新项目模型。
- 非敏感资料留在原平台、工作流资料或对应的正式文档中。
- 本说明不负责 Git 提交、发布或向外部系统同步。
