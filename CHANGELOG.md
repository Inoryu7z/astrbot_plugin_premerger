### v2.0.6

**🐛 修复对话历史保存失败**

* 修复 `_save_conversation` 中 `ImagePart` 导入错误：AstrBot 框架中不存在 `ImagePart`，正确类名为 `ImageURLPart`。
* 修复图片消息构造方式：`ImagePart(url=url)` / `ImagePart(file=url)` 改为 `ImageURLPart(image_url=ImageURLPart.ImageURL(url=url))`，与框架 API 对齐。

### v2.0.5

**🐛 修复消息丢失致命 bug & 增强失败恢复**

* 修复 `_direct_llm_call` 失败时 `sessions.pop()` 销毁整个会话导致所有合并消息丢失的致命 bug：现在调用 `_reset_session_for_retry` 将消息放回缓冲区，等待新消息触发时走框架管道（享受框架 fallback 机制）。
* 修复 `_direct_llm_call` 返回空响应时直接销毁会话的问题：现在将消息放回缓冲区等待恢复，不再重试主模型（避免越权绕过框架 fallback、加剧限流风险）。
* 修复 `_direct_llm_call` 获取 provider 失败时直接销毁会话的问题：现在将消息放回缓冲区等待恢复。
* 新增 `_reset_session_for_retry` 方法：将合并消息安全放回缓冲区，保留 `interrupted` 标志防止被中断的原始 LLM 响应泄露。
* 修复条件判断不一致：统一使用 `session.get()` 替代混用的 `"key" in session` 模式。
* 修复 KeyError 风险：`pending_text`/`pending_images` 回填时使用 `setdefault` 确保 `buffer`/`images` 键存在。
* 提取 `__version__` 常量，消除版本号硬编码重复。

### v2.0.4

**🐛 修复多个严重 bug**

* 修复僵尸会话清理后消息丢失：`_cleanup_session` 后缺少 `return`，代码穿透导致消息被 `stop_event()` 吞掉。
* 修复 `on_llm_response` 对无 session 用户清空 LLM 响应：发送指令的用户、群聊未启用的用户的 LLM 回复被静默吞掉。
* 修复 `_debounce_then_retry` 过早清除 `interrupted` 标志：可能导致原始 LLM 响应未被丢弃，用户收到重复回复。
* 修复 `on_llm_response` 丢弃中断响应后未重置状态：`interrupted` 永远为 `True`，后续正常响应可能被误判为中断而丢弃。

### v2.0.3

**🐛 修复图片/表情包丢失问题**

* 修复 `pending_images` 回填条件错误：空列表 `[]` 的布尔值为 `False`，导致中断时图片无法从 `pending_images` 回填到 `images`，改用 `"pending_images" in session` 检查。
* 修复 `_save_conversation` 不保存图片信息：现在用户消息中会包含 `ImagePart`，图片对话历史不再丢失。
* 修复 `_build_contexts` 在纯图片消息（无文本）时追加空 user 消息的问题：`merged_text` 为空时不再追加。

### v2.0.2

**🐛 修复对话历史读写失败**

* 修复 `self.context.conversationManager`（驼峰命名）不存在导致对话历史读取和保存均失败的 bug：正确属性名为 `conversation_manager`（下划线命名）。
* 改用 `getattr(self.context, "conversation_manager", None)` 安全访问方式，兼容旧版 AstrBot。
* 修复对话历史不可用时 `_build_contexts` 缺少当前用户消息的 fallback 处理。

### v2.0.1

**🐛 修复中断机制多个严重 bug**

* 修复 `on_llm_request` 钩子重置 `interrupted=False` 导致中断标志被清除，`on_llm_response` 永远无法拦截被中断的 LLM 响应的致命 bug。
* 修复中断重试时原始消息丢失的问题：新增 `pending_text`/`pending_images` 机制，防抖结算时保存已发送的合并文本，中断发生时回填到 buffer 头部，确保重试请求包含完整上下文。
* 修复 `_cleanup_session` 未设置 `flush_event` 导致 `handle_message` 协程永久挂起（死锁）的严重 bug。
* 修复 `on_llm_response` 在 session 不存在时未调用 `stop_event()`，导致 max_retry 达上限后旧 LLM 响应泄露给用户的 bug。
* 修复 `_debounce_then_retry` 和 `on_llm_response` 残留消息分支中 `interrupted` 标志未重置为 `False`，可能导致后续正常响应被错误拦截的问题。
* 中断时清空 `resp.completion_text`，防止 PostSplitter 部分发送被中断的响应。
* 改善 `_build_contexts` 错误日志级别，从 `debug` 改为 `warning`，便于排查上下文构建问题。

### v2.0.0

**🔄 架构重设计：中断立即重试，不再依赖 on_llm_response 触发**

核心变更：

* 中断时立即启动防抖+重试（`_debounce_then_retry`），不再等待 `on_llm_response` 来触发重试。
* `on_llm_response` 钩子仅负责丢弃被中断的框架 LLM 响应（`stop_event()`），不再承担重试职责。
* 新增 `after_message_sent` 钩子，在框架正常发送回复后清理会话，防止僵尸会话。

关键修复：

