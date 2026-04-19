import asyncio
import traceback
from typing import Any, Dict, List

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.provider import LLMResponse
from astrbot.api.star import Context, Star, register


@register(
    "astrbot_plugin_premerger",
    "Inoryu7z",
    "用户消息智能合并与中断重试：防抖收集、LLM请求中断重试",
    "1.1.0",
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
            f"[Premerger] v1.1.0 加载 | "
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

    def _reconstruct_event(
        self, event: AstrMessageEvent, text: str, image_urls: List[str]
    ):
        event.message_str = text
        chain: list = []
        if text:
            chain.append(Plain(text=text))
        for url in image_urls:
            try:
                chain.append(Image(file=url))
            except TypeError:
                chain.append(Image(url=url))
            except Exception:
                pass
        if hasattr(event.message_obj, "message"):
            try:
                event.message_obj.message = chain
            except Exception:
                pass

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
                self.sessions.pop(uid, None)
                logger.info(f"[Premerger] 用户 {uid} 发送指令，清空会话缓冲区")
            return

        if not text.strip() and not image_urls:
            return

        if uid in self.sessions:
            session = self.sessions[uid]

            if text.strip():
                session["buffer"].append(text.strip())
            if image_urls:
                session["images"].extend(image_urls)
            session["event"] = event

            dt = session.get("debounce_task")
            if dt and not dt.done():
                dt.cancel()

            if session.get("llm_in_progress"):
                session["interrupted"] = True
                retry = session.get("retry_count", 0) + 1
                session["retry_count"] = retry
                logger.info(
                    f"[Premerger] LLM 请求期间收到新消息，标记中断"
                    f"（第 {retry} 次中断）"
                )
                if retry >= self.max_retry_count:
                    logger.warning(
                        f"[Premerger] 用户 {uid} 达到最大中断次数 "
                        f"{self.max_retry_count}，后续消息不再中断"
                    )
                event.stop_event()
                return

            session["debounce_task"] = asyncio.create_task(
                self._debounce_timer(uid)
            )
            event.stop_event()
        else:
            flush_event = asyncio.Event()
            debounce_task = asyncio.create_task(
                self._debounce_timer(uid)
            )
            self.sessions[uid] = {
                "buffer": [text.strip()] if text.strip() else [],
                "images": image_urls,
                "event": event,
                "flush_event": flush_event,
                "debounce_task": debounce_task,
                "llm_in_progress": False,
                "interrupted": False,
                "retry_count": 0,
            }

            await flush_event.wait()

            if uid not in self.sessions:
                return

            session_data = self.sessions.pop(uid)
            buffer = session_data["buffer"]
            all_images = session_data["images"]
            evt = session_data["event"]

            merged_text = self.merge_separator.join(msg for msg in buffer if msg)
            if not merged_text and not all_images:
                return

            logger.info(
                f"[Premerger] 防抖结算 - 用户 {uid} | "
                f"合并消息数: {len(buffer)} | 图片数: {len(all_images)}"
            )

            self._reconstruct_event(evt, merged_text, all_images)
            return

    async def _debounce_timer(self, uid: str):
        try:
            await asyncio.sleep(self.debounce_time)
            if uid in self.sessions:
                self.sessions[uid]["flush_event"].set()
        except asyncio.CancelledError:
            pass

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req):
        if not self.enable:
            return

        uid = event.unified_msg_origin
        if uid in self.sessions:
            session = self.sessions[uid]
            session["llm_in_progress"] = True
            session["interrupted"] = False
            logger.debug(f"[Premerger] on_llm_request - 用户 {uid} LLM 请求开始")

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        if not self.enable:
            return

        uid = event.unified_msg_origin
        if uid not in self.sessions:
            return

        session = self.sessions[uid]

        if not session.get("interrupted"):
            session["llm_in_progress"] = False
            self.sessions.pop(uid, None)
            logger.debug(f"[Premerger] LLM 响应正常 - 用户 {uid}")
            return

        logger.info(
            f"[Premerger] LLM 响应被丢弃（用户 {uid} 发送了新消息）"
        )

        session["llm_in_progress"] = False
        session["interrupted"] = False

        if session["retry_count"] >= self.max_retry_count:
            logger.warning(
                f"[Premerger] 用户 {uid} 已达最大中断次数，放行当前响应"
            )
            self.sessions.pop(uid, None)
            return

        buffer = session["buffer"]
        all_images = session["images"]
        merged_text = self.merge_separator.join(msg for msg in buffer if msg)

        if not merged_text and not all_images:
            self.sessions.pop(uid, None)
            event.stop_event()
            return

        logger.info(
            f"[Premerger] 中断重试 - 用户 {uid} | "
            f"合并消息数: {len(buffer)} | 图片数: {len(all_images)}"
        )

        event.stop_event()

        asyncio.create_task(
            self._retry_llm_request(uid, merged_text, all_images, event)
        )

    async def _retry_llm_request(
        self,
        uid: str,
        merged_text: str,
        image_urls: List[str],
        original_event: AstrMessageEvent,
    ):
        try:
            provider_id = await self.context.get_current_chat_provider_id(uid)
            if not provider_id:
                logger.error("[Premerger] 无法获取 AI 提供商 ID")
                self.sessions.pop(uid, None)
                return

            llm_resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=merged_text,
            )

            reply_text = (getattr(llm_resp, "completion_text", "") or "").strip()

            if not reply_text:
                logger.warning(f"[Premerger] 重试 LLM 返回为空 - 用户 {uid}")
                self.sessions.pop(uid, None)
                return

            await original_event.send(original_event.plain_result(reply_text))

            self.sessions.pop(uid, None)
            logger.info(f"[Premerger] 中断重试完成 - 用户 {uid}")

        except Exception as e:
            logger.error(
                f"[Premerger] 中断重试失败: {e}\n{traceback.format_exc()}"
            )
            self.sessions.pop(uid, None)

    async def terminate(self):
        for uid, session in list(self.sessions.items()):
            dt = session.get("debounce_task")
            if dt and not dt.done():
                dt.cancel()
        self.sessions.clear()
        logger.info("[Premerger] 插件已卸载，所有会话已清理")
