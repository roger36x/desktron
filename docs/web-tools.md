# WebSearch / WebFetch

Desk 为公开网页查证提供受控工具。Pi 通过同一执行核心提供 `web_search` 与 `web_fetch`。Codex 和 Claude 继续使用各自原生网页能力，返回格式不保证与 Desk 证据 schema 相同。

## 架构

```text
Pi web_search
  → App Control 本地 capability 通道
  → 每次调用新建并销毁匿名后台原生浏览器标签
  → 固定 https://duckduckgo.com
  → 安全 href、结果解析、citation 与 provenance

Pi web_fetch
  → 本地无身份 HTTP(S) fetch
  → DNS/逐跳重定向校验、robots、正文提取
  → citation 与 provenance
```

本地 capability 通道在 POSIX 使用 Unix socket，在 Windows 使用专用环回 TCP；macOS 浏览器宿主为 WKWebView，Windows 为 WebView2。Linux 无界面服务不能据此认定具备原生浏览器宿主。

`web_search` 不使用用户 Cookie、SSO 或浏览器身份，也不切换用户当前标签。后台浏览器不可用时返回 `browser_unavailable`，不会静默降级到其他搜索传输。每次调用都在 `finally` 中关闭 Agent 自建标签，这类临时标签不写入浏览器标签持久记录，也不出现在”重新打开已关闭标签”中。

`web_fetch` 继续使用直连 HTTP，不做浏览器兜底。任意页面都可能把原生浏览器重定向到内网，因此在原生导航层支持等价的逐跳公网解析校验前，不能把抓取 URL 交给浏览器。

## 搜索约束

搜索查询先经过公网外发检查。允许后，工具通过 App Control 的本地通道及会话 capability 打开匿名隔离标签；令牌不进入命令行、日志或证据。搜索页主框架仅允许 HTTPS 精确主机 `duckduckgo.com`，子资源按系统浏览器策略加载。

文档观察只对真实链接节点暴露清洗后的绝对 `href`：按所在文档的 `baseURI` 解析相对地址，拒绝 userinfo，删除 fragment，并整体省略含常见凭证参数的链接。DuckDuckGo 的 `uddg` 跳转由 adapter 解包，最终 URL 再经过 Web 核心的 URL、敏感参数和公网 DNS 校验后才进入 provenance。遇到验证码、结构异常、导航不匹配、清理失败或宿主不可用时均返回稳定错误。

结果以 DuckDuckGo 的每个结果 `<article>` 为单位解析：标题链接取自 `h2`；摘要取 `article` 文本中网址路径与标题之后的部分，因为页面快照里摘要正文没有独立节点，只剩加粗的查询词片段。摘要开头的日期写入 `published_at`：绝对日期规范为 `YYYY-MM-DD`，“2 天前”等相对日期保留原文，视频时长前缀会去掉。DuckDuckGo 自身的视频、图像等模块（结果网址仍在 `duckduckgo.com`）、广告及重复网址都不计入结果条数。页面没有 `article` 时，退回按标题分组的旧解析。

搜索摘要只用于选源，不能直接作为已核实事实；引用事实前应使用 `web_fetch` 读取原网页。

## 搜索链路预算与诊断

macOS 原生宿主以人工委托租约判定空闲，可能把匿名自开页的连续操作也当作空闲：1.5 秒轮询且 browser/desk 轮转，每类最长约 3 秒一轮。新版宿主收到实际操作后维持 5 秒短突发，以 0.35 秒节奏公平轮转，无活跃租约且突发到期后恢复低频；不增加重叠请求，不改变审批或租约规则。此项须激活包含短突发实现的 macOS 原生壳才生效，不能用后端源码实测替代。Windows 当前按人工委托租约切换 1.5／0.35 秒轮询，尚无对应短突发状态，不能套用 macOS 的结论。