* 新增 `llm_generation` 代际计数器：每次中断递增，`_direct_llm_call` 完成后检查代际是否匹配。若已被新中断取代，自动丢弃过期响应，避免用户收到多条重复回复。
* 修复 `_direct_llm_call` 无条件 `sessions.pop()` 导致新中断的 debounce 状态被意外清除的严重 bug。
* 修复 `retry_count` 在 `_debounce_then_retry` 中被重置为 0 导致永远达不到 `max_retry_count` 上限的 bug。
* 修复 `on_llm_response` 残余缓冲区处理时未设置 `llm_in_progress = True`，导致新消息走错分支的 bug。
* 修复 `_direct_llm_call` 完成后若有残余缓冲区消息直接丢失的问题：现在会启动新的 `_debounce_then_retry` 处理残留消息。
* 移除 `session_timeout` 用户配置项，改为内部安全网 `_ZOMBIE_TIMEOUT = 60` 秒。僵尸会话检测仅作为最后防线，正常流程不应依赖超时清理。

### v1.4.0

**🐛 修复僵尸会话导致不回复的严重 bug**

* 新增会话超时机制：当会话处于 `llm_in_progress` 状态超过 `session_timeout`（默认 120 秒）后，强制清理会话。
* 根因：`on_llm_response` 钩子在某些情况下不会触发（如事件被其他插件终止），导致会话永远卡在 `llm_in_progress = True`，后续所有消息都被当作"中断"处理并被 `stop_event()` 吞掉，形成死锁。
* 新增 `session_timeout` 配置项，可在管理面板中调整。
* 新增 `llm_start_time` 字段跟踪 LLM 请求开始时间，用于超时检测。
* 修复会话超时后被清理后，新消息无法正确创建新会话的问题（`_cleanup_stuck_session` 后继续走新会话逻辑）。

### v1.3.0

**🐛 修复防抖结算后 session 过早清理导致消息无法合并的严重 bug**

* 防抖结算后不再 `pop session`，而是保留 session 并标记 `llm_in_progress = True`。
* 这样后续消息到来时能正确识别到"LLM 正在进行中"，从而触发中断重试逻辑。
* 修复前：三条连续消息被分成三次独立防抖结算，完全没有合并。
* 修复后：第一条消息防抖结算后 session 保留，后续消息能正确进入中断重试流程。

### v1.2.0

**🐛 修复最大中断次数逻辑 & 增强健壮性**

* 修复达到最大中断次数后仍调用 `stop_event()` 导致新消息被吞的 bug：现在达到上限后放行新消息由框架正常处理。
* 修复达到最大中断次数后 `interrupted=True` 已被设置但缓冲区消息永远不会被处理的 bug：重构了判断顺序，先检查重试次数再决定是否中断。
* 新增后台任务跟踪机制 `_track_task`/`_untrack_task`，插件卸载时正确取消所有后台任务。
* 补充所有静默异常的日志记录（debug/warning 级别）。
* 新增配置值验证：`debounce_time` 和 `max_retry_count` 不允许为负数。
* 为所有方法添加返回类型注解。

### v1.1.1

**🐛 修复中断重试相关 bug**

* 修复中断重试时图片丢失的问题：改用 `provider.text_chat()` 替代 `context.llm_generate()`，正确传递 `image_urls`。
* 修复达到最大中断次数后缓冲区消息丢失的问题：当前响应放行后，缓冲区残留消息会作为后续请求自动处理。
* 修复对话历史无法保存的问题：使用正确的导入路径 `astrbot.core.agent.message`。
* 补充静默异常的日志记录（debug/warning 级别）。
* 提取 `_merge_buffer` 方法消除重复代码。

### v1.1.0

**🔄 架构重构：兼容框架插件生态**

* 防抖阶段不再自行调用 LLM，改为重构事件后交由框架处理，其他插件（dayflow、记忆召回、postsplitter 等）正常生效。
* 中断重试改为在 `on_llm_response` 钩子中检测中断标记，丢弃被中断的响应后异步重新请求。
* 修复对话历史无法保存的问题（移除了错误的 `astrbot.api.all` 导入）。
* 移除了自行管理对话历史的逻辑，框架会自动处理。

### v1.0.1

**🐛 修复指令放行逻辑**

* 修复收到指令时错误地发起 LLM 请求的 bug。
* 现在收到指令时会正确取消所有防抖和 LLM 任务，清空会话缓冲区，然后放行给框架处理。

### v1.0.0

**🔀 首次发布：用户消息智能合并与中断重试插件上线**

**1. ⏳ 防抖收集**
* 收到用户消息后等待可配置的防抖时间再发起 LLM 请求。
* 防抖期间新消息追加到缓冲区并重置计时器。
* 防抖时间设为 0 可跳过等待，立即发起请求。

**2. ⚡ 中断重试**
* LLM 请求进行中，用户发送新消息时自动中断当前请求。
* 被中断的请求全程无痕：不发送提示、不记录对话历史、不触发其他插件钩子。
* 合并缓冲区消息后重新发起 LLM 请求。

**3. 🔀 消息合并**
* 多条消息按可配置分隔符（默认换行符）合并为一条。
* 自动识别图片并与文本一起传递给 LLM。

**4. 📋 指令放行**
* 以配置前缀开头的消息识别为指令，不参与合并，直接放行。
* 收到指令时立即结束当前防抖会话并刷新缓冲区。

**5. 👥 按用户隔离**
* 每个用户独立的消息缓冲区和 LLM 请求，互不干扰。
* 支持私聊和群聊（群聊默认关闭，可配置开启）。

**6. 🛡️ 安全限制**
* 最大中断重试次数（默认 5 次），避免无限中断循环。
* 达到上限后等待当前请求完成，新消息缓存待后续处理。
