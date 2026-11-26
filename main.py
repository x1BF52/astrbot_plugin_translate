"""AstrBot multi-engine translation plugin supporting /fy and /翻译 commands."""

from __future__ import annotations

import asyncio
import inspect
import os
import re
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any

import aiohttp
import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.star.filter.command import GreedyStr

from .translator import (
    LANG_LABELS,
    PROVIDER_PRESETS,
    SpeechResult,
    TranslationError,
    TranslationResult,
    TranslationService,
    build_llm_system_prompt,
    clean_llm_translation,
    format_dict_entries_markdown,
    get_lang_label,
    is_word_or_phrase,
    parse_command_args,
    redact_config_secrets,
    select_speech_target,
)

_CMD_PREFIX_PATTERN = re.compile(r"\s*\[MSG_ID:[^\]]+\]\s*$")
_QQ_OFFICIAL_IMAGE_URL_PATTERN = re.compile(
    r"\[附件\d+\][^\n]*?类型:图片[^\n]*?URL:(https?://\S+)"
)


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
        payload = await self._fetch_onebot_reply_payload(event, reply_id)
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

        return ""

    async def _fetch_onebot_reply_payload(
        self,
        event: AstrMessageEvent,
        reply_id: Any,
    ) -> dict[str, Any] | None:
        bot = getattr(event, "bot", None)
        if not reply_id or not bot:
            return None
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
            return payload if isinstance(payload, dict) else None
        except Exception as exc:
            logger.debug("获取引用消息详情失败: %s", exc)
            return None

    async def _resolve_image_component(self, seg: Any) -> str | None:
        """Convert an Image message component into a URL/base64/path string for multimodal LLM."""
        if hasattr(seg, "convert_to_base64"):
            try:
                b64 = await seg.convert_to_base64()
                if isinstance(b64, str) and b64.strip():
                    cleaned = b64.strip()
                    if cleaned.startswith(("data:", "base64://")):
                        return cleaned
                    return f"base64://{cleaned}"
            except Exception as exc:
                logger.debug("图片转 base64 失败: %s", exc)
        if hasattr(seg, "convert_to_file_path"):
            try:
                path = await seg.convert_to_file_path()
                if isinstance(path, str) and path.strip():
                    return path.strip()
            except Exception as exc:
                logger.debug("图片转本地路径失败: %s", exc)
        for attr in ("url", "file", "path"):
            val = getattr(seg, attr, None)
            if isinstance(val, str) and val.strip():
                return val.strip()
        return None

    async def _extract_message_images(self, event: AstrMessageEvent) -> list[str]:
        """Extract image references directly attached to the current message."""
        image_cls = getattr(Comp, "Image", None)
        if image_cls is None:
            return []
        try:
            messages = event.get_messages()
        except Exception:
            return []

        urls: list[str] = []
        for seg in messages:
            if isinstance(seg, image_cls):
                ref = await self._resolve_image_component(seg)
                if ref and ref not in urls:
                    urls.append(ref)
        return urls

    async def _extract_reply_images(
        self,
        event: AstrMessageEvent,
        *,
        allow_remote: bool = True,
        fallback_reply_text: str = "",
    ) -> list[str]:
        """Extract image references from a quoted Reply message."""
        try:
            messages = event.get_messages()
        except Exception:
            return []

        reply_seg = next((seg for seg in messages if isinstance(seg, Comp.Reply)), None)
        if not reply_seg:
            return []

        urls: list[str] = []

        def _add_url(val: str | None) -> None:
            if isinstance(val, str):
                s = val.strip()
                if s and s not in urls:
                    urls.append(s)

        if allow_remote:
            try:
                from astrbot.core.utils.quoted_message_parser import (
                    extract_quoted_message_images,
                )

                extracted = await extract_quoted_message_images(event, reply_seg)
                for ref in extracted or []:
                    _add_url(ref)
            except Exception:
                pass

        image_cls = getattr(Comp, "Image", None)
        if image_cls is not None:
            for seg in getattr(reply_seg, "chain", None) or []:
                if isinstance(seg, image_cls):
                    _add_url(await self._resolve_image_component(seg))

        if not urls and allow_remote:
            payload = await self._fetch_onebot_reply_payload(
                event, getattr(reply_seg, "id", None)
            )
            if isinstance(payload, dict):
                segments = payload.get("message", [])
                if isinstance(segments, list):
                    for seg in segments:
                        if isinstance(seg, dict) and seg.get("type") == "image":
                            data = seg.get("data") or {}
                            _add_url(data.get("url") or data.get("file"))

        if not urls and fallback_reply_text:
            for match in _QQ_OFFICIAL_IMAGE_URL_PATTERN.findall(fallback_reply_text):
                _add_url(match)

        return urls

    def _has_reply_segment(self, event: AstrMessageEvent) -> bool:
        try:
            return any(isinstance(seg, Comp.Reply) for seg in event.get_messages())
        except Exception:
            return False

    def _use_markdown(self) -> bool:
        adv = self.config.get("advanced") or {}
        return bool(adv.get("output_markdown", False))

    def _make_result(self, event: AstrMessageEvent, text: str):
        res = event.plain_result(text)
        if hasattr(res, "use_markdown"):
            res.use_markdown(self._use_markdown())
        return res

    async def _invoke_astrbot_llm(
        self,
        event: AstrMessageEvent,
        *,
        prompt: str,
        system_prompt: str,
        image_urls: list[str] | None = None,
        provider_id_override: str = "",
    ) -> str:
        """Invoke AstrBot's configured LLM provider (text or multimodal)."""
        llm_resp = None
        if hasattr(self.context, "get_current_chat_provider_id") and hasattr(
            self.context, "llm_generate"
        ):
            provider_id = provider_id_override.strip()
            if not provider_id:
                try:
                    provider_id = await self.context.get_current_chat_provider_id(
                        umo=event.unified_msg_origin
                    )
                except Exception as exc:
                    raise TranslationError("AstrBot 未配置可用的大模型提供商") from exc
            if not provider_id:
                raise TranslationError("AstrBot 未配置可用的大模型提供商")
            llm_kwargs: dict[str, Any] = {
                "chat_provider_id": provider_id,
                "prompt": prompt,
                "system_prompt": system_prompt,
            }
            if image_urls:
                llm_kwargs["image_urls"] = image_urls
            llm_resp = await self.context.llm_generate(**llm_kwargs)
        else:
            provider = None
            try:
                if provider_id_override.strip() and hasattr(
                    self.context, "get_provider_by_id"
                ):
                    prov_res = self.context.get_provider_by_id(
                        provider_id_override.strip()
                    )
                    provider = (
                        await prov_res
                        if inspect.isawaitable(prov_res)
                        else prov_res
                    )
                elif hasattr(self.context, "get_using_provider_async"):
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

            chat_kwargs: dict[str, Any] = {
                "prompt": prompt,
                "system_prompt": system_prompt,
            }
            if image_urls:
                chat_kwargs["image_urls"] = image_urls
            llm_resp = await provider.text_chat(**chat_kwargs)

        result_text = getattr(llm_resp, "completion_text", "") or ""
        if not result_text and getattr(llm_resp, "result_chain", None):
            result_text = llm_resp.result_chain.get_plain_text()
        result_text = str(result_text).strip()
        if getattr(llm_resp, "role", None) == "err":
            raise TranslationError(result_text or "AstrBot 内置 LLM 调用失败")
        return clean_llm_translation(result_text)

    async def _call_astrbot_llm(
        self,
        event: AstrMessageEvent,
        text: str,
        source_label: str,
        target_label: str,
        dict_mode: bool = False,
    ) -> str:
        """Translate text via AstrBot's active chat LLM provider."""
        system_prompt = build_llm_system_prompt(
            "",
            source_label,
            target_label,
            dict_mode=dict_mode,
            use_markdown=self._use_markdown(),
        )
        return await self._invoke_astrbot_llm(
            event,
            prompt=text,
            system_prompt=system_prompt,
        )

    async def _call_astrbot_image_llm(
        self,
        event: AstrMessageEvent,
        prompt: str,
        system_prompt: str,
        image_urls: list[str],
        provider_id_override: str = "",
    ) -> str:
        """Translate image(s) via AstrBot's multimodal LLM provider."""
        return await self._invoke_astrbot_llm(
            event,
            prompt=prompt,
            system_prompt=system_prompt,
            image_urls=image_urls,
            provider_id_override=provider_id_override,
        )

    async def _call_astrbot_tts(self, event: AstrMessageEvent, text: str) -> str:
        """Synthesize speech using AstrBot's configured TTS provider."""
        tts_provider = None
        if hasattr(self.context, "get_using_tts_provider_async"):
            tts_provider = await self.context.get_using_tts_provider_async(
                umo=event.unified_msg_origin
            )
        elif hasattr(self.context, "get_using_tts_provider"):
            tts_provider = self.context.get_using_tts_provider(
                umo=event.unified_msg_origin
            )
        if not tts_provider:
            raise TranslationError("AstrBot 未配置可用的 TTS 提供商")
        return str(await tts_provider.get_audio(text))

    async def _build_voice_result(
        self,
        event: AstrMessageEvent,
        speech: SpeechResult,
        speak_text: str,
    ):
        """Persist synthesized speech to a tracked temp file and build a Record/File result."""
        if not hasattr(event, "chain_result"):
            return None

        is_wav = speech.mime_type == "audio/wav" or (
            len(speech.audio_bytes) >= 12
            and speech.audio_bytes[:4] == b"RIFF"
            and speech.audio_bytes[8:12] == b"WAVE"
        )
        if is_wav:
            ext = ".wav"
        elif speech.mime_type == "audio/ogg":
            ext = ".ogg"
        else:
            ext = ".mp3"

        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

            temp_dir = get_astrbot_temp_path()
        except Exception:
            temp_dir = tempfile.gettempdir()

        os.makedirs(temp_dir, exist_ok=True)
        temp_path = str(Path(temp_dir) / f"translate_tts_{uuid.uuid4().hex}{ext}")
        await asyncio.to_thread(Path(temp_path).write_bytes, speech.audio_bytes)
        if hasattr(event, "track_temporary_local_file"):
            event.track_temporary_local_file(temp_path)

        has_ffmpeg = bool(shutil.which("ffmpeg"))
        record_cls = getattr(Comp, "Record", None)
        if (is_wav or has_ffmpeg) and record_cls is not None:
            if hasattr(record_cls, "fromFileSystem"):
                comp = record_cls.fromFileSystem(temp_path, text=speak_text)
            else:
                comp = record_cls(file=temp_path)
            return event.chain_result([comp])

        file_cls = getattr(Comp, "File", None)
        if file_cls is not None:
            filename = f"pronunciation_{speech.lang}{ext}"
            return event.chain_result([file_cls(name=filename, file=temp_path)])

        if record_cls is not None:
            comp = (
                record_cls.fromFileSystem(temp_path, text=speak_text)
                if hasattr(record_cls, "fromFileSystem")
                else record_cls(file=temp_path)
            )
            return event.chain_result([comp])

        return None

    def _build_help_text(self) -> str:
        default_target = str(self.config.get("default_target_lang") or "auto_zh_en")
        target_desc = (
            "智能中英互译" if default_target == "auto_zh_en" else get_lang_label(default_target)
        )
        if self._use_markdown():
            return (
                "**翻译插件使用说明**\n"
                "- `/fy <文本>` 或 `/翻译 <文本>`（单词/短语自动附带词性与多义）\n"
                "- 发送图片或引用图片并发送 `/fy`（可加目标语如 `/fy 日语`）自动识图翻译\n"
                "- 附带发音：`/fy -v <文本>` 或 `/fy 日语 -v 你好`（也支持 `--voice` / `--tts`）\n"
                "- 引用任意消息并发送 `/fy` 或 `/翻译`（支持加目标语如 `/fy en`）\n"
                "- 指定目标语：`/fy 日语 你好` | `/fy ->ja 你好` | `/fy en->zh Hello`\n"
                "- 查看支持语种：`/fy --langs` | 查看配置状态：`/fy --status`\n\n"
                f"**默认目标**：{target_desc}"
            )
        return (
            "翻译插件使用说明：\n"
            "• /fy <文本> 或 /翻译 <文本>（单词/短语自动附带词性与多义）\n"
            "• 发送或引用图片并输入 /fy（可加目标语如 /fy 日语）自动识图翻译\n"
            "• 附带发音：/fy -v <文本> 或 /fy 日语 -v 你好（支持 --voice / --tts）\n"
            "• 引用任意消息并发送 /fy 或 /翻译（支持加目标语如 /fy en）\n"
            "• 指定目标语：/fy 日语 你好  |  /fy ->ja 你好  |  /fy en->zh Hello\n"
            "• 查看支持语种：/fy --langs  |  查看配置状态：/fy --status\n"
            f"默认目标：{target_desc}"
        )

    def _build_langs_text(self) -> str:
        if self._use_markdown():
            pairs = [
                f"{label} (`{code}`)"
                for code, label in LANG_LABELS.items()
                if code != "auto"
            ]
            return (
                "**支持的目标语种代码与名称**\n"
                + "、".join(pairs)
                + "\n\n用法示例：`/fy 日语 你好` | `/fy ->zh-TW 你好` | `/fy en->fr Hello`"
            )
        pairs = [
            f"{label} ({code})"
            for code, label in LANG_LABELS.items()
            if code != "auto"
        ]
        return (
            "支持的目标语种代码与名称：\n"
            + "、".join(pairs)
            + "\n用法示例：/fy 日语 你好  |  /fy ->zh-TW 你好  |  /fy en->fr Hello"
        )

    def _build_status_text(self) -> str:
        primary_key = str(self.config.get("api_provider") or "bing_cn")
        preset = PROVIDER_PRESETS.get(primary_key, PROVIDER_PRESETS["bing_cn"])
        custom_url = str(self.config.get("custom_api_url") or "").strip()
        if primary_key == "astrbot_llm":
            active_url = "AstrBot 当前内置大模型"
        else:
            active_url = custom_url or preset.default_url or "未配置"
        default_target = str(self.config.get("default_target_lang") or "auto_zh_en")
        target_desc = (
            "智能中英互译" if default_target == "auto_zh_en" else get_lang_label(default_target)
        )
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
        output_fmt = "Markdown" if self._use_markdown() else "纯文本"
        proxy = "已配置" if str(self.config.get("proxy_url") or "").strip() else "直连"

        img_cfg = self.config.get("image_translation") or {}
        img_enabled = bool(img_cfg.get("enable_image_translation", True))
        img_source = str(img_cfg.get("image_llm_source") or "auto")
        img_labels = {
            "auto": "自动选择",
            "astrbot_llm": "AstrBot 内置大模型",
            "openai_compat": "OpenAI 兼容接口",
        }
        img_status = (
            f"开启（{img_labels.get(img_source, img_source)}）" if img_enabled else "已关闭"
        )

        voice_cfg = self.config.get("voice_settings") or {}
        voice_enabled = bool(voice_cfg.get("enable_voice", True))
        tts_prov = str(voice_cfg.get("tts_provider") or "auto")
        tts_labels = {
            "auto": "自动选择",
            "bing_tts": "Bing 微软语音",
            "youdao_tts": "有道语音",
            "google_tts": "Google 语音",
            "astrbot_tts": "AstrBot 内置 TTS",
        }
        voice_status = (
            f"开启（{tts_labels.get(tts_prov, tts_prov)}）" if voice_enabled else "已关闭"
        )

        if self._use_markdown():
            return (
                "**当前翻译配置状态**\n"
                f"- **主接口**：{preset.name}\n"
                f"- **接口地址**：`{active_url}`\n"
                f"- **默认目标**：{target_desc}\n"
                f"- **图片翻译**：{img_status}\n"
                f"- **语音发音**：{voice_status}\n"
                f"- **输出格式**：{output_fmt}\n"
                f"- **备用回退**：{fallback_line}\n"
                f"- **代理模式**：{proxy}\n\n"
                "可在 AstrBot 管理面板「插件管理 -> 多引擎翻译插件 -> 配置」中修改。"
            )
        return (
            "当前翻译配置状态：\n"
            f"• 主接口：{preset.name}\n"
            f"• 接口地址：{active_url}\n"
            f"• 默认目标：{target_desc}\n"
            f"• 图片翻译：{img_status}\n"
            f"• 语音发音：{voice_status}\n"
            f"• 输出格式：{output_fmt}\n"
            f"• 备用回退：{fallback_line}\n"
            f"• 代理模式：{proxy}\n"
            "可在 AstrBot 管理面板「插件管理 -> 多引擎翻译插件 -> 配置」中切换预设免费 API 或填写自定义接口。"
        )

    def _format_output(self, original: str, res: TranslationResult) -> str:
        adv = self.config.get("advanced") or {}
        show_engine = adv.get("show_engine_name", False)
        show_lang = adv.get("show_detected_lang", True)
        show_original = adv.get("show_original", False)
        use_markdown = self._use_markdown()

        meta_parts: list[str] = []
        if show_lang:
            src_code = (
                res.source_lang
                if res.source_lang != "auto"
                else (res.detected_lang or res.source_lang)
            )
            meta_parts.append(f"{get_lang_label(src_code)} → {get_lang_label(res.target_lang)}")
        if show_engine:
            suffix = "（备用回退）" if res.fallback_used else ""
            meta_parts.append(f"{res.provider_name}{suffix}")

        lines: list[str] = []
        if meta_parts:
            header = f"[{' | '.join(meta_parts)}]"
            lines.append(f"**{header}**" if use_markdown else header)
        if show_original and original.strip():
            if use_markdown:
                lines.append(f"**原文：**\n{original}\n\n**译文：**\n{res.translated_text}")
            else:
                lines.append(f"原文：{original}")
                lines.append(f"译文：{res.translated_text}")
        else:
            lines.append(res.translated_text)
        if res.dict_entries:
            lines.append("")
            if use_markdown:
                lines.append("**词典释义**")
                lines.extend(format_dict_entries_markdown(res.dict_entries))
            else:
                lines.append("词典释义：")
                lines.extend(res.dict_entries)
        return "\n".join(lines)

    async def _synthesize_voice_result(
        self,
        event: AstrMessageEvent,
        *,
        content: str,
        result: TranslationResult,
        explicit_voice: bool,
        has_images: bool,
    ):
        """Optionally synthesize and wrap pronunciation audio after text translation."""
        voice_cfg = self.config.get("voice_settings") or {}
        if not bool(voice_cfg.get("enable_voice", True)):
            return None
        auto_voice = bool(voice_cfg.get("auto_voice_for_words", False))
        if not (explicit_voice or (not has_images and auto_voice and is_word_or_phrase(content))):
            return None

        voice_mode = str(voice_cfg.get("voice_target_mode") or "smart").strip()
        try:
            tts_max_chars = int(voice_cfg.get("tts_max_chars", 300))
        except (TypeError, ValueError):
            tts_max_chars = 300
        speak_text, speak_lang = select_speech_target(
            content,
            result,
            mode=voice_mode,
            max_chars=tts_max_chars,
        )
        if not speak_text:
            return None

        async def astrbot_tts_cb(txt: str) -> str:
            return await self._call_astrbot_tts(event, txt)

        try:
            speech = await self._service.synthesize_speech(
                text=speak_text,
                lang=speak_lang,
                config=self.config,
                astrbot_tts_callback=astrbot_tts_cb,
            )
            return await self._build_voice_result(event, speech, speak_text)
        except Exception as exc:
            safe_msg = redact_config_secrets(str(exc), self.config)
            logger.warning("语音发音生成失败: %s", safe_msg)
            if explicit_voice:
                return self._make_result(event, f"语音生成失败：{safe_msg}")
            return None

    @filter.command("fy", alias={"翻译"})
    async def translate_command(self, event: AstrMessageEvent, text: GreedyStr):
        """翻译文本、图片或引用的消息，支持 /fy 与 /翻译（附带 -v 可发音）。"""
        has_reply = self._has_reply_segment(event)
        direct_images = await self._extract_message_images(event)
        raw_body = self._extract_raw_command_body(event, str(text or ""))
        parsed = parse_command_args(
            raw_body,
            has_reply=has_reply,
            has_media=bool(direct_images),
        )

        if parsed.action == "help":
            yield self._make_result(event, self._build_help_text())
            return
        if parsed.action == "status":
            yield self._make_result(event, self._build_status_text())
            return
        if parsed.action == "langs":
            yield self._make_result(event, self._build_langs_text())
            return

        content = parsed.text
        images = list(direct_images)
        if not content and not images and has_reply:
            content = await self._extract_reply_text(event)
            images = await self._extract_reply_images(
                event, allow_remote=True, fallback_reply_text=content
            )
            if not content and not images:
                yield self._make_result(event, "引用的消息中没有检测到可翻译的文本或图片内容。")
                return
        elif not images and has_reply:
            images = await self._extract_reply_images(event, allow_remote=False)

        if not content and not images:
            yield self._make_result(event, self._build_help_text())
            return

        adv = self.config.get("advanced") or {}
        try:
            max_len = max(50, int(adv.get("max_text_length", 3000)))
        except (TypeError, ValueError):
            max_len = 3000

        if len(content) > max_len:
            yield self._make_result(
                event,
                f"待翻译文本长度 ({len(content)} 字符) 超过上限 ({max_len} 字符)，请分段后重试。",
            )
            return

        op_label = "图片翻译" if images else "翻译"
        try:
            if images:
                async def image_llm_cb(
                    prompt_txt: str,
                    sys_prompt: str,
                    img_urls: list[str],
                    provider_id_override: str = "",
                ) -> str:
                    return await self._call_astrbot_image_llm(
                        event,
                        prompt_txt,
                        sys_prompt,
                        img_urls,
                        provider_id_override=provider_id_override,
                    )

                result = await self._service.translate_images(
                    image_urls=images,
                    user_text=content,
                    config=self.config,
                    source_lang=parsed.source_lang,
                    target_lang=parsed.target_lang,
                    astrbot_image_callback=image_llm_cb,
                )
            else:
                async def llm_cb(
                    txt: str,
                    src_lbl: str,
                    tgt_lbl: str,
                    dict_mode: bool = False,
                ) -> str:
                    return await self._call_astrbot_llm(
                        event, txt, src_lbl, tgt_lbl, dict_mode=dict_mode
                    )

                result = await self._service.translate(
                    text=content,
                    config=self.config,
                    source_lang=parsed.source_lang,
                    target_lang=parsed.target_lang,
                    llm_callback=llm_cb,
                )
        except TranslationError as exc:
            safe_msg = redact_config_secrets(str(exc), self.config)
            logger.warning("%s失败: %s", op_label, safe_msg)
            yield self._make_result(event, f"{op_label}失败：{safe_msg}")
            return
        except Exception as exc:
            safe_msg = redact_config_secrets(str(exc), self.config)
            logger.error("%s异常: %s", op_label, safe_msg, exc_info=True)
            yield self._make_result(event, f"{op_label}时发生内部错误：{safe_msg}")
            return

        yield self._make_result(event, self._format_output(content, result))

        voice_result = await self._synthesize_voice_result(
            event,
            content=content,
            result=result,
            explicit_voice=parsed.voice,
            has_images=bool(images),
        )
        if voice_result is not None:
            yield voice_result
