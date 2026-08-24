# Faryo Plans

本目录统一管理 Faryo 的实施计划、阶段路线图和完成证据。普通产品说明、安全说明和
交互文档继续保留在 `docs/`；只有带执行阶段、验收条件和进度记录的文档放在这里。

## Active

当前无活动计划。

## Completed or maintenance mode

- [`v1.11.11-error-contract-plan.md`](v1.11.11-error-contract-plan.md)：统一 Owner、Gateway 和
  浏览器错误契约，提供稳定错误码、可重试语义、恢复建议和隐私安全翻译；真实归档占用冲突、
  浏览器、部署和普通 reload 验收已完成。

- [`v1.11.8-mobile-history-status-reliability-plan.md`](v1.11.8-mobile-history-status-reliability-plan.md)：
  以权威 TUI JSONL blocks 修复历史边界、问题导航和复制漂移；建立动态 footer footprint、
  窄屏状态横滑、多行 composer 纵向操作组，以及 TUI/App Server 单一短名命名域和冲突
  失败关闭。source/browser/真实部署与普通 reload 验收已完成。

- [`v1.11-command-and-tool-timeline-plan.md`](v1.11-command-and-tool-timeline-plan.md)：
  把 `/rename`、`/model`、`/fast`、`/permissions` 和 Goal 等本地会话命令建模为不进入
  模型上下文、可在刷新后恢复的 command lifecycle；同时把 command execution、文件修改、
  search、MCP 和未知工具从有损文本升级为 typed activity、真正惰性的折叠分组和按需安全
  详情。

- [`v1.10-release-and-maintainability-plan.md`](v1.10-release-and-maintainability-plan.md)：
  收敛 Owner/前端 composition root；让 New/Resume 显式选择 Codex App Server 或 Codex TUI
  (tmux)，建立版本感知命令能力、状态代际隔离、浏览器 envelope、依赖更新、固定 Actions、
  CodeQL 和 Ubuntu 22.04 最低兼容门。

- [`v1.9-appserver-streaming-plan.md`](v1.9-appserver-streaming-plan.md)：以官方 Codex App
  Server 建立正文 delta、双向 RPC、单写者会话状态机、有界 SSE replay/gap 和 JSONL
  最终收敛；Owner Web 层已迁移到 Starlette/Uvicorn，同时保留 tmux/TUI 兼容、认证、可靠
  发送、长历史和移动端几何。参考与许可证边界见
  [`../appserver-streaming-reference-audit.md`](../appserver-streaming-reference-audit.md)。

- [`v1.8-mobile-keyboard-app-shell-plan.md`](v1.8-mobile-keyboard-app-shell-plan.md)：
  以单一会话滚动区、Grid 锚定的透明 composer 和浏览器原生
  `interactive-widget=resizes-content` 替换移动端 fixed/VisualViewport/VirtualKeyboard inset
  像素补偿；无线 CDP 已完成真实 Edge A/B，v1.8.3 已证明原生 resize 与最小安全聚焦间距，
  v1.8.4 实机发现缺少共享 Grid 列并已回滚；v1.8.5 透明输入区、动态尾部留白、全宽同列、
  长历史、可靠发送、服务、tmux 与 `main` 推送均已完成。

- [`v1.7-preact-transcript-migration-plan.md`](v1.7-preact-transcript-migration-plan.md)：
  已建立 Owner ConversationStore 和 Preact transcript 生命周期边界，修复折叠工作站名称与
  移动键盘几何；TanStack Virtual 量化试点因前插锚点未达标而未进入生产。source、匿名长
  历史、真实部署、普通 reload、移动/Edge 浏览器和 tmux 几何门均通过。

- [`long-conversation-rendering-plan.md`](long-conversation-rendering-plan.md)：参考 DeepSeek
  Harness 的稳定 key、可见窗口、实测高度与前插锚点契约，为长 Markdown/KaTeX 对话建立
  有界富 DOM，缓存问题导航几何并覆盖连续窗口/DPI 变化；v1.8.6 继续加入快速滚动占位、
  180 ms 停止门禁和程序滚动写入账本，保留完整逻辑历史与前插锚点。

- [`session-context-window-plan.md`](session-context-window-plan.md)：为新建和恢复 Codex 会话
  增加会话级上下文窗口预设与自定义 `K` 输入，同时清理持久 tmux 继承的旧安装环境；已完成
  source、真实 Codex、移动 Gateway、部署和 v1.6.7 发布门禁。

- [`live-stream-resilience-plan.md`](live-stream-resilience-plan.md)：为移动浏览器、网络切换和
  半开代理连接增加 SSE 心跳超时、去重安全捕获与前台自动恢复；已完成真实 Owner/Gateway
  故障注入、普通重新加载、部署和 v1.6.6 发布门禁。

- [`codex-auto-update-runtime-plan.md`](codex-auto-update-runtime-plan.md)：动态解析 NVM
  default，启动前串行自动更新，失败后继续旧版，并同步新版命令目录与 App Server；已完成
  真实新会话、浏览器、部署和 v1.6.5 发布门禁。

- [`v1.6-structured-interactions-and-owner-ui-plan.md`](v1.6-structured-interactions-and-owner-ui-plan.md)：
  统一 Codex pending interaction、动态 slash catalog、Goal/Git/history/resume 修复、Owner
  Preact/TypeScript shell、快速异步启动、普通刷新资产版本化与 queued Esc Send now；已完成
  source/browser/真实 Codex/部署门并发布 v1.6.0。

- [`github-standalone-renaming-and-discoverability-plan.md`](github-standalone-renaming-and-discoverability-plan.md)：
  已永久脱离 fork network，把独立 GitHub 仓库和本地目录统一为 `faryo-codex-web-ui`，保留上游归属
  与旧 v1.5.0 更新兼容，并发布完成搜索/目录回归修复的 v1.5.1。
