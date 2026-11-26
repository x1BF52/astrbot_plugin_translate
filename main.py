"""AstrBot multi-engine translation plugin supporting /fy and /翻译 commands."""

from __future__ import annotations

import re
from typing import Any

import aiohttp
import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.star.filter.command import GreedyStr

from .translator import (
    DEFAULT_LLM_PROMPT,
    PROVIDER_PRESETS,
    TranslationError,
    TranslationResult,
    TranslationService,
    get_lang_label,
    parse_command_args,
    redact_secret,
)

_CMD_PREFIX_PATTERN = re.compile(r"\s*\[MSG_ID:[^\]]+\]\s*$")


class TranslatePlugin(Star):
    """AstrBot 翻译插件：支持 /fy 与 /翻译，内置常见免费 API 选项与自动回退。"""

    def __init__(self, context: Context, config: AstrBotConfig | dict | None = None):
        super().__init__(context)
        self.config: dict[str, Any] = config if config is not None else {}
        self._session: aiohttp.ClientSession | None = None
        self._service = TranslationService(self._get_session)

    async def initialize(self) -> None:
        """Initialize shared HTTP client session on plugin load."""
        await self._get_session()

    async def terminate(self) -> None:
        """Close shared HTTP client session when plugin unloads or reloads."""
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """Lazily create and return a reusable aiohttp ClientSession."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(trust_env=True)
        return self._session

    def _get_wake_prefixes(self, event: AstrMessageEvent) -> list[str]:
        """Fetch configured wake prefixes for the current session."""
        try:
            cfg = self.context.get_config(umo=event.unified_msg_origin)
            prefixes = cfg.get("wake_prefix", ["/"]) if isinstance(cfg, dict) else ["/"]
            if isinstance(prefixes, list) and prefixes:
                return [str(p) for p in prefixes if isinstance(p, str)]
        except Exception:
            pass
        return ["/"]

    def _extract_raw_command_body(
        self,
        event: AstrMessageEvent,
        greedy_fallback: str,
    ) -> str:
        """Extract command argument tail while preserving original newlines."""
        plain_parts: list[str] = []
        try:
            for seg in event.get_messages():
                if isinstance(seg, Comp.Plain) and getattr(seg, "text", None):
                    plain_parts.append(seg.text)
        except Exception:
            pass

        raw_msg = "".join(plain_parts) if plain_parts else (event.get_message_str() or "")
        raw_msg = _CMD_PREFIX_PATTERN.sub("", raw_msg).lstrip()

        for prefix in sorted(self._get_wake_prefixes(event), key=len, reverse=True):
            if prefix and raw_msg.startswith(prefix):
                raw_msg = raw_msg[len(prefix) :].lstrip()
                break

        cmd_match = re.match(r"^(?:fy|翻译)(?:$|([\s\S]*))$", raw_msg, flags=re.IGNORECASE)
        if cmd_match:
            tail = cmd_match.group(1) or ""
            if not tail or tail[0].isspace():
                return tail.strip()

        return _CMD_PREFIX_PATTERN.sub("", str(greedy_fallback or "")).strip()

    async def _extract_reply_text(self, event: AstrMessageEvent) -> str:
        """Extract plain text from a quoted Reply message if present."""
        try:
            messages = event.get_messages()
        except Exception:
            return ""

        reply_seg = next((seg for seg in messages if isinstance(seg, Comp.Reply)), None)
        if not reply_seg:
            return ""

        try:
            from astrbot.core.utils.quoted_message_parser import (
                extract_quoted_message_text,
            )

            extracted = await extract_quoted_message_text(event, reply_seg)
            if isinstance(extracted, str) and extracted.strip():
                return _CMD_PREFIX_PATTERN.sub("", extracted).strip()
        except Exception:
            pass

        msg_str = getattr(reply_seg, "message_str", "") or ""
        if msg_str.strip():
            return _CMD_PREFIX_PATTERN.sub("", msg_str).strip()

        chain = getattr(reply_seg, "chain", None) or []
        chain_texts = [
            seg.text
            for seg in chain
            if isinstance(seg, Comp.Plain) and getattr(seg, "text", None)
        ]
        joined = "".join(chain_texts).strip()
        if joined:
            return _CMD_PREFIX_PATTERN.sub("", joined).strip()

        legacy_text = getattr(reply_seg, "text", "") or ""
        if isinstance(legacy_text, str) and legacy_text.strip():
            return _CMD_PREFIX_PATTERN.sub("", legacy_text).strip()

        # Fallback for adapters like OneBot v11 that only populate Reply.id
        reply_id = getattr(reply_seg, "id", None)
        bot = getattr(event, "bot", None)
        if reply_id and bot:
            try:
                raw = None
                api = getattr(bot, "api", None)
                if api and hasattr(api, "call_action"):
                    raw = await api.call_action("get_msg", message_id=reply_id)
                elif hasattr(bot, "get_msg"):
                    raw = await bot.get_msg(message_id=reply_id)

                payload = (
                    raw.get("data")
                    if isinstance(raw, dict) and isinstance(raw.get("data"), dict)
                    else raw
                )
                if isinstance(payload, dict):
                    segments = payload.get("message", [])
                    if isinstance(segments, str):
                        return segments.strip()
                    if isinstance(segments, list):
                        parts = [
                            str((seg.get("data") or {}).get("text") or "")
                            for seg in segments
                            if isinstance(seg, dict)
                            and seg.get("type") in {"text", "plain"}
                        ]
                        joined_raw = "".join(parts).strip()
                        if joined_raw:
                            return joined_raw
                    raw_msg = payload.get("raw_message")
                    if isinstance(raw_msg, str) and raw_msg.strip():
                        return raw_msg.strip()
            except Exception as exc:
                logger.debug("获取引用消息原文失败: %s", exc)

        return ""

    def _has_reply_segment(self, event: AstrMessageEvent) -> bool:
        try:
            return any(isinstance(seg, Comp.Reply) for seg in event.get_messages())
        except Exception:
            return False

    async def _call_astrbot_llm(
        self,
        event: AstrMessageEvent,
        text: str,
        source_label: str,
        target_label: str,
    ) -> str:
        """Translate via AstrBot's active chat LLM provider."""
        system_prompt = DEFAULT_LLM_PROMPT.replace(
            "{target_lang}", target_label
        ).replace("{source_lang}", source_label)

        llm_resp = None
        if hasattr(self.context, "get_current_chat_provider_id") and hasattr(
            self.context, "llm_generate"
        ):
            try:
                provider_id = await self.context.get_current_chat_provider_id(
                    umo=event.unified_msg_origin
                )
            except Exception as exc:
                raise TranslationError("AstrBot 未配置可用的大模型提供商") from exc
            if not provider_id:
                raise TranslationError("AstrBot 未配置可用的大模型提供商")
            llm_resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=text,
                system_prompt=system_prompt,
            )
        else:
            provider = None
            try:
                if hasattr(self.context, "get_using_provider_async"):
                    provider = await self.context.get_using_provider_async(
                        umo=event.unified_msg_origin
                    )
                elif hasattr(self.context, "get_using_provider"):
                    provider = self.context.get_using_provider(
                        umo=event.unified_msg_origin
                    )
            except Exception as exc:
                raise TranslationError("AstrBot 未配置可用的大模型提供商") from exc

            if not provider:
                raise TranslationError("AstrBot 未配置可用的大模型提供商")

            llm_resp = await provider.text_chat(
                prompt=text,
                system_prompt=system_prompt,
            )

        result_text = getattr(llm_resp, "completion_text", "") or ""
        if not result_text and getattr(llm_resp, "result_chain", None):
            result_text = llm_resp.result_chain.get_plain_text()
        result_text = str(result_text).strip()
        if getattr(llm_resp, "role", None) == "err":
            raise TranslationError(result_text or "AstrBot 内置 LLM 调用失败")
        return result_text

    def _build_help_text(self) -> str:
        primary_key = str(self.config.get("api_provider") or "bing_cn")
        preset = PROVIDER_PRESETS.get(primary_key, PROVIDER_PRESETS["bing_cn"])
        default_target = str(self.config.get("default_target_lang") or "auto_zh_en")
        target_desc = (
            "智能中英互译" if default_target == "auto_zh_en" else get_lang_label(default_target)
        )
        return (
            "翻译插件使用说明：\n"
            "• /fy <文本> 或 /翻译 <文本>\n"
            "• 引用任意消息并发送 /fy 或 /翻译（支持加目标语如 /fy en）\n"
            "• 指定目标语：/fy 日语 你好  |  /fy ->ja 你好  |  /fy en->zh Hello\n"
            "• 查看当前配置与支持接口：/fy --status\n"
            f"当前引擎：{preset.name} | 默认目标：{target_desc}"
        )

    def _build_status_text(self) -> str:
        primary_key = str(self.config.get("api_provider") or "bing_cn")
        preset = PROVIDER_PRESETS.get(primary_key, PROVIDER_PRESETS["bing_cn"])
        custom_url = str(self.config.get("custom_api_url") or "").strip()
        if primary_key == "astrbot_llm":
            active_url = "AstrBot 当前内置大模型"
        else:
            active_url = custom_url or preset.default_url or "未配置"
        fallbacks = [
            PROVIDER_PRESETS[k].name
            for k in (self.config.get("fallback_providers") or [])
            if k in PROVIDER_PRESETS and k != primary_key
        ]
        fallback_line = (
            "、".join(fallbacks)
            if self.config.get("enable_auto_fallback", True) and fallbacks
            else "已关闭"
        )
        proxy = "已配置" if str(self.config.get("proxy_url") or "").strip() else "直连"
        return (
            "当前翻译配置状态：\n"
            f"• 主接口：{preset.name}\n"
            f"• 接口地址：{active_url}\n"
            f"• 备用回退：{fallback_line}\n"
            f"• 代理模式：{proxy}\n"
            "可在 AstrBot 管理面板「插件管理 -> 多引擎翻译插件 -> 配置」中切换预设免费 API 或填写自定义接口。"
        )

    def _format_output(self, original: str, res: TranslationResult) -> str:
        adv = self.config.get("advanced") or {}
        show_engine = adv.get("show_engine_name", True)
        show_lang = adv.get("show_detected_lang", True)
        show_original = adv.get("show_original", False)

        meta_parts: list[str] = []
        if show_lang:
            src_code = res.detected_lang or res.source_lang
            meta_parts.append(f"{get_lang_label(src_code)} → {get_lang_label(res.target_lang)}")
        if show_engine:
            suffix = "（备用回退）" if res.fallback_used else ""
            meta_parts.append(f"{res.provider_name}{suffix}")

        lines: list[str] = []
        if meta_parts:
            lines.append(f"[{' | '.join(meta_parts)}]")
        if show_original:
            lines.append(f"原文：{original}")
            lines.append(f"译文：{res.translated_text}")
        else:
            lines.append(res.translated_text)
        return "\n".join(lines)

    @filter.command("fy", alias={"翻译"})
    async def translate_command(self, event: AstrMessageEvent, text: GreedyStr):
        """翻译文本或引用的消息，支持 /fy 与 /翻译。"""
        has_reply = self._has_reply_segment(event)
        raw_body = self._extract_raw_command_body(event, str(text or ""))
        parsed = parse_command_args(raw_body, has_reply=has_reply)

        if parsed.action == "help":
            yield event.plain_result(self._build_help_text())
            return
        if parsed.action == "status":
            yield event.plain_result(self._build_status_text())
            return

        content = parsed.text
        if not content and has_reply:
            content = await self._extract_reply_text(event)
            if not content:
                yield event.plain_result("引用的消息中没有检测到可翻译的文本内容。")
                return

        if not content:
            yield event.plain_result(self._build_help_text())
            return

        adv = self.config.get("advanced") or {}
        try:
            max_len = max(50, int(adv.get("max_text_length", 3000)))
        except (TypeError, ValueError):
            max_len = 3000

        if len(content) > max_len:
            yield event.plain_result(
                f"待翻译文本长度 ({len(content)} 字符) 超过上限 ({max_len} 字符)，请分段后重试。"
            )
            return

        async def llm_cb(txt: str, src_lbl: str, tgt_lbl: str) -> str:
            return await self._call_astrbot_llm(event, txt, src_lbl, tgt_lbl)

        try:
            result = await self._service.translate(
                text=content,
                config=self.config,
                source_lang=parsed.source_lang,
                target_lang=parsed.target_lang,
                llm_callback=llm_cb,
            )
        except TranslationError as exc:
            safe_msg = redact_secret(str(exc), str(self.config.get("api_key") or "").strip())
            logger.warning("翻译失败: %s", safe_msg)
            yield event.plain_result(f"翻译失败：{safe_msg}")
            return
        except Exception as exc:
            safe_msg = redact_secret(str(exc), str(self.config.get("api_key") or "").strip())
            logger.error("翻译插件异常: %s", safe_msg, exc_info=True)
            yield event.plain_result(f"翻译时发生内部错误：{safe_msg}")
            return

        yield event.plain_result(self._format_output(content, result))
