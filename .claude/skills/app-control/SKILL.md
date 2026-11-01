---
name: app-control
description: 操作 Desk 内置浏览器，包括打开和读取网页、填写、点击、按键与截图。默认使用不带用户身份的独立页面；任务确需登录时，按说明取得用户同意后再使用身份。
---

# App Control

Desk 通过 Runtime 强制租约、权限、幂等和回执。使用 `app-control` CLI；不要调用
`/host/*` 端点、注入任意 JavaScript 或尝试取得宿主令牌。

## 两种拿到页面的方式

**自己开页(默认选它)。** `app-control open <url>` 直接开一个隔离标签并持有它，不需要用户
点任何按钮：那个标签用独立存储，没有用户的 cookie 与内网 SSO，能力上限等于一个刚装好的
浏览器。公网查资料、读文档、填公开表单都走这条。

`app-control open <url> --with-identity` 才用用户的登录态（内网站点需要）。首次会由 Desk
弹确认框问用户，批准后该站点记住一天，之后同一站点不再逐个标签问。授权站点清单
不向 Agent 暴露；用户在 Desk 设置中查看和撤销。没授权时不要反复重试，
也不要改用隔离标签绕过登录墙。

**接管用户的页面。** 用户已经打开、并且要你在他那一页上干活时，他点“交给 Agent”，你再
`app-control claim`。这条挡的不是“操作网页”，是“用你的身份操作网页”，所以必须由人发起。

## 使用流程

1. `app-control list` 确认应用与动作可用；`app-control tabs` 看本会话持有的标签。
2. 自己开页用 `app-control open`；要接管用户已打开的页面才等他点“交给 Agent”，然后
   `app-control claim`。若提示没有委托，停下并请用户点击按钮。
3. `app-control inspect document` 读结构化页面。返回的是父子层级的节点树，带 role、
   name、states、state_sources、selected_values、frame_id，以及 modal/menu/popover stack。
   节点树含正文结构：段落、列表、表格、章节、图注、代码块都有自己的节点和角色；
   canvas、svg、video、audio、iframe 也进树并带尺寸位置，看到它们说明该处内容读不出来，
   要改用截图。顶层 `text` 字段保留原页面的换行，可直接当正文读。页面大时用
   `--input '{"find":"关键字"}'` 只取命中及其祖先，`root_node_id` 只读子树，
   `include_offscreen` 连视口外一起读；`truncated` 为真说明节点被 `max_nodes` 截断，
   可调大 `max_nodes` 或改用 `find` / `root_node_id` 收窄。之后的节点
   动作携带返回的 `observation_version`；导航或重绘造成版本过期时重新观察。
4. 填写、选择、输入、悬停、滚动，以及 `dblclick`、`check`、`uncheck`、`focus`、`blur` 用
   `app-control act <动作> --input '<JSON>' --expected-version <版本>`。拖拽用 `drag`，输入分别提供
   `source` 与 `target` 两个稳定目标。
   目标可用 `node_id`（本次观察内有效）、`selector`，或更稳定的 `locator`（支持
   `role`、`name`、`text`、`test_id`、`selector`、`exact` 与 `nth`；`nth` 从 0 开始，-1 为最后一个）。
   目标动作会自动等待元素出现、可见、启用和稳定；指针动作会在目标内部寻找未遮挡点，
   失败时回传遮挡节点和候选点，绝不穿透。`fill` 只改值不提交；`click`、`hover`、
   `dblclick`、`check`、`uncheck`、`drag`、`type` 与 `press` 都由宿主送真实鼠标/按键事件。
5. `click`、`dblclick`、`check`、`uncheck`、`drag`、`press`、`coordinate_click` 等可能造成外部写入的动作必须带唯一
   `--idempotency-key`，并用 `--intent "一句话说明这次执行目的"` 告诉用户具体在审批什么。
   `--intent` 写业务目的，不复述“点击页面元素”等技术动作；它只用于说明，不改变权限等级。
   Runtime 会等用户确认后才执行。`coordinate_click` 只在结构化节点
   拿不到目标时用，必须带最近一次 inspect 的版本。不要用别的动作模拟提交。
6. 导航用 `navigate` / `back` / `forward` / `reload`；`activate` 确认后台执行目标，不切换用户前台；
   需要人继续原表单时用 `handoff`，它经用户确认后显示并接管租约绑定的原标签；不要重开 URL。`close`
   关掉标签（要确认）。`wait` 可等 `text`、`selector`、`url`（`url_mode` equals/contains/prefix）
   或 `network_idle`，配 `timeout_ms`。网络空闲会同时等待文档/DOM 安定和同源页面里的
   Fetch/XHR 清零；跨域 iframe、WebSocket、图片和字体请求不在这项统计内。
7. 动作回执里 `requires_reinspect` 为真时先重新 inspect 再继续。用
   `app-control receipt <operation_id>` 取最终回执；失败时用 `app-control trace <operation_id>`
   查看等待次数、目标矩形、遮挡命中、页面归属与事件时间线。trace 会隐藏输入文字和表单值。
8. 工作结束后 `app-control yield` 归还控制权。

