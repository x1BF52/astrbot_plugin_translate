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
    DEFAULT_VOICE_FLAGS,
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
    parse_trigger_list,
    redact_config_secrets,
    resolve_ai_provider_choice,
    select_speech_target,
)

_CMD_PREFIX_PATTERN = re.compile(r"\s*\[MSG_ID:[^\]]+\]\s*$")
_QQ_OFFICIAL_IMAGE_URL_PATTERN = re.compile(
    r"\[附件\d+\][^\n]*?类型:图片[^\n]*?URL:(https?://\S+)"
)
_DEFAULT_NORMAL_TRIGGERS = ("/fy", "/翻译")
_DEFAULT_AI_TRIGGERS = ("/aify", "/ai翻译")
_WAKE_SCHEMA_VERSION = 2
_ACTIVE_PLUGIN: "TranslatePlugin | None" = None

_BaseCustomFilter = getattr(filter, "CustomFilter", object)


def _custom_filter_decorator(filter_cls):
    deco = getattr(filter, "custom_filter", None)
    if callable(deco):
        return deco(filter_cls)
    return lambda fn: fn


class _DynamicWakeFilter(_BaseCustomFilter):
    """Match user-configured custom wake words without duplicating default @filter.command hits."""

    def filter(self, event: AstrMessageEvent, cfg: Any) -> bool:
        plugin = _ACTIVE_PLUGIN
        if plugin is None:
            return False
        if plugin._matches_registered_command(event, cfg):
            return False
        matched = plugin._match_custom_wake_trigger(event, cfg)
        if not matched:
            return False
        if hasattr(event, "set_extra"):
            event.set_extra("_translate_custom_trigger", matched)
        return True