Web tool `1.1.1` 保留每次调用独立匿名标签，不共享 Cookie、页面或搜索结果缓存。搜索先立即观察；只有页面未就绪、查询尚未匹配、结构未知或结果不足且未稳定时，才在同一租约内最多观察 3 次，后两次页面稳定等待预算分别为 300/800ms。初次快照最多 1200 个节点，截断时后续扩大到 4000；验证码与离开固定主机立即失败，不重发搜索、不切换传输、不绕过风控。确定的空结果与未知页面结构分别返回成功空数组和错误。

解析一次建立文章分组与节点位置索引，支持无显式 heading role 的 h2 及文章内 h3 标题；仍过滤广告、自有模块、重复和危险 URL。结果公网 DNS 校验最多 3 路并行，保持原排序；仅本次观察内复用同源解析，Fetch 每次重新解析、校验并绑定真实连接。

搜索成功与已取得阶段数据的失败可附 `diagnostics`：开页、观察、解析、清理、provider 总耗时、结果 URL 校验耗时，以及观察次数、节点数、截断和清理失败标记；不包含正文、查询副本、路径或 capability。`provider_ms` 包含开页至清理，不包含核心层 URL 校验；排队时间仍计入总调用超时。取消/超时不重试新搜索；开页创建请求不因调用方取消而截断有界的创建回执，取得租约后独立清理。清理失败不得当作搜索成功。外层取消先返回、底层仍在执行时标记 `operation_pending`；创建回执彻底丢失且无法确认租约时标记 `open_outcome_unknown`，不推断已关闭、不重发开页。自动恢复该窗口仍需 Runtime 关联号协议。本地通道请求同时受绝对期限和空闲超时约束。

这些改动不能保证 DuckDuckGo 的固定额度或解除服务端拦截。来源质量、单次链路耗时、长期成功率必须分别验收，真实数据不得用局部或合成测试替代。

## 证据与安全边界

机器可校验 schema 位于 `desktron/lib/web_tools/schema.json`。成功与失败均包含：

- `schema_version`、`tool_version`、`tool`、`status`；
- `provider` 和 `transport`：搜索为 `app_control_browser`，抓取为 `direct_http`；
- `content_trust=untrusted_web_content`；
- `browser_recommended` 与稳定 `failure_reason`。

核心层继续负责：

- 查询外发检查、会话调用预算、超时和并发限制；
- 搜索结果 URL 二次校验、citation 与 provenance；
- `web_fetch` 的 DNS 全结果校验、固定已核验 IP、保留/私网/metadata 地址阻断；
- `web_fetch` 重定向逐跳重验、HTTPS 降级拒绝及 robots fail-closed；
- 响应、解压比例、输出量和抓取深度限制。

`web_fetch` URL 必须来自用户输入、当前分支搜索结果或已读取页面链接。查询分类器阻断内部资料、凭证、内网地址、邮箱与手机号；它不替代组织的数据防泄漏（DLP）措施。内网域名由 `DESKTRON_INTERNAL_DOMAINS`（逗号分隔）声明，命中的地址与查询同样被阻断。

## 启用与验证

功能默认开启；设置 `DESKTRON_WEB_TOOLS=0` 可关闭 Pi 的两项工具。无需 API key。

```bash
node --test \
  desktron/tests/app_control_client.test.mjs \
  desktron/tests/browser_search.test.mjs \
  desktron/tests/browser_search_optimization.test.mjs \
  desktron/tests/web_tools_core.test.mjs \
  desktron/tests/pi_web.test.mjs

python3 -m unittest \
  desktron.tests.test_app_control_runtime \
  desktron.tests.test_app_control_browser_ac \
  desktron.tests.test_app_control_native_bridge \
  desktron.tests.test_app_control_background \
  desktron.tests.test_app_control_unix_transport

xcrun swiftc -typecheck \
  desktop/native/backend_supervisor.swift \
  desktop/native/native_control.swift \
  desktop/native/main.swift
```

真实浏览器验收必须使用包含对应平台宿主及 DOM 改动的新版原生壳；上述 Swift 检查仅适用于 macOS，Windows 构建和验收见 [Windows 说明](windows.md)。仅同步后端或重启后端不能证明功能生效；未经用户确认，不安装、不重启、不部署。