自开页默认后台运行，动作按租约的 `resource_id` 定向执行，不需要用户切标签或把 Desk 放在
前台。用户可从浏览器标签栏查看页面。后台兼容性因网站而异；依赖前台可见性或系统弹窗的
流程不保证可后台完成。明确遇到此类阻碍时报告并交人，不自动抢前台，不绕审批。

截图用 `app-control inspect screenshot`。自开的隔离标签（`open` 默认那种）与读文本同档，
不再逐次请求确认；带身份的标签（`--with-identity`）和用户交给你的那一页仍逐次确认。
不论哪种资源，截图都会遮挡密码与令牌类输入。要把截图给用户看，在回复正文里写
`![说明](/workbench/view?raw=1&path=<回执里的 path，需 URL 编码>)`，聊天区会内嵌渲染、
点击可看原图；`src` 只认这个本机接口，写外链会降级成一行链接文字。`--input '{"full_page":true}'`
截整页，`--input '{"clip":{"x":0,"y":0,"width":800,"height":600}}'` 截可视区的一部分。
结构化快照仍是默认观察方式。
资源加载排障用 `app-control inspect network --input '{"limit":200}'`；它返回同源页面的资源时序、
类型、传输大小与协议，地址只保留 origin + path，不提供查询串、请求头或响应正文。
条件等待可用 `wait` 的 `selector` 或 `locator`，配合
`attached / detached / visible / hidden / enabled / disabled / checked / unchecked / mixed` 状态，
也可用 `value` / `values` 验证选择结果；多选框用
`select --input '{"selector":"#team","values":["A","B"]}'`，单选仍使用 `value`。

发布、多层确认等异步流程：在动作 input 中加入 `wait_for`，字段与 `wait` 相同，例如
`{"selector":"#publish","wait_for":{"selector":"[role=dialog]","state":"visible","timeout_ms":10000}}`。
先逐次审批并执行动作，再只读探测条件；`action_executed:true` 不等于 `condition_met:true`。
没有后置条件的指针回执会标为 `outcome:delivery_only`、`effect_verified:false`；不能据此宣称业务完成。
`postcondition_timeout` 时查询回执、重新观察，不换幂等键重复点击。

长等待用 `app-control act wait --timeout 0 --input '<条件>'` 立即取得 operation_id，随后用
`app-control status <operation_id>` 短查询。条件 `timeout_ms` 最长 1800000（30 分钟），
Runtime 每轮探测约 1 秒，重启保留原期限，只恢复读取，不重放写动作。可用 `state:enabled`
等待继续按钮解锁；`abort_if` 指定失败提示的 CSS 选择器，出现时返回 `wait_aborted`；
`guard_url` 绑定完整业务页 URL（含查询串和 hash），变化时返回 `resource_changed`。
只有明确预期导航时才省略 URL 约束。标签消失、用户介入或租约失效都会结束等待。

## 交给人的情况

系统弹窗（alert/confirm/prompt）、文件选择、下载保存位置由用户处理：宿主会把租约暂停并在
回执里标 `needs_user`，人处理完交还租约，但旧排队动作不会续跑。先重新 `inspect document`，
核对 URL、工单或发布对象，再决定下一步；`human_intervention_reinspect_required` 表示还未完成
这次复核。快照里 `capabilities` 与 `opaque_frames` 说明哪些
部分读不到（跨域 iframe）。遇到这些就停下来告诉用户，不要绕。

用户实际点击、输入、导航或收回控制后租约暂停或失效；自开页仅被查看后切走仍可后台运行，
真人已介入的暂停页切走则等待用户切回。收到 `lease_revoked`、
`stale_or_missing_node`、`locator_ambiguous`、目标等待超时或版本冲突时停止当前动作，不自动绕过
或重复提交。可操作性失败会明确返回不可见、禁用、不稳定或被遮挡，不会强行点击。
租约报 `browser_tab_unavailable` 时标签视图已不在，重新 open 或请用户重新委托。

## 原生应用窗口（只读）

`app-control list` 里带 `native_application` 的应用（如 `shadowrocket`）只开放 L1 观察。
用户在 Desk 设置的“调试”区打开对应开关并在弹窗里选定窗口后，`app-control --app <应用> claim`，
再 `app-control --app <应用> inspect window`。返回的是 `desk.native-window/v1` 结构：节点只有 `ref`、`parent`、
`role`、`semantic`、`enabled`、`selected`，以及清单预先登记的取值令牌（如 `on`、`status_connected`）；
不含窗口标题、文字原文、节点名称、订阅或凭证。读不到的信息就是不开放，不要尝试截图、OCR、
AppleScript 或坐标点击。用户操作该窗口时租约暂停，回到空闲后重新 inspect；目标关闭、进程
重启或身份变化后租约撤销，请用户重新选择窗口。原生应用没有任何动作。

## 固化成可复用步骤

一段流程跑通后，`app-control sequence capture > seq.json` 导出本会话里可移植的成功动作
（只含 selector/locator 目标，不含一次性 node_id 与表单值），`app-control sequence run seq.json
--run-id <稳定标识>` 按原有审批与回执规则重放，`app-control sequence skill <名字> seq.json`
把它固化成本地 Skill。

敏感输入返回 `sensitive_field_needs_user` 时交给用户填写。不得请求或输出密码、Cookie、
Token、请求头、LocalStorage 或凭证值。
