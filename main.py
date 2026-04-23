import asyncio
import json
import time
from typing import Any, Dict, List

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.provider import LLMResponse
from astrbot.api.star import Context, Star, register

_ZOMBIE_TIMEOUT = 60
__version__ = "2.0.5"


@register(
    "astrbot_plugin_premerger",
    "Inoryu7z",
    "用户消息智能合并与中断重试：防抖收集、LLM请求中断重试",
    __version__,
)
class PremergerPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        self.enable = bool(config.get("enable", True))
        self.debounce_time = float(config.get("debounce_time", 0.5))
        if self.debounce_time < 0:
            logger.warning("[Premerger] debounce_time 不能为负数，已设置为 0")
            self.debounce_time = 0
        self.merge_separator = self._parse_separator(
            config.get("merge_separator", "\\n")
        )
        self.enable_private_chat = bool(config.get("enable_private_chat", True))
        self.enable_group_chat = bool(config.get("enable_group_chat", False))
        self.max_retry_count = int(config.get("max_retry_count", 5))
        if self.max_retry_count < 0:
            logger.warning("[Premerger] max_retry_count 不能为负数，已设置为 0")
            self.max_retry_count = 0
        self.command_prefixes = config.get("command_prefixes", ["/"])

        self.sessions: Dict[str, Dict[str, Any]] = {}

        logger.info(
            f"[Premerger] v{__version__} 加载 | "
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
        except Exception as e:
            logger.debug(f"[Premerger] 提取图片 URL 失败: {e}")
        return urls

    def _merge_buffer(self, buffer: List[str]) -> str:
        return self.merge_separator.join(msg for msg in buffer if msg)

    def _reconstruct_event(
        self, event: AstrMessageEvent, text: str, image_urls: List[str]
    ) -> None:
        event.message_str = text
        chain: list = []
        if text:
            chain.append(Plain(text=text))
        for url in image_urls:
            try:
                chain.append(Image(file=url))
            except TypeError:
                chain.append(Image(url=url))
            except Exception as e:
                logger.warning(f"[Premerger] 图片组件添加失败: {url}, 错误: {e}")
        if hasattr(event.message_obj, "message"):
            try:
                event.message_obj.message = chain
            except Exception as e:
                logger.warning(f"[Premerger] 事件消息链更新失败: {e}")

    def _is_session_zombie(self, session: Dict[str, Any]) -> bool:
        if not session.get("llm_in_progress"):
            return False
        t = session.get("llm_start_time", 0)
        return t > 0 and (time.monotonic() - t) > _ZOMBIE_TIMEOUT

    def _cleanup_session(self, uid: str) -> None:
        session = self.sessions.get(uid)
        if session:
            flush_event = session.get("flush_event")
            if flush_event and not flush_event.is_set():
                flush_event.set()
            for task in session.get("background_tasks", []):
                if not task.done():
                    task.cancel()
            dt = session.get("debounce_task")
            if dt and not dt.done():
                dt.cancel()
            self.sessions.pop(uid, None)

    def _reset_session_for_retry(self, uid: str, merged_text: str, image_urls: List[str]) -> None:
        session = self.sessions.get(uid)
        if not session:
            return
        session["buffer"] = [merged_text] if merged_text else []
        session["images"] = list(image_urls) if image_urls else []
        session["llm_in_progress"] = False
        session["llm_start_time"] = 0
        session["pending_text"] = ""
        session["pending_images"] = []
        logger.info(
            f"[Premerger] 已将合并消息放回缓冲区，等待新消息触发重试 - 用户 {uid}"
        )

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
                self._cleanup_session(uid)
                logger.info(f"[Premerger] 用户 {uid} 发送指令，清空会话缓冲区")
            return

        if not text.strip() and not image_urls:
            return

        if uid in self.sessions:
            session = self.sessions[uid]

            if self._is_session_zombie(session):
                logger.warning(f"[Premerger] 用户 {uid} 会话超时，强制清理")
                self._cleanup_session(uid)
                return
            elif session.get("llm_in_progress"):
                retry = session.get("retry_count", 0) + 1

                if retry >= self.max_retry_count:
                    logger.warning(
                        f"[Premerger] 用户 {uid} 达到最大中断次数 "
                        f"{self.max_retry_count}，放行新消息"
                    )
                    self._cleanup_session(uid)
                    return

                if session.get("pending_text") and not session.get("buffer"):
                    session.setdefault("buffer", []).insert(0, session["pending_text"])
                    session["pending_text"] = ""
                if session.get("pending_images") and not session.get("images"):
                    session.setdefault("images", []).extend(session["pending_images"])
                    session["pending_images"] = []

                if text.strip():
                    session["buffer"].append(text.strip())
                if image_urls:
                    session["images"].extend(image_urls)
                session["event"] = event
                session["interrupted"] = True
                session["retry_count"] = retry
                session["llm_generation"] = session.get("llm_generation", 0) + 1
                logger.info(
                    f"[Premerger] LLM 期间收到新消息，标记中断"
                    f"（第 {retry} 次，代际 {session['llm_generation']}）"
                )

                old_dt = session.get("debounce_task")
                if old_dt and not old_dt.done():
                    old_dt.cancel()

                session["debounce_task"] = asyncio.create_task(
                    self._debounce_then_retry(uid)
                )

                event.stop_event()
                return

            if text.strip():
                session["buffer"].append(text.strip())
            if image_urls:
                session["images"].extend(image_urls)
            session["event"] = event

            old_dt = session.get("debounce_task")
            if old_dt and not old_dt.done():
                old_dt.cancel()

            flush_event = session.get("flush_event")
            if flush_event:
                flush_event.clear()

            session["debounce_task"] = asyncio.create_task(
                self._debounce_timer(uid)
            )

            if flush_event:
                await flush_event.wait()

                if uid not in self.sessions:
                    return

                session = self.sessions[uid]
                if session.get("llm_in_progress"):
                    event.stop_event()
                    return

                buffer = session["buffer"]
                all_images = session["images"]
                evt = session["event"]

                merged_text = self._merge_buffer(buffer)
                if not merged_text and not all_images:
                    self.sessions.pop(uid, None)
                    return

                logger.info(
                    f"[Premerger] 防抖结算 - 用户 {uid} | "
                    f"合并消息数: {len(buffer)} | 图片数: {len(all_images)}"
                )

                session["pending_text"] = merged_text
                session["pending_images"] = list(all_images)
                session["buffer"] = []
                session["images"] = []
                session["llm_in_progress"] = True
                session["interrupted"] = False
                session["retry_count"] = 0
                session["llm_start_time"] = time.monotonic()

                self._reconstruct_event(evt, merged_text, all_images)
                return
            else:
                event.stop_event()
                return

        flush_event = asyncio.Event()
        debounce_task = asyncio.create_task(self._debounce_timer(uid))
        self.sessions[uid] = {
            "buffer": [text.strip()] if text.strip() else [],
            "images": image_urls,
            "event": event,
            "flush_event": flush_event,
            "debounce_task": debounce_task,
            "llm_in_progress": False,
            "interrupted": False,
            "retry_count": 0,
            "background_tasks": [],
            "llm_start_time": 0,
            "llm_generation": 0,
            "pending_text": "",
            "pending_images": [],
        }

        await flush_event.wait()

        if uid not in self.sessions:
            return

        session = self.sessions[uid]
        if session.get("llm_in_progress"):
            return

        buffer = session["buffer"]
        all_images = session["images"]
        evt = session["event"]

        merged_text = self._merge_buffer(buffer)
        if not merged_text and not all_images:
            self.sessions.pop(uid, None)
            return

        logger.info(
            f"[Premerger] 防抖结算 - 用户 {uid} | "
            f"合并消息数: {len(buffer)} | 图片数: {len(all_images)}"
        )

        session["pending_text"] = merged_text
        session["pending_images"] = list(all_images)
        session["buffer"] = []
        session["images"] = []
        session["llm_in_progress"] = True
        session["interrupted"] = False
        session["retry_count"] = 0
        session["llm_start_time"] = time.monotonic()

        self._reconstruct_event(evt, merged_text, all_images)
        return

    async def _debounce_timer(self, uid: str) -> None:
        try:
            await asyncio.sleep(self.debounce_time)
            if uid in self.sessions:
                self.sessions[uid]["flush_event"].set()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[Premerger] 防抖定时器异常: {e}")
            if uid in self.sessions:
                try:
                    self.sessions[uid]["flush_event"].set()
                except Exception:
                    self.sessions.pop(uid, None)

    async def _debounce_then_retry(self, uid: str) -> None:
        try:
            await asyncio.sleep(self.debounce_time)
            if uid not in self.sessions:
                return
            session = self.sessions[uid]
            buffer = session["buffer"]
            all_images = session["images"]
            merged_text = self._merge_buffer(buffer)

            if not merged_text and not all_images:
                self.sessions.pop(uid, None)
                return

            logger.info(
                f"[Premerger] 中断重试防抖结算 - 用户 {uid} | "
                f"合并消息数: {len(buffer)} | 图片数: {len(all_images)}"
            )

            session["pending_text"] = merged_text
            session["pending_images"] = list(all_images)
            session["buffer"] = []
            session["images"] = []
            session["llm_in_progress"] = True
            session["llm_start_time"] = time.monotonic()

            generation = session.get("llm_generation", 0)

            task = asyncio.create_task(
                self._direct_llm_call(
                    uid, merged_text, all_images, session["event"], generation
                )
            )
            session.setdefault("background_tasks", []).append(task)
            task.add_done_callback(
                lambda t, u=uid: self._remove_task(u, t)
            )
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[Premerger] 中断重试防抖异常 - 用户 {uid}: {e}")
            self.sessions.pop(uid, None)

    def _remove_task(self, uid: str, task: asyncio.Task) -> None:
        if uid in self.sessions:
            tasks = self.sessions[uid].get("background_tasks", [])
            if task in tasks:
                tasks.remove(task)

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req) -> None:
        if not self.enable:
            return

        uid = event.unified_msg_origin
        if uid in self.sessions:
            session = self.sessions[uid]
            session["llm_in_progress"] = True
            session["llm_start_time"] = time.monotonic()
            logger.debug(f"[Premerger] on_llm_request - 用户 {uid}")

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse) -> None:
        if not self.enable:
            return

        uid = event.unified_msg_origin
        if uid not in self.sessions:
            return

        session = self.sessions[uid]

        if session.get("interrupted"):
            logger.info(f"[Premerger] 丢弃被中断的 LLM 响应 - 用户 {uid}")
            if hasattr(resp, "completion_text"):
                resp.completion_text = ""
            session["interrupted"] = False
            has_active_bg = any(
                not t.done() for t in session.get("background_tasks", [])
            )
            if not has_active_bg:
                session["llm_in_progress"] = False
                session["llm_start_time"] = 0
            event.stop_event()
            return

        session["llm_in_progress"] = False
        session["llm_start_time"] = 0
        session["pending_text"] = ""
        session["pending_images"] = []

        if session.get("buffer") and len(session["buffer"]) > 0:
            logger.info(
                f"[Premerger] 响应正常但缓冲区有残留消息 - 用户 {uid}"
            )
            buffer = session["buffer"]
            all_images = session["images"]
            merged_text = self._merge_buffer(buffer)
            session["pending_text"] = merged_text
            session["pending_images"] = list(all_images)
            session["buffer"] = []
            session["images"] = []
            session["llm_in_progress"] = True
            session["interrupted"] = False
            session["llm_start_time"] = time.monotonic()
            generation = session.get("llm_generation", 0)
            task = asyncio.create_task(
                self._direct_llm_call(uid, merged_text, all_images, event, generation)
            )
            session.setdefault("background_tasks", []).append(task)
            task.add_done_callback(
                lambda t, u=uid: self._remove_task(u, t)
            )
        else:
            self.sessions.pop(uid, None)
            logger.debug(f"[Premerger] LLM 响应正常 - 用户 {uid}")

    @filter.after_message_sent()
    async def after_message_sent(self, event: AstrMessageEvent) -> None:
        if not self.enable:
            return

        uid = event.unified_msg_origin
        if uid not in self.sessions:
            return

        session = self.sessions[uid]

        if not session.get("interrupted") and not session.get("background_tasks"):
            self.sessions.pop(uid, None)
            logger.debug(f"[Premerger] after_message_sent 清理会话 - 用户 {uid}")

    async def _direct_llm_call(
        self,
        uid: str,
        merged_text: str,
        image_urls: List[str],
        original_event: AstrMessageEvent,
        generation: int,
    ) -> None:
        provider = self.context.get_using_provider(uid)
        if not provider:
            logger.error("[Premerger] 无法获取 AI 提供商，将消息放回缓冲区")
            session = self.sessions.get(uid)
            if session and session.get("llm_generation", 0) == generation:
                self._reset_session_for_retry(uid, merged_text, image_urls)
            return

        try:
            system_prompt = ""
            begin_dialogs: list = []
            try:
                persona = await self.context.persona_manager.get_default_persona_v3(
                    uid
                )
                system_prompt = persona.get("prompt", "")
                begin_dialogs = persona.get("_begin_dialogs_processed", [])
            except Exception as e:
                logger.debug(f"[Premerger] 获取 persona 失败: {e}")

            contexts = await self._build_contexts(uid, begin_dialogs)

            logger.info(
                f"[Premerger] 直接 LLM 请求 - 用户 {uid} | "
                f"图片数: {len(image_urls)} | 上下文长度: {len(contexts)} | "
                f"代际: {generation}"
            )

            response = await provider.text_chat(
                prompt=merged_text,
                contexts=contexts,
                image_urls=image_urls,
                func_tool=None,
                system_prompt=system_prompt,
            )

            reply_text = (getattr(response, "completion_text", "") or "").strip()

            session = self.sessions.get(uid)
            if not session or session.get("llm_generation", 0) != generation:
                logger.info(
                    f"[Premerger] 直接 LLM 已被新中断取代，丢弃结果"
                    f" - 用户 {uid}（代际 {generation}）"
                )
                return

            if not reply_text:
                logger.warning(
                    f"[Premerger] 直接 LLM 返回为空，将消息放回缓冲区 - 用户 {uid}"
                )
                self._reset_session_for_retry(uid, merged_text, image_urls)
                return

            await original_event.send(original_event.plain_result(reply_text))

            session = self.sessions.get(uid)
            if not session or session.get("llm_generation", 0) != generation:
                logger.info(
                    f"[Premerger] 发送后代际已变，跳过保存 - 用户 {uid}"
                )
                return

            await self._save_conversation(uid, merged_text, reply_text, image_urls)

            session = self.sessions.get(uid)
            if not session:
                return

            if session.get("llm_generation", 0) != generation:
                logger.info(
                    f"[Premerger] 发送后代际已变，不清理会话 - 用户 {uid}"
                )
                return

            session["pending_text"] = ""
            session["pending_images"] = []

            if session.get("buffer") and len(session["buffer"]) > 0:
                logger.info(
                    f"[Premerger] 直接 LLM 完成但缓冲区有残留 - 用户 {uid}"
                )
                session["debounce_task"] = asyncio.create_task(
                    self._debounce_then_retry(uid)
                )
            else:
                self.sessions.pop(uid, None)
                logger.info(f"[Premerger] 直接 LLM 完成 - 用户 {uid}")

        except asyncio.CancelledError:
            logger.debug(f"[Premerger] 直接 LLM 被取消 - 用户 {uid}")
        except Exception as e:
            logger.error(f"[Premerger] 直接 LLM 失败，将消息放回缓冲区 - 用户 {uid}: {e}")
            session = self.sessions.get(uid)
            if session and session.get("llm_generation", 0) == generation:
                self._reset_session_for_retry(uid, merged_text, image_urls)

    async def _build_contexts(
        self, uid: str, begin_dialogs: list
    ) -> list:
        contexts: list = []

        try:
            if begin_dialogs:
                contexts.extend(begin_dialogs)
        except Exception as e:
            logger.debug(f"[Premerger] 添加 begin_dialogs 失败: {e}")

        try:
            conv_mgr = getattr(self.context, "conversation_manager", None)
            if not conv_mgr:
                logger.debug("[Premerger] conversation_manager 不可用，跳过对话历史读取")
                return contexts
            curr_cid = await conv_mgr.get_curr_conversation_id(uid)

            if curr_cid:
                conversation = await conv_mgr.get_conversation(uid, curr_cid)
                if conversation and hasattr(conversation, "history"):
                    history = conversation.history
                    if isinstance(history, str):
                        try:
                            history = json.loads(history)
                        except Exception as e:
                            logger.warning(f"[Premerger] 对话历史 JSON 解析失败: {e}")
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
            logger.warning(f"[Premerger] 读取对话历史失败: {e}")

        return contexts

    async def _save_conversation(
        self, uid: str, user_text: str, assistant_text: str, image_urls: List[str] = None
    ) -> None:
        try:
            from astrbot.core.agent.message import (
                AssistantMessageSegment,
                ImagePart,
                TextPart,
                UserMessageSegment,
            )

            conv_mgr = getattr(self.context, "conversation_manager", None)
            if not conv_mgr:
                logger.debug("[Premerger] conversation_manager 不可用，跳过对话历史保存")
                return
            curr_cid = await conv_mgr.get_curr_conversation_id(uid)

            if curr_cid:
                user_content: list = []
                if user_text:
                    user_content.append(TextPart(text=user_text))
                if image_urls:
                    for url in image_urls:
                        try:
                            user_content.append(ImagePart(url=url))
                        except Exception:
                            user_content.append(ImagePart(file=url))
                if not user_content:
                    user_content.append(TextPart(text=""))
                user_msg = UserMessageSegment(content=user_content)
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

    async def terminate(self) -> None:
        for uid, session in list(self.sessions.items()):
            dt = session.get("debounce_task")
            if dt and not dt.done():
                dt.cancel()
            for task in session.get("background_tasks", []):
                if not task.done():
                    task.cancel()
        self.sessions.clear()
        logger.info("[Premerger] 插件已卸载，所有会话已清理")
