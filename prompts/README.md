# Prompt 注入治理

这里登记 Desk 向模型注入的内容及其投递方式。第一阶段只提供清单、校验和观测，不改变任何引擎的运行行为。

## 文件

- `manifest.yaml`：注入语义及差异。只引用稳定的 Segment/Resolver ID，不保存代码路径，也不展开六种执行方式；`refresh` 表示正文多久重新计算，`lifecycle` 表示语义在模型侧持续多久。
- `capabilities.yaml`：通用能力、三个引擎差异、两种运行方式差异及少量交叉覆盖；编译器再展开成六种执行方式。
- `../desktron/lib/prompt_sources.py`：稳定来源 ID 到当前 Markdown/Python 实现位置的唯一映射。代码重构只维护这里。
- `../schemas/prompt-*.schema.json`：供编辑器、评审和外部工具使用的结构定义。
- `../desktron/lib/prompt_governance.py`：运行时无第三方 Schema 依赖的严格校验、矩阵展开与 JSON 编译器。

## 查看

```bash
# 校验清单，只输出段落元数据和哈希
desktron/bin/desk-prompt --root .

# 查看某种执行方式当前计划收到的内容
desktron/bin/desk-prompt --root . --target claude-shepherd

# 同时查看尚未接线的计划项
desktron/bin/desk-prompt --root . --target claude-shepherd --include-planned
```

默认输出不含 Prompt 正文。`--full` 只用于本机审查，会输出可静态解析的正文。

## 规则

1. 一段语义只有一个稳定 ID 和一个权威来源；YAML 保存语义，`prompt_sources.py` 保存实现位置，引擎专属内容只描述差异。
2. `current` 表示当前代码已经投递，`planned` 表示已知缺口，不能把目标状态写成现状。正文重新计算不等于重新注入，必须分别声明 `refresh` 与投递 `cadence`。
3. 新增引擎或运行方式时，修改默认值与差异配置，由编译器展开完整矩阵；禁止手写笛卡尔积结果。
4. YAML 使用严格子集：禁止锚点、别名、合并键、重复键、隐式日期和未加引号的 `yes/no/on/off`。
5. 动态生成内容只登记解析函数，不在 YAML 中复制运行时正文。
6. 适配器接入清单之前，先比较编译结果与现有字符串拼装；行为变化必须单独评审和发布。
