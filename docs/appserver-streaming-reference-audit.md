# Codex App Server 流式架构参考审计

状态：2026-08-22 已完成首轮审计，实施期间持续校验。

本文只记录公开仓库、公开提交和可公开的技术结论，不记录部署域名、账号、Token、
Cookie、会话正文或本机目录。Faryo 对参考项目采用 clean-room 架构借鉴，不复制其代码。

## 固定参考快照

| 项目 | 固定提交 | 用途 | 许可证边界 |
| --- | --- | --- | --- |
| [YepAnywhere](https://github.com/kzahel/yepanywhere) | `b1091fb05d021c7044af5b41fe15f2d754ea659e` | Codex delta 聚合、稳定消息身份、local-command system row、typed tool fallback 和长历史 | README 标注 MIT，但该快照根目录没有标准许可证文件；只借鉴公开架构思想，不复制代码 |
| [HAPI](https://github.com/tiann/hapi) | `be1ef2a2e4d6d8836ca28695d86ccff47d0c03a3` | 双向 App Server RPC、typed tool begin/end、工具分组/详情和 Web 交互边界 | AGPL-3.0-only；只做行为和协议研究，不复制实现 |
| [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) | `b150a551b8d465e31e418e1b2eaf5e79bbb7d28e` | command run/done 独立生命周期、session title event 和 log-only command 边界 | 只借鉴公开事件模型；不复制实现或视觉资产 |
| [OpenAI Codex](https://github.com/openai/codex) | `ad9e8097fd3d0d2f1c1166575d2c6cd8cb9e1833` | App Server 官方协议、Unix socket、多连接、线程生命周期 | 上游官方源码与本机版本生成 schema 是协议依据 |

两个第三方仓库均以完整工作树的浅克隆保存于仓库外的参考区，未加入 Faryo Git 历史、
构建上下文或发布包。固定提交用于让审计结论可复核，后续上游变化不会静默改变本计划。

## OpenAI App Server 的已确认能力

本轮实现以 `codex-cli 0.149.1` 生成的 JSON Schema 为运行时基线。协议是版本化且仍在
演进的实验接口，不能把字段集合写死为永久不变的产品契约。

已从官方 README、生成 schema 和官方源码确认：

- App Server 使用双向 JSON-RPC；每条连接必须完成 `initialize` / `initialized`。
- `thread/start`、`thread/resume` 和 `turn/start` 是正式会话路径。
- 活动 regular turn 的追加输入使用带 `expectedTurnId` 的 `turn/steer`；不能用第二次
  `turn/start` 冒充网页队列。
- `item/agentMessage/delta` 提供回答正文增量；`item/completed` 提供同一 item 的最终值；
  `turn/completed` 提供终态和 token usage。
- Unix socket transport 在一个长期进程中接受多个 WebSocket 连接；慢连接有有界队列并会
  被主动断开，不会无限积压内存。
- 连接关闭只移除该连接的订阅和待处理 RPC；App Server 进程和活动线程不会随之退出。
- 最后一个订阅者离开后，空闲线程会延迟 30 分钟卸载；活动 turn 可以继续执行。
- 重连可以用 `thread/resume`、`thread/read`、`thread/turns/list` 和 `thread/items/list`
  恢复权威状态，但 App Server 不承诺为新连接重放断线期间的每个正文 delta。
- 同一个持久 thread 只能由一个 App Server 进程持有写锁；另一个进程 resume 会失败。
- Unix socket lifecycle daemon 仍是 experimental，并假定 Codex 由官方 standalone
  installer 管理。Faryo 当前不能把这个安装假设强加给所有 npm/NVM 用户。

因此 Faryo 不自造 Codex Runtime Host，而是用独立的用户级服务直接监督官方
`codex app-server --listen unix://…`。Faryo CLI 每次启动服务时动态解析受支持的 Codex
launcher，避免把某个 NVM 版本目录写死到服务文件中；Owner 只作为协议客户端连接私有
socket。Owner 重启不会杀死活动 turn，Codex 升级也不会在活动 turn 中途替换运行时。

## 从 YepAnywhere 吸收的设计

### 1. 实时尾部与持久历史分工

YepAnywhere 将 provider JSONL 视为持久真相，将实时 provider stream 视为短暂尾部。
Faryo 采用同样的责任划分：

- delta 让用户立即看到回答，不承担长期存储职责；
- final item 使用稳定 identity 覆盖 streaming item；
- JSONL / App Server 历史页负责刷新、冷启动和断线后的最终收敛；
- 不维护一份与 Codex 竞争的正文数据库。

### 2. 高频更新不驱动整棵 UI

YepAnywhere 的客户端将高频 token 更新限制在叶节点，并按负载自适应节流；Markdown、
KaTeX 与代码高亮按闭合块和最终内容处理。Faryo 的 Preact 迁移采用相同原则：

- `Map<itemId, accumulator>` 原地聚合 delta；
- 每帧或短批次只发布一次尾部 revision；
- 已完成历史块保持稳定 DOM identity；
- 流式阶段只渲染可确认闭合的 Markdown/TeX，完成后执行一次完整富文本收敛；
- 快速滚动期间沿用现有有界富 DOM 和脱水策略。

### 3. 原始事件优先、增强异步完成

实时正文不得等待语法高亮、diff 摘要或其他富化步骤。任何较慢的增强结果都用同一稳定
identity 追加 revision，不能阻塞原始消息进入浏览器。

### 4. 会话命令与模型正文分离

YepAnywhere 把本地 slash command 投影成独立 system message，不把它伪装成用户 prompt。
Faryo 使用同一职责边界，但为 mutating Web command 建立专门的 command lifecycle：稳定 id、
`running/waiting/completed/failed`、安全摘要和最近 turn 锚点。读取型 `/usage` 等面板不产生
持久噪声，Goal objective 和未知自由参数不进入该日志。

## 从 HAPI 吸收的设计

### 1. 真正双向的协议客户端

HAPI 的客户端长期消费 JSONL RPC，分别处理 response、notification 和 App Server 发来的
request。Faryo 现有同步 helper 会在等 response 时丢弃通知，也无法正确回答审批或用户输入
请求，本次必须替换为一个单读循环、多 pending future、显式 server-request handler 的
异步客户端。

### 2. SSE cursor、replay 与 gap

HAPI 的 Hub 使用有界事件环、epoch/sequence cursor 和 replay gap。Faryo 采用等价但独立的
实现：

- cursor 为 `{epoch}:{sequence}`；
- 环同时限制事件数和总字节数；
- `Last-Event-ID` 在窗口内时按序重放；
- cursor 过旧或 epoch 不同时发送 `gap`，随后发送权威 snapshot；
- replay 写出期间产生的新事件先排队，再接回 live，避免重放/实时交界丢事件。

### 3. 会话所有权互斥

HAPI 在 local 与 remote launcher 间做显式切换，而不是让两个输入端并发驱动同一进程。
Faryo 将同一思想落实为 `Codex App Server` 与 `Codex TUI (tmux)` 两种后端：

- 现有 tmux/TUI 会话继续由 `Codex TUI (tmux)` 驱动；
- 新会话默认由 `Codex App Server` 驱动，也可显式选择 TUI；
- 两者共享历史入口和视觉组件，但不共享写权限；
- 未证明线程已空闲、无草稿、无审批、无活动 turn 且原 writer 已释放前，禁止启动第二个
  独立 writer；
- Codex 0.149.1 的 TUI `--remote unix://...` 可以在 Web actor 已关闭但 resident App Server
  尚未卸载时复用同一 writer。Faryo 仅采用这种官方单 writer attach，不伪造 lock handoff。

旧 registry/wire 值只在集中兼容适配层读取；领域代码使用
`APP_SERVER`/`CODEX_TUI`，用户界面不显示旧协议名称。

HAPI 当前 Codex converter 会在正文 delta 到达时只更新“正在输出”状态，并在 item complete
后一次性提交正文。因此它不是 Faryo 正文流式实现的直接范本；Faryo 直接消费官方
`item/agentMessage/delta`。

### 4. Typed tool 分组与按需详情

HAPI 保留 command/file/MCP 等 begin/end 类型，并把连续工具放进可折叠组；YepAnywhere 也
保留 tool-use/tool-result identity，并为未知工具提供通用 fallback。Faryo clean-room 实现为：

- 主 capture/history 只携带 type、status、短标题、计数和 detail capability；
- 运行、等待、失败项目无需展开即可识别，已完成组默认折叠；
- output、result 和 diff 只在认证后的单 item 请求中有界投影；
- `thread/read` 省略旧工具时，从 Codex rollout 只读恢复同一 item 的摘要和详情；
- reasoning body 永不进入工具详情。

DeepSeek Harness 的 command registry 进一步证明命令控制事件应由 session/runtime 配对，而
不是靠 assistant 文本回显。Faryo 借鉴这一事件边界，视觉上仍采用自身的紧凑 system row。

## 未采用的方案

- 不把浏览器直接连到 Codex Unix socket：认证、协议兼容、审批与路径权限必须留在 Owner。
- 不直接暴露 experimental WebSocket listener：官方明确标为 unsupported；只使用本机私有
  Unix socket。
- 不复制 HAPI 的 React/TanStack/assistant-ui 整套栈：Faryo 已有 Preact 和经实机验证的移动
  app shell，整体重写会扩大回归面。
- 不把每个 token 写入 Faryo 数据库：它会制造第二份不可靠的历史真相并增加隐私面。
- 不继续用 tmux 文本猜测来驱动新 Web 会话：终端捕获只保留给既有兼容会话。
- 不让独立 TUI App Server 与 Faryo App Server 同时写同一 thread。TUI 的官方 `--remote`
  模式只是连接现有 App Server，不是第二个 writer。

## 需要持续验证的上游变化

每次支持新的 Codex CLI 版本时，CI 或 `faryo doctor` 至少检查：

1. 能生成当前版本 schema，且必需 method/notification 仍存在；
2. initialize 能力和 server request union 能被兼容解析；
3. 未知 notification 被记录计数但不会让 reader loop 崩溃；
4. 未知 server request 默认 fail closed，并向网页给出可理解的不可用状态；
5. `item/agentMessage/delta` 与 `item/completed` 的 stable identity 收敛仍成立；
6. 断开 Owner 后 turn 继续，重连后最终历史可恢复；
7. 独立 App Server 与 TUI 双写尝试被明确拒绝；resident writer 保留期内的 TUI resume
   必须使用官方 `--remote` 复用同一 writer。