- [`v1.5-unified-cli-and-service-installation-plan.md`](v1.5-unified-cli-and-service-installation-plan.md)：
  用统一 `faryo` CLI、Python 3.10+ 私有标准 venv、直接 Owner/Gateway systemd user services、
  checksum update、rollback 和 data-preserving uninstall 隐藏部署复杂度；已发布 v1.5.0。
- [`v1.4-backend-modernization-and-modularization-plan.md`](v1.4-backend-modernization-and-modularization-plan.md)：
  保留 Python/Conda 和既有安全/可靠性边界，完成 Owner/Gateway/前端职责拆分、唯一
  Starlette/Uvicorn Gateway、35 附件与量化 keyed-list Preact 采用，并发布 source-only v1.4.0；
  完整试点评估位于 [`../preact-pilot-evaluation.md`](../preact-pilot-evaluation.md)。
- [`v1.3-maintainability-and-product-capabilities-plan.md`](v1.3-maintainability-and-product-capabilities-plan.md)：
  用 Playwright/Ruff 与选择性前端库降低维护成本，拆分 Gateway portal，并实施 capability、
  脱敏 diagnostics、只读 diff review 和 body-free attention；pending queue 仅走正式协议。
- [`codebase-architecture-and-mobile-immersive-plan.md`](codebase-architecture-and-mobile-immersive-plan.md)：
  审计代码/依赖/同类项目，实施 Edge 文档滚动、可退出 Fullscreen 与 PWA 补强，并形成
  渐进重构路线。
- [`session-history-archive-plan.md`](session-history-archive-plan.md)：通过 Codex App Server
  正式 RPC 为 Session History 增加可恢复 Archive/Unarchive，不暴露硬删除。
- [`retire-project-orchestration-plan.md`](retire-project-orchestration-plan.md)：退役零配置、零数据
  且含义不清的 `/projects` 编排页面与不可达后端，让 `/` 成为唯一主页。
- [`source-only-ci-release-plan.md`](source-only-ci-release-plan.md)：source-only CI、Python/Node
  运行时发现和已发布的 `v1.2.0` 发布链。
- [`control-audit-session-state-plan.md`](control-audit-session-state-plan.md)：不记录正文的控制
  审计、明确会话状态和准确的 TUI Enter 文案。
- [`tui-control-clarity-plan.md`](tui-control-clarity-plan.md)：v1.2 的历史 raw-key 控制设计；
  已由 v1.6 的结构化 InteractionHost 和不透明 action/option 协议取代。
- [`live-tmux-reading-copy-plan.md`](live-tmux-reading-copy-plan.md)：当前轮次 180 行 Live 尾部、
  稳定 DOM/滚动/文字选择和显式复制。
- [`chat-raw-mode-switch-plan.md`](chat-raw-mode-switch-plan.md)：隔离 Chat/Raw capture cache，
  修复 Raw 切回 Chat 后仍显示终端原始内容的回归。
- [`clipboard-image-paste-plan.md`](clipboard-image-paste-plan.md)：Owner composer 直接粘贴
  剪贴板图片并复用现有压缩、预览、上传和可靠发送链路。
- [`session-history-search-plan.md`](session-history-search-plan.md)：数百条 Session History 的
  隐私安全服务端元数据搜索与过滤。
- [`directory-picker-redesign-plan.md`](directory-picker-redesign-plan.md)：Start Codex 目录选择器
  使用折叠面包屑、即时搜索、分组目录和固定主操作。
- [`copy-fidelity-plan.md`](copy-fidelity-plan.md)：回答按钮、跨块选择与单公式复制使用
  内存中的原始 Markdown/TeX，并提供安全 HTML。
- [`codex-command-completion-plan.md`](codex-command-completion-plan.md)：从当前 Codex CLI
  的真实命令面板建立 46 项、版本可审计的网页命令提示，并同步 `/rename` 标题。
- [`start-codex-runtime-plan.md`](start-codex-runtime-plan.md)：Gateway `Start Codex`
  的真实就绪、`faryoN` 命名和安全图形目录选择。
- [`full-history-navigation-plan.md`](full-history-navigation-plan.md)：单会话完整 turn 索引、
  游标分页、旧历史懒加载和全问题导航。
- [`codebase-cleanup-plan.md`](codebase-cleanup-plan.md)：收敛为 Ubuntu/Linux + Codex
  单一生产路径，删除不可达资源、旧兼容层和未验证打包链。
- [`codex-reliability-hardening-plan.md`](codex-reliability-hardening-plan.md)：Codex 长会话、
  可靠发送、安全流式认证、历史分页和内部引用展示加固。
- [`deepseek-inspired-ui-plan.md`](deepseek-inspired-ui-plan.md)：DeepSeek Harness 启发的
  Workbench v2 与 Markdown/TeX 重构计划。
- [`personal-fork-roadmap.md`](personal-fork-roadmap.md)：个人 fork 的部署、认证、实时性、
  Gateway 和公网路径总路线图。

## 管理规则

1. 每个活动计划必须写明范围、非目标、阶段、验收标准、验证证据和当前状态。
2. 计划中的真实账号、域名、Token、Cookie、会话正文和本机私有路径不得进入公开仓库。
3. 完成阶段后立即更新证据；不以“代码已写”代替测试、部署和真实浏览器验证。
4. 不修改 Codex tmux/TUI 尺寸；涉及公网身份策略时保留操作者已经确认的选择。
5. 完成的计划保留在本目录并改为维护状态，避免计划散落到仓库其他位置。
