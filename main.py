import asyncio
import traceback
from typing import Any, Dict, List

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image
from astrbot.api.star import Context, Star, register


@register(
    "astrbot_plugin_premerger",
    "Inoryu7z",
    "用户消息智能合并与中断重试：防抖收集、LLM请求中断重试",
    "1.0.1",
)
class PremergerPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        self.enable = bool(config.get("enable", True))
        self.debounce_time = float(config.get("debounce_time", 0.5))
        self.merge_separator = self._parse_separator(
            config.get("merge_separator", "\\n")
        )
        self.enable_private_chat = bool(config.get("enable_private_chat", True))
        self.enable_group_chat = bool(config.get("enable_group_chat", False))
        self.max_retry_count = int(config.get("max_retry_count", 5))
        self.command_prefixes = config.get("command_prefixes", ["/"])

        self.sessions: Dict[str, Dict[str, Any]] = {}

        logger.info(
            f"[Premerger] v1.0.1 加载 | "
            f"防抖: {self.debounce_time}s | "
            f"私聊: {self.enable_private_chat} | "
            f"群聊: {self.enable_group_chat} | "
            f"最大中断: {self.max_retry_count}"
        )

    @staticmethod
    def _parse_separator(raw: str) -> str:
        s = str(raw)
        s = s.replace("\\n", "\n").replace("\\t", "\t")
        return s

    def _is_group_event(self, event: AstrMessageEvent) -> bool:
        return bool(getattr(event.message_obj, "group_id", ""))

    @filter.event_message_type(filter.EventMessageType.ALL, priority=1)
    async def handle_message(self, event: AstrMessageEvent):
        if not self.enable:
            return

        is_group = self._is_group_event(event)
        if is_group and not self.enable_group_chat:
            return
        if not is_group and not self.enable_private_chat:
            return

        uid = event.unified_msg_origin
        text = event.message_str or ""
        image_urls = self._extract_image_urls(event)

        if self._is_command(text):
            if uid in self.sessions:
                session = self.sessions[uid]
                dt = session.get("debounce_task")
                if dt and not dt.done():
                    dt.cancel()
                lt = session.get("llm_task")
                if lt and not lt.done():
                    lt.cancel()
                self.sessions.pop(uid, None)
                logger.info(f"[Premerger] 用户 {uid} 发送指令，清空会话缓冲区")
            return

        if not text.strip() and not image_urls:
            return

        if uid in self.sessions:
            session = self.sessions[uid]

            new_retry_count = session.get("retry_count", 0) + 1

            lt = session.get("llm_task")
            if lt and not lt.done():
                if new_retry_count >= self.max_retry_count:
                    logger.warning(
                        f"[Premerger] 用户 {uid} 达到最大中断次数 "
                        f"{self.max_retry_count}，放行消息由框架处理"
                    )
                    return
                lt.cancel()
                logger.info(
                    f"[Premerger] 中断用户 {uid} 的 LLM 请求"
                    f"（第 {new_retry_count} 次中断）"
                )

            dt = session.get("debounce_task")
            if dt and not dt.done():
                dt.cancel()

            session["retry_count"] = new_retry_count

            if text.strip():
                session["buffer"].append(text.strip())
            if image_urls:
                session["images"].extend(image_urls)
            session["event"] = event

            if self.debounce_time <= 0:
                session["debounce_task"] = None
                asyncio.create_task(self._start_llm_request(uid))
            else:
                session["debounce_task"] = asyncio.create_task(
                    self._debounce_timer(uid)
                )
        else:
            self.sessions[uid] = {
                "buffer": [text.strip()] if text.strip() else [],
                "images": image_urls,
                "event": event,
                "llm_task": None,
                "debounce_task": (
                    asyncio.create_task(self._debounce_timer(uid))
                    if self.debounce_time > 0
                    else None
                ),
                "retry_count": 0,
            }
            if self.debounce_time <= 0:
                asyncio.create_task(self._start_llm_request(uid))

        event.stop_event()

    async def _debounce_timer(self, uid: str):
        try:
            await asyncio.sleep(self.debounce_time)
            await self._start_llm_request(uid)
        except asyncio.CancelledError:
            pass

    async def _start_llm_request(self, uid: str):
        session = self.sessions.get(uid)
        if not session:
            return

        buffer = session.get("buffer", [])
        images = list(session.get("images", []))
        event = session.get("event")

        if not buffer and not images:
            self.sessions.pop(uid, None)
            return

        merged_text = self.merge_separator.join(msg for msg in buffer if msg)

        if not merged_text and not images:
            self.sessions.pop(uid, None)
            return

        if not event:
            self.sessions.pop(uid, None)
            return

        session["llm_task"] = asyncio.create_task(
            self._call_llm(uid, merged_text, images, event)
        )

    async def _call_llm(
        self,
        uid: str,
        merged_text: str,
        image_urls: List[str],
        event: AstrMessageEvent,
    ):
        try:
            provider = self.context.get_using_provider(uid)
            if not provider:
                logger.error("[Premerger] 无法获取 AI 提供商")
                self.sessions.pop(uid, None)
                return

            system_prompt = ""
            begin_dialogs: list = []
            try:
                persona = await self.context.persona_manager.get_default_persona_v3(
                    uid
                )
                system_prompt = persona.get("prompt", "")
                begin_dialogs = persona.get("_begin_dialogs_processed", [])
            except Exception:
                pass

            contexts = await self._build_contexts(uid, merged_text, begin_dialogs)

            logger.info(
                f"[Premerger] 用户 {uid} 开始 LLM 请求 | "
                f"合并消息数: {len(merged_text.split(self.merge_separator))} | "
                f"图片数: {len(image_urls)} | "
                f"上下文长度: {len(contexts)}"
            )

            response = await provider.text_chat(
                prompt=merged_text,
                contexts=contexts,
                image_urls=image_urls,
                func_tool=None,
                system_prompt=system_prompt,
            )

            reply_text = (getattr(response, "completion_text", "") or "").strip()

            if not reply_text:
                logger.warning(f"[Premerger] 用户 {uid} 的 LLM 返回为空")
                self.sessions.pop(uid, None)
                return

            await event.send(event.plain_result(reply_text))

            await self._save_conversation(uid, merged_text, reply_text)

            self.sessions.pop(uid, None)
            logger.info(f"[Premerger] 用户 {uid} 的 LLM 请求完成")

        except asyncio.CancelledError:
            logger.info(f"[Premerger] 用户 {uid} 的 LLM 请求被取消（无痕）")
        except Exception as e:
            logger.error(f"[Premerger] LLM 调用失败: {e}\n{traceback.format_exc()}")
            self.sessions.pop(uid, None)

    async def _build_contexts(
        self, uid: str, current_text: str, begin_dialogs: list
    ) -> list:
        contexts: list = []

        try:
            if begin_dialogs:
                contexts.extend(begin_dialogs)
        except Exception:
            pass

        try:
            conv_mgr = self.context.conversation_manager
            curr_cid = await conv_mgr.get_curr_conversation_id(uid)

            if curr_cid:
                conversation = await conv_mgr.get_conversation(uid, curr_cid)
                if conversation and hasattr(conversation, "history"):
                    history = conversation.history
                    if isinstance(history, str):
                        import json

                        try:
                            history = json.loads(history)
                        except Exception:
                            history = []
                    if isinstance(history, list):
                        for msg in history:
                            if isinstance(msg, dict):
                                role = msg.get("role", "")
                                content = msg.get("content", "")
                                if role and content:
                                    contexts.append(
                                        {"role": role, "content": content}
                                    )
        except Exception as e:
            logger.debug(f"[Premerger] 读取对话历史失败: {e}")

        contexts.append({"role": "user", "content": current_text})
        return contexts

    async def _save_conversation(
        self, uid: str, user_text: str, assistant_text: str
    ):
        try:
            from astrbot.api.all import (
                AssistantMessageSegment,
                TextPart,
                UserMessageSegment,
            )

            conv_mgr = self.context.conversation_manager
            curr_cid = await conv_mgr.get_curr_conversation_id(uid)

            if curr_cid:
                user_msg = UserMessageSegment(content=[TextPart(text=user_text)])
                assistant_msg = AssistantMessageSegment(
                    content=[TextPart(text=assistant_text)]
                )
                await conv_mgr.add_message_pair(
                    cid=curr_cid,
                    user_message=user_msg,
                    assistant_message=assistant_msg,
                )
                logger.debug(f"[Premerger] 用户 {uid} 对话历史已保存")
        except Exception as e:
            logger.warning(f"[Premerger] 保存对话历史失败: {e}")

    def _is_command(self, text: str) -> bool:
        text = text.strip()
        if not text:
            return False
        for prefix in self.command_prefixes:
            if isinstance(prefix, str) and prefix and text.startswith(prefix):
                return True
        return False

    def _extract_image_urls(self, event: AstrMessageEvent) -> List[str]:
        urls: List[str] = []
        try:
            if hasattr(event, "message_obj") and hasattr(event.message_obj, "message"):
                for comp in event.message_obj.message:
                    if isinstance(comp, Image):
                        url = getattr(comp, "url", None) or getattr(
                            comp, "file", None
                        )
                        if url:
                            urls.append(url)
        except Exception:
            pass
        return urls

    async def terminate(self):
        for uid, session in list(self.sessions.items()):
            lt = session.get("llm_task")
            if lt and not lt.done():
                lt.cancel()
            dt = session.get("debounce_task")
            if dt and not dt.done():
                dt.cancel()
        self.sessions.clear()
        logger.info("[Premerger] 插件已卸载，所有会话已清理")
