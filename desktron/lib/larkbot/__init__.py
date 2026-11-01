"""飞书 bot 托管:收消息 → 组上下文 → 跑引擎 → 过红线闸 → 回消息。

设计稿 docs/agent-hosting-design.md。模块分工:
    route     一条消息该不该理、归到哪条会话(纯函数,可完整单测)
    context   上下文四段组装(_sealed 从不进入,红线第一道闸)
    runner    非交互跑一轮引擎
    redline   红线出站闸(第二道)
    outbound  过闸 + 发飞书(闸与出口在同一处,别处不直接调 larkapi 发消息)
    larkapi   飞书 REST(纯 urllib)
    store     声明与会话映射的持久化
    handler   把上面串成一条链
    sidecar   Node 长连接进程(bin/lark-ws.mjs)的生命周期
    api       HTTP 路由
"""