class TranslatePlugin(Star):
    """AstrBot 翻译插件：支持 /fy、/翻译、/aify 与自定义唤醒词，内置常见免费 API 与多模态/语音能力。"""

    def __init__(self, context: Context, config: AstrBotConfig | dict | None = None):
        global _ACTIVE_PLUGIN
        super().__init__(context)
        self.config: dict[str, Any] = config if config is not None else {}
        self._session: aiohttp.ClientSession | None = None
        self._service = TranslationService(self._get_session)
        _ACTIVE_PLUGIN = self
        self._migrate_wake_settings()
        self._sync_command_filters()

    @staticmethod
    def _merge_default_triggers(
        raw_value: Any,
        defaults: tuple[str, ...],
        *,
        force_merge: bool,
    ) -> list[str] | None:
        """Return migrated trigger list if defaults should be backfilled/merged, else None."""
        existing = parse_trigger_list(raw_value)
        if not existing:
            return list(defaults)
        if force_merge:
            merged = list(defaults)
            for item in existing:
                if item not in merged:
                    merged.append(item)
            if merged != existing:
                return merged
        return None

    def _migrate_wake_settings(self) -> None:
        """Migrate legacy empty wake lists and /fyai triggers once via wake_schema_version."""
        wake_cfg = self.config.get("wake_settings")
        if not isinstance(wake_cfg, dict):
            return
        try:
            version = int(wake_cfg.get("wake_schema_version") or 0)
        except (TypeError, ValueError):
            version = 0
        if version >= _WAKE_SCHEMA_VERSION:
            return

        changed = False
        has_legacy_flag = "enable_default_fy" in wake_cfg
        legacy_enable = bool(wake_cfg.get("enable_default_fy", True))

        if version < 1 and (not has_legacy_flag or legacy_enable):
            for key, defaults in (
                ("custom_wake_words", _DEFAULT_NORMAL_TRIGGERS),
                ("custom_ai_wake_words", _DEFAULT_AI_TRIGGERS),
            ):
                if key in wake_cfg:
                    merged = self._merge_default_triggers(
                        wake_cfg.get(key),
                        defaults,
                        force_merge=has_legacy_flag and legacy_enable,
                    )
                    if merged is not None:
                        wake_cfg[key] = merged
                        changed = True
            if has_legacy_flag:
                wake_cfg.pop("enable_default_fy", None)
                changed = True

        if version < 2 and isinstance(wake_cfg.get("custom_ai_wake_words"), list):
            raw_ai = wake_cfg.get("custom_ai_wake_words") or []
            migrated_ai: list[str] = []
            ai_changed = False
            for item in raw_ai:
                token = str(item or "").strip()
                if not token:
                    continue
                if token.lower() in {"/fyai", "fyai"}:
                    token = "/aify" if token.startswith("/") else "aify"
                    ai_changed = True
                if token not in migrated_ai:
                    migrated_ai.append(token)
                else:
                    ai_changed = True
            if ai_changed:
                wake_cfg["custom_ai_wake_words"] = migrated_ai
                changed = True

        if wake_cfg.get("wake_schema_version") != _WAKE_SCHEMA_VERSION:
            wake_cfg["wake_schema_version"] = _WAKE_SCHEMA_VERSION
            changed = True

        if changed and hasattr(self.config, "save_config"):
            try:
                self.config.save_config()
            except Exception as exc:
                logger.debug("保存唤醒词迁移配置失败: %s", exc)

    async def initialize(self) -> None:
        """Initialize shared HTTP client session and sync custom command aliases on load."""
        global _ACTIVE_PLUGIN
        _ACTIVE_PLUGIN = self
        await self._get_session()
        self._sync_command_filters()

    async def terminate(self) -> None:
        """Close shared HTTP client session when plugin unloads or reloads."""
        global _ACTIVE_PLUGIN
        if _ACTIVE_PLUGIN is self:
            _ACTIVE_PLUGIN = None
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """Lazily create and return a reusable aiohttp ClientSession."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(trust_env=True)
        return self._session

    def _get_wake_prefixes(
        self,
        event: AstrMessageEvent,
        cfg: Any = None,
    ) -> list[str]:
        """Fetch configured wake prefixes for the current session."""
        try:
            eff_cfg = cfg if isinstance(cfg, dict) else self.context.get_config(
                umo=event.unified_msg_origin
            )
            prefixes = eff_cfg.get("wake_prefix", ["/"]) if isinstance(eff_cfg, dict) else ["/"]
            if isinstance(prefixes, list) and prefixes:
                return [str(p) for p in prefixes if isinstance(p, str)]
        except Exception:
            pass
        return ["/"]

    def _get_configured_triggers(self) -> tuple[list[str], list[str]]:
        """Return configured (normal_triggers, ai_triggers) using built-in defaults when unset."""
        wake_cfg = self.config.get("wake_settings") or {}
        has_legacy_flag = "enable_default_fy" in wake_cfg
        legacy_enable = bool(wake_cfg.get("enable_default_fy", True))

        if "custom_wake_words" in wake_cfg:
            normal = parse_trigger_list(wake_cfg.get("custom_wake_words"))
            if has_legacy_flag and legacy_enable:
                for item in _DEFAULT_NORMAL_TRIGGERS:
                    bare = item.lstrip("/")
                    if item not in normal and bare not in normal:
                        normal.append(item)
        else:
            normal = (
                list(_DEFAULT_NORMAL_TRIGGERS)
                if (not has_legacy_flag or legacy_enable)
                else []
            )

        if "custom_ai_wake_words" in wake_cfg:
            ai = parse_trigger_list(wake_cfg.get("custom_ai_wake_words"))
            if has_legacy_flag and legacy_enable:
                for item in _DEFAULT_AI_TRIGGERS:
                    bare = item.lstrip("/")
                    if item not in ai and bare not in ai:
                        ai.append(item)
        else:
            ai = (
                list(_DEFAULT_AI_TRIGGERS)
                if (not has_legacy_flag or legacy_enable)
                else []
            )

        return normal, ai

    def _get_voice_triggers(self) -> list[str]:
        """Return configured voice/TTS flag tokens; explicit empty list disables flags."""
        voice_cfg = self.config.get("voice_settings") or {}
        if "voice_triggers" in voice_cfg:
            return parse_trigger_list(voice_cfg.get("voice_triggers"))
        return list(DEFAULT_VOICE_FLAGS)

    @staticmethod
    def _has_explicit_symbol(trigger: str) -> bool:
        if not trigger:
            return False
        first = trigger[0]
        last = trigger[-1]
        return (not first.isalnum() and first not in {"_"}) or last in {":", "："}

    @staticmethod
    def _strip_leading_wake_prefix(trigger: str, prefixes: list[str]) -> str | None:
        """Convert '/tl' -> 'tl' if it starts with an active AstrBot wake_prefix."""
        candidates = sorted(
            {p for p in prefixes if p},
            key=len,
            reverse=True,
        )
        stripped = trigger.strip()
        for prefix in candidates:
            if stripped.startswith(prefix):
                rest = stripped[len(prefix) :].strip()
                return rest or None
        if not TranslatePlugin._has_explicit_symbol(stripped):
            return stripped
        return None

    def _sync_command_filters(self) -> None:
        """Synchronize configured custom command words onto AstrBot CommandFilter descriptors."""
        try:
            from astrbot.core.star.filter.command import CommandFilter
            from astrbot.core.star.star_handler import star_handlers_registry
        except Exception:
            return

        prefixes: list[str] = ["/"]
        try:
            cfg = self.context.get_config()
            if isinstance(cfg, dict) and isinstance(cfg.get("wake_prefix"), list):
                prefixes = [str(p) for p in cfg["wake_prefix"] if isinstance(p, str)] or ["/"]
        except Exception:
            pass

        normal_raw, ai_raw = self._get_configured_triggers()

        def _bare_cmds(items: list[str]) -> list[str]:
            cmds: list[str] = []
            for item in items:
                bare = self._strip_leading_wake_prefix(item, prefixes)
                if bare and not any(ch.isspace() for ch in bare) and bare not in cmds:
                    cmds.append(bare)
            return cmds

        target_map = {
            "translate_command": _bare_cmds(normal_raw),
            "translate_ai_command": _bare_cmds(ai_raw),
        }

        for handler in star_handlers_registry:
            if getattr(handler, "handler_module_path", None) != self.__module__:
                continue
            h_name = getattr(handler, "handler_name", "")
            if h_name not in target_map:
                continue
            cmds = target_map[h_name]
            for f in getattr(handler, "event_filters", []) or []:
                if isinstance(f, CommandFilter):
                    if not cmds:
                        handler.enabled = False
                    else:
                        handler.enabled = True
                        f.command_name = cmds[0]
                        f.alias = set(cmds[1:])
                        f._cmpl_cmd_names = None

    def _matches_registered_command(self, event: AstrMessageEvent, cfg: Any = None) -> bool:
        """Return True if our CommandFilter handlers already match this event."""
        if not getattr(event, "is_at_or_wake_command", False):
            return False
        try:
            from astrbot.core.star.filter.command import CommandFilter
            from astrbot.core.star.star_handler import star_handlers_registry
        except Exception:
            return False

        msg_str = re.sub(r"\s+", " ", (event.get_message_str() or "").strip())
        if not msg_str:
            return False
        for handler in star_handlers_registry:
            if getattr(handler, "handler_module_path", None) != self.__module__:
                continue
            if not getattr(handler, "enabled", True):
                continue
            if getattr(handler, "handler_name", "") not in {
                "translate_command",
                "translate_ai_command",
            }:
                continue
            for f in getattr(handler, "event_filters", []) or []:
                if isinstance(f, CommandFilter):
                    if not f.custom_filter_ok(event, cfg):
                        continue
                    for full_cmd in f.get_complete_command_names():
                        if msg_str == full_cmd or msg_str.startswith(f"{full_cmd} "):
                            return True
        return False

    @staticmethod
    def _match_trigger_prefix(text: str, trigger: str) -> bool:
        if not text or not trigger:
            return False
        if len(text) < len(trigger):
            return False
        if not text[: len(trigger)].lower() == trigger.lower():
            return False
        rest = text[len(trigger) :]
        if not rest:
            return True
        if trigger[-1] in {":", "："}:
            return True
        return rest[0].isspace()

    def _get_raw_plain_text(self, event: AstrMessageEvent) -> str:
        plain_parts: list[str] = []
        try:
            for seg in event.get_messages():
                if isinstance(seg, Comp.Plain) and getattr(seg, "text", None):
                    plain_parts.append(seg.text)
        except Exception:
            pass
        raw_msg = "".join(plain_parts) if plain_parts else (event.get_message_str() or "")
        return _CMD_PREFIX_PATTERN.sub("", raw_msg).lstrip()

    def _match_custom_wake_trigger(
        self,
        event: AstrMessageEvent,
        cfg: Any = None,
    ) -> tuple[bool, str] | None:
        """Check whether an incoming event matches custom wake words (returns (force_ai, trigger))."""
        normal_triggers, ai_triggers = self._get_configured_triggers()
        candidates: list[tuple[bool, str]] = [
            *((True, t) for t in ai_triggers),
            *((False, t) for t in normal_triggers),
        ]
        candidates.sort(key=lambda item: len(item[1]), reverse=True)
        if not candidates:
            return None

        raw_plain = self._get_raw_plain_text(event)
        msg_str = (event.get_message_str() or "").strip()
        consumed_prefix = (
            event.get_extra("_consumed_wake_prefix", "")
            if hasattr(event, "get_extra")
            else ""
        ) or ""
        reconstructed = f"{consumed_prefix}{msg_str}" if consumed_prefix and msg_str else msg_str
        is_wake_cmd = bool(getattr(event, "is_at_or_wake_command", False))
        prefixes = self._get_wake_prefixes(event, cfg)

        for force_ai, trig in candidates:
            if self._has_explicit_symbol(trig):
                if self._match_trigger_prefix(raw_plain, trig) or self._match_trigger_prefix(
                    reconstructed, trig
                ):
                    return force_ai, trig
            elif is_wake_cmd:
                # Stripped command string (already had wake_prefix removed by AstrBot)
                if self._match_trigger_prefix(msg_str, trig):
                    return force_ai, trig
                # Raw message with wake_prefix intact
                for prefix in sorted(prefixes, key=len, reverse=True):
                    if prefix and raw_plain.startswith(prefix):
                        tail = raw_plain[len(prefix) :].lstrip()
                        if self._match_trigger_prefix(tail, trig):
                            return force_ai, trig
                        break
        return None

    def _extract_raw_command_body(
        self,
        event: AstrMessageEvent,
        greedy_fallback: str,
    ) -> str:
        """Extract command argument tail while preserving original newlines."""
        raw_msg = self._get_raw_plain_text(event)
        normal_triggers, ai_triggers = self._get_configured_triggers()
        all_triggers = sorted(
            {*_DEFAULT_NORMAL_TRIGGERS, *_DEFAULT_AI_TRIGGERS, *normal_triggers, *ai_triggers},
            key=len,
            reverse=True,
        )

        for trig in all_triggers:
            if self._has_explicit_symbol(trig) and self._match_trigger_prefix(raw_msg, trig):
                return raw_msg[len(trig) :].strip()

        stripped_msg = raw_msg
        for prefix in sorted(self._get_wake_prefixes(event), key=len, reverse=True):
            if prefix and stripped_msg.startswith(prefix):
                stripped_msg = stripped_msg[len(prefix) :].lstrip()
                break

        for trig in all_triggers:
            if not self._has_explicit_symbol(trig) and self._match_trigger_prefix(
                stripped_msg, trig
            ):
                return stripped_msg[len(trig) :].strip()

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

    def _resolve_astrbot_provider_id(
        self,
        provider_id_override: str = "",
    ) -> str:
        """Resolve configured/manual AstrBot provider ID or name (blank = active default)."""
        raw = (provider_id_override or resolve_ai_provider_choice(self.config)).strip()
        if not raw or raw.lower() in {"openai_compat", "openai"}:
            return ""
        providers = []
        if hasattr(self.context, "get_all_providers"):
            try:
                providers = self.context.get_all_providers() or []
            except Exception:
                providers = []

        raw_lower = raw.lower()
        for exact in (True, False):
            for prov in providers:
                canonical_id = ""
                candidates: list[str] = []
                try:
                    meta = prov.meta() if hasattr(prov, "meta") else None
                    if meta and getattr(meta, "id", None):
                        canonical_id = str(meta.id)
                        candidates.append(canonical_id)
                except Exception:
                    pass
                pcfg = getattr(prov, "provider_config", None)
                if isinstance(pcfg, dict):
                    for key in ("id", "provider_source_id", "name"):
                        val = pcfg.get(key)
                        if val:
                            sval = str(val)
                            if not canonical_id and key == "id":
                                canonical_id = sval
                            candidates.append(sval)
                if hasattr(prov, "get_model"):
                    try:
                        model_name = prov.get_model()
                        if model_name:
                            candidates.append(str(model_name))
                    except Exception:
                        pass
                for cand in candidates:
                    if (cand == raw) if exact else (cand.lower() == raw_lower):
                        return canonical_id or candidates[0]
        return raw

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
        resolved_override = self._resolve_astrbot_provider_id(provider_id_override)
        llm_resp = None
        if hasattr(self.context, "get_current_chat_provider_id") and hasattr(
            self.context, "llm_generate"
        ):
            provider_id = resolved_override
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
                if resolved_override and hasattr(self.context, "get_provider_by_id"):
                    prov_res = self.context.get_provider_by_id(resolved_override)
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
        normal_trigs, ai_trigs = self._get_configured_triggers()
        ex_cmd = normal_trigs[0] if normal_trigs else (ai_trigs[0] if ai_trigs else "/fy")
        voice_trigs = self._get_voice_triggers()
        if self._use_markdown():
            normal_disp = (
                "、".join(f"`{w}`" for w in normal_trigs) if normal_trigs else "（已停用）"
            )
            ai_disp = "、".join(f"`{w}`" for w in ai_trigs) if ai_trigs else "（已停用）"
            if voice_trigs:
                v_ex = voice_trigs[0]
                v_more = " / ".join(f"`{w}`" for w in voice_trigs[1:3])
                voice_line = (
                    f"- 附带发音：`{ex_cmd} {v_ex} <文本>` 或 `{ex_cmd} 日语 {v_ex} 你好`"
                    + (f"（也支持 {v_more}）\n" if v_more else "\n")
                )
            else:
                voice_line = "- 附带发音：已关闭手动发音参数\n"
            return (
                "**翻译插件使用说明**\n"
                f"- 普通翻译：{normal_disp} `<文本>`（单词/短语自动附带词性与多义）\n"
                f"- AI 翻译：{ai_disp} `<文本>` 手动强制使用 AI 大模型翻译\n"
                f"- 发送图片或引用图片并发送 `{ex_cmd}`（可加目标语如 `{ex_cmd} 日语`）自动识图翻译\n"
                f"{voice_line}"
                f"- 引用任意消息并发送 `{ex_cmd}`（支持加目标语如 `{ex_cmd} en`）\n"
                f"- 指定目标语：`{ex_cmd} 日语 你好` | `{ex_cmd} ->ja 你好` | `{ex_cmd} en->zh Hello`\n"
                f"- 查看支持语种：`{ex_cmd} --langs` | 配置状态：`{ex_cmd} --status` | 预设探活：`{ex_cmd} --check`\n\n"
                f"**默认目标**：{target_desc}"
            )
        normal_disp_plain = "、".join(normal_trigs) if normal_trigs else "（已停用）"
        ai_disp_plain = "、".join(ai_trigs) if ai_trigs else "（已停用）"
        if voice_trigs:
            v_ex = voice_trigs[0]
            v_more_plain = " / ".join(voice_trigs[1:3])
            voice_line_plain = (
                f"• 附带发音：{ex_cmd} {v_ex} <文本> 或 {ex_cmd} 日语 {v_ex} 你好"
                + (f"（支持 {v_more_plain}）\n" if v_more_plain else "\n")
            )
        else:
            voice_line_plain = "• 附带发音：已关闭手动发音参数\n"
        return (
            "翻译插件使用说明：\n"
            f"• 普通翻译：{normal_disp_plain} <文本>（单词/短语自动附带词性与多义）\n"
            f"• AI 翻译：{ai_disp_plain} <文本> 手动强制使用 AI 大模型翻译\n"
            f"• 发送或引用图片并输入 {ex_cmd}（可加目标语如 {ex_cmd} 日语）自动识图翻译\n"
            f"{voice_line_plain}"
            f"• 引用任意消息并发送 {ex_cmd}（支持加目标语如 {ex_cmd} en）\n"
            f"• 指定目标语：{ex_cmd} 日语 你好  |  {ex_cmd} ->ja 你好  |  {ex_cmd} en->zh Hello\n"
            f"• 查看语种：{ex_cmd} --langs  |  配置状态：{ex_cmd} --status  |  预设探活：{ex_cmd} --check\n"
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

        ai_cfg = self.config.get("ai_settings") or {}
        legacy_img = self.config.get("image_translation") or {}
        ai_choice = resolve_ai_provider_choice(self.config)
        ai_prov_desc = ai_choice if ai_choice else "当前会话默认提供商"
        img_enabled = bool(
            ai_cfg.get(
                "enable_image_translation",
                legacy_img.get("enable_image_translation", True),
            )
        )
        img_status = f"开启（{ai_prov_desc}）" if img_enabled else "已关闭"

        normal_trigs, ai_trigs = self._get_configured_triggers()
        wake_desc = (
            f"普通 [{'、'.join(normal_trigs) or '无'}] | AI [{'、'.join(ai_trigs) or '无'}]"
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
        voice_trigs = self._get_voice_triggers()
        voice_trigs_desc = "、".join(voice_trigs) if voice_trigs else "无"
        voice_status = (
            f"开启（{tts_labels.get(tts_prov, tts_prov)} | 触发词: {voice_trigs_desc}）"
            if voice_enabled
            else "已关闭"
        )

        rows: list[tuple[str, str, bool]] = [
            ("主接口", preset.name, False),
            ("接口地址", active_url, True),
            ("AI 提供商（/aify & 识图）", ai_prov_desc, True),
            ("唤醒指令", wake_desc, False),
            ("默认目标", target_desc, False),
            ("图片翻译", img_status, False),
            ("语音发音", voice_status, False),
            ("输出格式", output_fmt, False),
            ("备用回退", fallback_line, False),
            ("代理模式", proxy, False),
        ]
        if self._use_markdown():
            body = "\n".join(
                f"- **{k}**：`{v}`" if is_code else f"- **{k}**：{v}"
                for k, v, is_code in rows
            )
            return (
                "**当前翻译配置状态**\n"
                f"{body}\n\n"
                "提示：发送 `/fy --check` 可并发实测所有预设在当前网络下的可用性与延迟。\n"
                "可在 AstrBot 管理面板「插件管理 -> 多引擎翻译插件 -> 配置」中修改。"
            )
        body = "\n".join(f"• {k}：{v}" for k, v, _ in rows)
        return (
            "当前翻译配置状态：\n"
            f"{body}\n"
            "提示：发送 /fy --check 可并发探活所有预设在当前网络下的可用性与延迟。\n"
            "可在 AstrBot 管理面板「插件管理 -> 多引擎翻译插件 -> 配置」中切换预设免费 API 或填写自定义接口。"
        )

    async def _build_probe_text(self) -> str:
        results = await self._service.probe_presets(self.config, timeout_seconds=4.0)
        keyless = [r for r in results if r.category == "keyless"]
        key_req = [r for r in results if r.category == "key_required"]
        self_hosted = [r for r in results if r.category in {"self_hosted", "builtin_llm"}]

        ok_list = sorted([r for r in keyless if r.status == "ok"], key=lambda x: x.latency_ms)
        fail_list = [r for r in keyless if r.status != "ok"]
        active_key = str(self.config.get("api_provider") or "bing_cn")

        def _tag(key: str) -> str:
            return " [当前]" if key == active_key else ""

        if self._use_markdown():
            lines = [
                f"**预设接口探活报告**（免密可用 {len(ok_list)}/{len(keyless)}）",
                "",
                "**免密免费接口（已实测）**",
            ]
            for r in ok_list:
                lines.append(f"- ✅ `{r.key}` {r.name}{_tag(r.key)} — `{r.detail}`")
            for r in fail_list:
                lines.append(f"- ❌ `{r.key}` {r.name}{_tag(r.key)} — {r.detail}")
            lines.append("")
            lines.append("**免费开放平台 / 大模型（需 API Key）**")
            for r in key_req:
                icon = "🔑" if r.status == "needs_key" else "✅"
                lines.append(f"- {icon} `{r.key}` {r.name}{_tag(r.key)} — {r.detail}")
            lines.append("")
            lines.append("**自建 / 内置扩展**")
            for r in self_hosted:
                icon = "🤖" if r.status == "builtin" else "🏠"
                lines.append(f"- {icon} `{r.key}` {r.name}{_tag(r.key)} — {r.detail}")
            if ok_list:
                lines.append(f"\n建议主接口优先选用延迟最低的 `{ok_list[0].key}`（{ok_list[0].detail}）。")
            return "\n".join(lines)

        lines = [
            f"预设接口探活报告（免密可用 {len(ok_list)}/{len(keyless)}）：",
            "【免密免费接口（已实测）】",
        ]
        for r in ok_list:
            lines.append(f"  ✅ {r.name} ({r.key}){_tag(r.key)}：{r.detail}")
        for r in fail_list:
            lines.append(f"  ❌ {r.name} ({r.key}){_tag(r.key)}：{r.detail}")
        lines.append("【需填免费 Key 的平台】")
        for r in key_req:
            icon = "🔑" if r.status == "needs_key" else "✅"
            lines.append(f"  {icon} {r.name} ({r.key}){_tag(r.key)}：{r.detail}")
        lines.append("【自建 / 内置扩展】")
        for r in self_hosted:
            icon = "🤖" if r.status == "builtin" else "🏠"
            lines.append(f"  {icon} {r.name} ({r.key}){_tag(r.key)}：{r.detail}")
        if ok_list:
            lines.append(f"建议主接口优先选用当前延迟最低的 {ok_list[0].name} ({ok_list[0].key}，{ok_list[0].detail})。")
        return "\n".join(lines)

    @staticmethod
    def _format_markdown_result(text: str) -> str:
        """Bold translated text in Markdown mode without breaking existing Markdown blocks."""
        stripped = text.strip()
        if not stripped:
            return text
        lines = stripped.splitlines()
        has_md = (
            "**" in stripped
            or "```" in stripped
            or any(
                re.match(r"^\s*(?:#{1,6}\s|[-*+]\s|\d+\.\s|>\s)", ln)
                for ln in lines
            )
        )
        if not has_md:
            return "\n".join(f"**{ln}**" if ln.strip() else "" for ln in lines)

        out: list[str] = []
        bolded_lead = False
        for ln in lines:
            if (
                not bolded_lead
                and ln.strip()
                and "**" not in ln
                and not re.match(r"^\s*(?:#{1,6}\s|[-*+]\s|\d+\.\s|>\s|`)", ln)
            ):
                out.append(f"**{ln.strip()}**")
                bolded_lead = True
            else:
                out.append(ln)
        return "\n".join(out)

    def _format_output(self, original: str, res: TranslationResult) -> str:
        adv = self.config.get("advanced") or {}
        show_engine = adv.get("show_engine_name", False)
        show_lang = adv.get("show_detected_lang", True)
        show_original = adv.get("show_original", False)
        use_markdown = self._use_markdown()

        lines: list[str] = [
            self._format_markdown_result(res.translated_text)
            if use_markdown
            else res.translated_text
        ]

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
        if meta_parts:
            lines.append(f"[{' | '.join(meta_parts)}]")

        if show_original and original.strip():
            lines.append(
                f"**原文**：{original}" if use_markdown else f"原文：{original}"
            )
        if res.dict_entries:
            lines.append("")
            if use_markdown:
                lines.append("**词典释义**")
                lines.extend(format_dict_entries_markdown(res.dict_entries))
            else:
                lines.append("词典释义：")
                lines.extend(res.dict_entries)
        if res.low_confidence_hint:
            lines.append("")
            lines.append(
                f"> 提示：{res.low_confidence_hint}"
                if use_markdown
                else f"提示：{res.low_confidence_hint}"
            )
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

    async def _run_image_translation(
        self,
        event: AstrMessageEvent,
        *,
        images: list[str],
        content: str,
        source_lang: str,
        target_lang: str | None,
    ) -> TranslationResult:
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

        return await self._service.translate_images(
            image_urls=images,
            user_text=content,
            config=self.config,
            source_lang=source_lang,
            target_lang=target_lang,
            astrbot_image_callback=image_llm_cb,
        )

    async def _run_text_translation(
        self,
        event: AstrMessageEvent,
        *,
        content: str,
        source_lang: str,
        target_lang: str | None,
        force_ai: bool,
    ) -> TranslationResult:
        provider_override: str | None = None
        if force_ai:
            ai_choice = resolve_ai_provider_choice(self.config)
            provider_override = (
                "openai_compat"
                if ai_choice.lower() in {"openai_compat", "openai"}
                else "astrbot_llm"
            )

        async def llm_cb(
            txt: str,
            src_lbl: str,
            tgt_lbl: str,
            dict_mode: bool = False,
        ) -> str:
            return await self._call_astrbot_llm(
                event, txt, src_lbl, tgt_lbl, dict_mode=dict_mode
            )

        try:
            return await self._service.translate(
                text=content,
                config=self.config,
                source_lang=source_lang,
                target_lang=target_lang,
                llm_callback=llm_cb,
                provider_override=provider_override,
            )
        except TranslationError as ai_exc:
            plugin_llm_key = str(self.config.get("api_provider") or "")
            if (
                force_ai
                and provider_override == "astrbot_llm"
                and not resolve_ai_provider_choice(self.config)
                and plugin_llm_key
                in {"openai_compat", "zhipu_free", "siliconflow_free", "groq_free"}
                and "未配置可用的大模型提供商" in str(ai_exc)
            ):
                return await self._service.translate(
                    text=content,
                    config=self.config,
                    source_lang=source_lang,
                    target_lang=target_lang,
                    llm_callback=llm_cb,
                    provider_override=plugin_llm_key,
                )
            raise

    async def _execute_translate(
        self,
        event: AstrMessageEvent,
        raw_text: str,
        *,
        force_ai: bool = False,
    ):
        """Shared execution path for /fy, /aify, and custom wake-word triggers."""
        has_reply = self._has_reply_segment(event)
        direct_images = await self._extract_message_images(event)
        raw_body = self._extract_raw_command_body(event, raw_text)
        parsed = parse_command_args(
            raw_body,
            has_reply=has_reply,
            has_media=bool(direct_images),
            voice_flags=self._get_voice_triggers(),
        )

        if parsed.action == "help":
            yield self._make_result(event, self._build_help_text())
            return
        if parsed.action == "status":
            yield self._make_result(event, self._build_status_text())
            return
        if parsed.action == "check":
            yield self._make_result(event, await self._build_probe_text())
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

        op_label = "图片翻译" if images else ("AI 翻译" if force_ai else "翻译")
        try:
            if images:
                result = await self._run_image_translation(
                    event,
                    images=images,
                    content=content,
                    source_lang=parsed.source_lang,
                    target_lang=parsed.target_lang,
                )
            else:
                result = await self._run_text_translation(
                    event,
                    content=content,
                    source_lang=parsed.source_lang,
                    target_lang=parsed.target_lang,
                    force_ai=force_ai,
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

    @filter.command("fy", alias={"翻译"})
    async def translate_command(self, event: AstrMessageEvent, text: GreedyStr):
        """翻译文本、图片或引用的消息，支持 /fy 与 /翻译（附带 -v 可发音）。"""
        async for res in self._execute_translate(event, str(text or ""), force_ai=False):
            yield res

    @filter.command("aify", alias={"ai翻译"})
    async def translate_ai_command(self, event: AstrMessageEvent, text: GreedyStr):
        """手动指定使用 AI 大模型进行翻译，支持 /aify 与 /ai翻译。"""
        async for res in self._execute_translate(event, str(text or ""), force_ai=True):
            yield res

    @_custom_filter_decorator(_DynamicWakeFilter)
    async def custom_wake_translate(self, event: AstrMessageEvent):
        """响应用户在配置中动态添加的自定义唤醒词（如 /tl、!tl 等）。"""
        extra = (
            event.get_extra("_translate_custom_trigger")
            if hasattr(event, "get_extra")
            else None
        )
        force_ai = bool(extra[0]) if isinstance(extra, tuple) and extra else False
        async for res in self._execute_translate(event, "", force_ai=force_ai):
            yield res
