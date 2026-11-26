"""Core multi-engine translation service, command parser, and preset registry."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html
import json
import random
import re
import time
import urllib.parse
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import aiohttp

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
)

DEFAULT_LLM_PROMPT = (
    "You are a professional translator. Translate the user's text into "
    "{target_lang} accurately and naturally, preserving paragraph breaks and "
    "formatting. Output ONLY the translated text without explanations."
)

DEFAULT_LLM_DICT_PROMPT = (
    "You are a concise bilingual dictionary and translator. The user provided a "
    "word or short phrase to translate into {target_lang}. Output:\n"
    "1. Primary translation and phonetic transcription / reading (if applicable).\n"
    "2. Common meanings grouped by part of speech (e.g. n., v., adj., adv., prep.) "
    "with multiple senses per part of speech.\n"
    "3. 1-2 short collocations or brief bilingual examples if helpful.\n"
    "Keep the output clean, well-structured, and free of filler."
)


def build_llm_system_prompt(
    custom_prompt: str,
    source_label: str,
    target_label: str,
    *,
    dict_mode: bool = False,
    use_markdown: bool = False,
) -> str:
    """Build LLM system prompt tailored to translation vs dictionary and Markdown mode."""
    cleaned_custom = (custom_prompt or "").strip()
    if cleaned_custom and cleaned_custom != DEFAULT_LLM_PROMPT:
        tpl = cleaned_custom
    elif dict_mode:
        style = (
            "Format clearly using concise Markdown (bold part-of-speech labels like **n.**, **v.**, bullet lists, and inline code for phonetics)."
            if use_markdown
            else "Use clean plain text with bullet prefix '• ' and NO Markdown syntax (no **, #, or backticks)."
        )
        tpl = f"{DEFAULT_LLM_DICT_PROMPT}\n{style}"
    else:
        tpl = (
            DEFAULT_LLM_PROMPT
            if use_markdown
            else f"{DEFAULT_LLM_PROMPT} Output plain text without extra Markdown formatting."
        )
    return tpl.replace("{target_lang}", target_label).replace("{source_lang}", source_label)


def format_dict_entries_markdown(entries: list[str]) -> list[str]:
    """Format dictionary entry lines into readable Markdown list items."""
    out: list[str] = []
    for line in entries:
        if line.startswith("• "):
            body = line[2:].strip()
            match = re.match(r"^([A-Za-z]+\.|\[[^\]]+\]|【[^】]+】|释义)\s+([\s\S]+)$", body)
            if match:
                out.append(f"- **{match.group(1)}** {match.group(2)}")
            else:
                out.append(f"- {body}")
        elif line.startswith("常见搭配："):
            rest = line[len("常见搭配：") :].strip()
            rest = re.sub(
                r"([A-Za-z][A-Za-z0-9' -]*[A-Za-z0-9])\s*\(([^)]+)\)",
                r"`\1` (\2)",
                rest,
            )
            out.append(f"- **常见搭配**：{rest}")
        else:
            formatted = re.sub(r"/([^/\n]+)/", r"`/\1/`", line)
            out.append(f"- **发音**：{formatted}")
    return out


class TranslationError(Exception):
    """Raised when a translation provider fails or returns invalid data."""


@dataclass(frozen=True)
class ProviderPreset:
    """Metadata describing a built-in translation API preset."""

    key: str
    name: str
    default_url: str
    kind: str
    requires_key: bool = False
    default_sub_engine: str = ""
    default_model: str = ""
    model_config_key: str = ""


PROVIDER_PRESETS: dict[str, ProviderPreset] = {
    "bing_cn": ProviderPreset(
        key="bing_cn",
        name="Bing 翻译 (国内版)",
        default_url="https://cn.bing.com/ttranslatev3",
        kind="bing",
    ),
    "bing_global": ProviderPreset(
        key="bing_global",
        name="Bing 翻译 (国际版)",
        default_url="https://www.bing.com/ttranslatev3",
        kind="bing",
    ),
    "youdao_web": ProviderPreset(
        key="youdao_web",
        name="有道网页翻译",
        default_url="https://m.youdao.com/translate",
        kind="youdao_web",
    ),
    "mymemory": ProviderPreset(
        key="mymemory",
        name="MyMemory",
        default_url="https://api.mymemory.translated.net/get",
        kind="mymemory",
    ),
    "google_free": ProviderPreset(
        key="google_free",
        name="Google 翻译 (GTX)",
        default_url="https://translate.googleapis.com/translate_a/single",
        kind="google_gtx",
    ),
    "google_clients5": ProviderPreset(
        key="google_clients5",
        name="Google 翻译 (Chrome)",
        default_url="https://clients5.google.com/translate_a/t",
        kind="google_clients5",
    ),
    "mozhi_adminforge": ProviderPreset(
        key="mozhi_adminforge",
        name="Mozhi (AdminForge)",
        default_url="https://mozhi.adminforge.de/api/translate",
        kind="mozhi",
        default_sub_engine="duckduckgo",
    ),
    "mozhi_aryak": ProviderPreset(
        key="mozhi_aryak",
        name="Mozhi (Aryak)",
        default_url="https://mozhi.aryak.me/api/translate",
        kind="mozhi",
        default_sub_engine="yandex",
    ),
    "mozhi_pussthecat": ProviderPreset(
        key="mozhi_pussthecat",
        name="Mozhi (PussTheCat)",
        default_url="https://mozhi.pussthecat.org/api/translate",
        kind="mozhi",
        default_sub_engine="duckduckgo",
    ),
    "lingva_ml": ProviderPreset(
        key="lingva_ml",
        name="Lingva",
        default_url="https://lingva.ml/api/v1",
        kind="lingva",
    ),
    "zhipu_free": ProviderPreset(
        key="zhipu_free",
        name="智谱 AI (免费模型)",
        default_url="https://open.bigmodel.cn/api/paas/v4/chat/completions",
        kind="openai_compat",
        requires_key=True,
        default_model="GLM-4-Flash-250414",
        model_config_key="zhipu_model",
    ),
    "siliconflow_free": ProviderPreset(
        key="siliconflow_free",
        name="硅基流动 (免费模型)",
        default_url="https://api.siliconflow.cn/v1/chat/completions",
        kind="openai_compat",
        requires_key=True,
        default_model="tencent/Hunyuan-MT-7B",
        model_config_key="siliconflow_model",
    ),
    "groq_free": ProviderPreset(
        key="groq_free",
        name="Groq (免费开源模型)",
        default_url="https://api.groq.com/openai/v1/chat/completions",
        kind="openai_compat",
        requires_key=True,
        default_model="openai/gpt-oss-120b",
        model_config_key="groq_model",
    ),
    "niutrans": ProviderPreset(
        key="niutrans",
        name="小牛翻译",
        default_url="https://api.niutrans.com/NiuTransServer/translation",
        kind="niutrans",
        requires_key=True,
    ),
    "volcengine": ProviderPreset(
        key="volcengine",
        name="火山引擎机器翻译",
        default_url="https://translate.volcengineapi.com",
        kind="volcengine",
        requires_key=True,
    ),
    "baidu_fanyi": ProviderPreset(
        key="baidu_fanyi",
        name="百度翻译开放平台",
        default_url="https://fanyi-api.baidu.com/api/trans/vip/translate",
        kind="baidu_fanyi",
        requires_key=True,
    ),
    "aliyun_mt": ProviderPreset(
        key="aliyun_mt",
        name="阿里云机器翻译",
        default_url="https://mt.cn-hangzhou.aliyuncs.com",
        kind="aliyun_mt",
        requires_key=True,
    ),
    "libretranslate": ProviderPreset(
        key="libretranslate",
        name="LibreTranslate",
        default_url="https://libretranslate.com/translate",
        kind="libretranslate",
    ),
    "deeplx": ProviderPreset(
        key="deeplx",
        name="DeepLX",
        default_url="http://127.0.0.1:1188/translate",
        kind="deeplx",
    ),
    "openai_compat": ProviderPreset(
        key="openai_compat",
        name="OpenAI 兼容接口",
        default_url="https://api.openai.com/v1/chat/completions",
        kind="openai_compat",
        default_model="gpt-4o-mini",
        model_config_key="openai_model",
    ),
    "astrbot_llm": ProviderPreset(
        key="astrbot_llm",
        name="AstrBot 内置 LLM",
        default_url="",
        kind="astrbot_llm",
    ),
    "custom_http": ProviderPreset(
        key="custom_http",
        name="自定义 HTTP API",
        default_url="",
        kind="custom_http",
    ),
}

LANG_LABELS: dict[str, str] = {
    "auto": "自动检测",
    "zh-CN": "简体中文",
    "zh-TW": "繁体中文",
    "en": "英语",
    "ja": "日语",
    "ko": "韩语",
    "fr": "法语",
    "de": "德语",
    "es": "西班牙语",
    "ru": "俄语",
    "it": "意大利语",
    "pt": "葡萄牙语",
    "ar": "阿拉伯语",
    "th": "泰语",
    "vi": "越南语",
    "id": "印尼语",
    "nl": "荷兰语",
    "pl": "波兰语",
    "tr": "土耳其语",
    "uk": "乌克兰语",
    "he": "希伯来语",
    "hi": "印地语",
    "ms": "马来语",
    "sv": "瑞典语",
    "da": "丹麦语",
    "fi": "芬兰语",
    "no": "挪威语",
    "cs": "捷克语",
    "el": "希腊语",
    "hu": "匈牙利语",
    "ro": "罗马尼亚语",
}

LANG_ALIASES: dict[str, str] = {
    "auto": "auto",
    "auto-detect": "auto",
    "自动": "auto",
    "zh": "zh-CN",
    "zh-cn": "zh-CN",
    "zh_cn": "zh-CN",
    "zh-hans": "zh-CN",
    "zh-chs": "zh-CN",
    "chs": "zh-CN",
    "cn": "zh-CN",
    "中文": "zh-CN",
    "简体": "zh-CN",
    "简体中文": "zh-CN",
    "中": "zh-CN",
    "汉语": "zh-CN",
    "zh-tw": "zh-TW",
    "zh_tw": "zh-TW",
    "zh-hant": "zh-TW",
    "zh-cht": "zh-TW",
    "cht": "zh-TW",
    "zt": "zh-TW",
    "tw": "zh-TW",
    "hk": "zh-TW",
    "繁中": "zh-TW",
    "繁体": "zh-TW",
    "繁体中文": "zh-TW",
    "en": "en",
    "en-us": "en",
    "en-gb": "en",
    "eng": "en",
    "英": "en",
    "英文": "en",
    "英语": "en",
    "ja": "ja",
    "jp": "ja",
    "jpn": "ja",
    "日": "ja",
    "日文": "ja",
    "日语": "ja",
    "ko": "ko",
    "kr": "ko",
    "kor": "ko",
    "韩": "ko",
    "韩文": "ko",
    "韩语": "ko",
    "fr": "fr",
    "fra": "fr",
    "fre": "fr",
    "法": "fr",
    "法文": "fr",
    "法语": "fr",
    "de": "de",
    "deu": "de",
    "ger": "de",
    "德": "de",
    "德文": "de",
    "德语": "de",
    "es": "es",
    "sp": "es",
    "spa": "es",
    "西": "es",
    "西语": "es",
    "西班牙语": "es",
    "ru": "ru",
    "rus": "ru",
    "俄": "ru",
    "俄文": "ru",
    "俄语": "ru",
    "it": "it",
    "ita": "it",
    "意语": "it",
    "意大利语": "it",
    "pt": "pt",
    "pt-br": "pt",
    "pt-pt": "pt",
    "por": "pt",
    "葡语": "pt",
    "葡萄牙语": "pt",
    "ar": "ar",
    "ara": "ar",
    "阿语": "ar",
    "阿拉伯语": "ar",
    "th": "th",
    "tha": "th",
    "泰文": "th",
    "泰语": "th",
    "vi": "vi",
    "vie": "vi",
    "越语": "vi",
    "越南语": "vi",
    "id": "id",
    "ind": "id",
    "印尼语": "id",
    "nl": "nl",
    "nld": "nl",
    "荷兰语": "nl",
    "pl": "pl",
    "pol": "pl",
    "波兰语": "pl",
    "tr": "tr",
    "tur": "tr",
    "土耳其语": "tr",
    "uk": "uk",
    "ukr": "uk",
    "乌克兰语": "uk",
    "he": "he",
    "iw": "he",
    "heb": "he",
    "希伯来语": "he",
    "hi": "hi",
    "hin": "hi",
    "印地语": "hi",
    "ms": "ms",
    "马来语": "ms",
    "sv": "sv",
    "瑞典语": "sv",
    "da": "da",
    "丹麦语": "da",
    "fi": "fi",
    "芬兰语": "fi",
    "no": "no",
    "nb": "no",
    "挪威语": "no",
    "cs": "cs",
    "捷克语": "cs",
    "el": "el",
    "希腊语": "el",
    "hu": "hu",
    "匈牙利语": "hu",
    "ro": "ro",
    "罗马尼亚语": "ro",
}

# Unambiguous hyphenated or multi-char CJK language names allowed without ':'/'->'
_UNAMBIGUOUS_PREFIX_CODES = {
    "zh-cn",
    "zh-tw",
    "zh-hans",
    "zh-hant",
    "zh_cn",
    "zh_tw",
    "中文",
    "简体",
    "简体中文",
    "汉语",
    "繁中",
    "繁体",
    "繁体中文",
    "英文",
    "英语",
    "日文",
    "日语",
    "韩文",
    "韩语",
    "法文",
    "法语",
    "德文",
    "德语",
    "西语",
    "西班牙语",
    "俄文",
    "俄语",
    "意语",
    "意大利语",
    "葡语",
    "葡萄牙语",
    "阿语",
    "阿拉伯语",
    "泰文",
    "泰语",
    "越语",
    "越南语",
    "印尼语",
    "荷兰语",
    "波兰语",
    "土耳其语",
    "乌克兰语",
    "希伯来语",
    "印地语",
    "马来语",
    "瑞典语",
    "丹麦语",
    "芬兰语",
    "挪威语",
    "捷克语",
    "希腊语",
    "匈牙利语",
    "罗马尼亚语",
}
_SAFE_SHORT_WITH_NON_ASCII = {"zh", "en", "ja", "jp", "ko", "kr", "fr", "de", "es", "ru"}

_BING_LANG_MAP = {
    "auto": "auto-detect",
    "zh-CN": "zh-Hans",
    "zh-TW": "zh-Hant",
}

_MOZHI_LANG_MAP = {
    "zh-CN": "zh",
    "zh-TW": "zh-TW",
}

_LINGVA_LANG_MAP = {
    "zh-CN": "zh",
    "zh-TW": "zh_HANT",
}

_LIBRE_LANG_MAP = {
    "zh-CN": "zh",
    "zh-TW": "zt",
}

_DEEPLX_LANG_MAP = {
    "zh-CN": "ZH",
    "zh-TW": "ZH-HANT",
}

_YOUDAO_ZH_PREFIX = {
    "zh-CN": "ZH_CN",
    "zh-TW": "ZH_CN",
    "en": "EN",
    "ja": "JA",
    "ko": "KR",
    "fr": "FR",
    "ru": "RU",
    "es": "SP",
}

_NIUTRANS_LANG_MAP = {
    "zh-CN": "zh",
    "zh-TW": "cht",
}

_VOLCENGINE_LANG_MAP = {
    "zh-CN": "zh",
    "zh-TW": "zh-Hant",
}

_BAIDU_LANG_MAP = {
    "zh-CN": "zh",
    "zh-TW": "cht",
    "ja": "jp",
    "ko": "kor",
    "fr": "fra",
    "es": "spa",
    "ar": "ara",
    "vi": "vie",
    "ro": "rom",
    "da": "dan",
    "fi": "fin",
    "sv": "swe",
    "ms": "may",
    "no": "nor",
    "he": "heb",
    "uk": "ukr",
}

_ALIYUN_LANG_MAP = {
    "zh-CN": "zh",
    "zh-TW": "zh-tw",
}

_BING_POS_LABELS = {
    "NOUN": "n.",
    "VERB": "v.",
    "ADJ": "adj.",
    "ADV": "adv.",
    "PREP": "prep.",
    "CONJ": "conj.",
    "PRON": "pron.",
    "INTJ": "intj.",
    "DET": "det.",
    "OTHER": "释义",
}


@dataclass
class ParsedCommand:
    """Result of parsing a /fy or /翻译 command invocation."""

    action: str  # "translate" | "help" | "status" | "langs"
    source_lang: str = "auto"
    target_lang: str | None = None
    text: str = ""


@dataclass
class TranslationResult:
    """Normalized response returned by a translation backend."""

    translated_text: str
    provider_key: str
    provider_name: str
    source_lang: str
    target_lang: str
    detected_lang: str = ""
    fallback_used: bool = False
    errors: list[str] | None = None
    dict_entries: list[str] | None = None


def normalize_lang(raw: str | None, allow_auto: bool = True) -> str | None:
    """Normalize a user-supplied language code or Chinese name into canonical form."""
    if not raw:
        return None
    key = raw.strip().lower()
    canonical = LANG_ALIASES.get(key)
    if canonical == "auto" and not allow_auto:
        return None
    return canonical


def get_lang_label(code: str) -> str:
    """Return a human-readable Chinese label for a canonical language code."""
    if not code:
        return "未知"
    canonical = normalize_lang(code, allow_auto=True) or code
    return LANG_LABELS.get(canonical, code)


def detect_script_lang(text: str) -> str:
    """Heuristically detect primary script language when an API requires an explicit source."""
    if not text:
        return "en"
    if re.search(r"[\u3040-\u30ff]", text):
        return "ja"
    if re.search(r"[\uac00-\ud7af\u1100-\u11ff]", text):
        return "ko"
    han_count = len(re.findall(r"[\u4e00-\u9fff]", text))
    latin_count = len(re.findall(r"[A-Za-z]", text))
    if han_count > 0 and (latin_count == 0 or han_count * 2 >= latin_count):
        return "zh-CN"
    if re.search(r"[\u0400-\u04ff]", text):
        return "ru"
    if re.search(r"[\u0600-\u06ff]", text):
        return "ar"
    if re.search(r"[\u0e00-\u0e7f]", text):
        return "th"
    return "en"


def resolve_target_lang(
    text: str,
    default_target: str,
    explicit_target: str | None = None,
) -> str:
    """Determine final target language, resolving smart bilingual auto_zh_en mode."""
    if explicit_target:
        return explicit_target
    if default_target == "auto_zh_en":
        detected = detect_script_lang(text)
        return "en" if detected in {"zh-CN", "zh-TW"} else "zh-CN"
    return normalize_lang(default_target, allow_auto=False) or "zh-CN"


def parse_command_args(raw_body: str, has_reply: bool = False) -> ParsedCommand:
    """Parse flags, optional inline language specifier, and translation text."""
    stripped = raw_body.strip()
    if not stripped:
        return ParsedCommand(action="translate" if has_reply else "help")

    lower = stripped.lower()
    if lower in {"-h", "--help"}:
        return ParsedCommand(action="help")
    if lower in {"--status", "--apis", "--engines"}:
        return ParsedCommand(action="status")
    if lower in {"--langs", "--languages", "--lang"}:
        return ParsedCommand(action="langs")

    # 1. Explicit -t <lang> / --to <lang> / --to=<lang>
    flag_match = re.match(
        r"^(?:-t|--to)(?:\s+|=)(\S+)(?:[\s\r\n]+([\s\S]*))?$",
        stripped,
        flags=re.IGNORECASE,
    )
    if flag_match:
        tgt = normalize_lang(flag_match.group(1), allow_auto=False)
        if tgt:
            return ParsedCommand(
                action="translate",
                target_lang=tgt,
                text=(flag_match.group(2) or "").strip(),
            )

    # 2. Explicit [src]->tgt (e.g. "->ja hello", "en->zh hello", "中->英 你好")
    arrow_match = re.match(
        r"^([^\s\->:：]*)->([^\s:：]+)(?:[\s\r\n]+([\s\S]*))?$",
        stripped,
    )
    if arrow_match:
        raw_src, raw_tgt, rest = arrow_match.groups()
        tgt = normalize_lang(raw_tgt, allow_auto=False)
        src = normalize_lang(raw_src, allow_auto=True) if raw_src else "auto"
        if tgt and (not raw_src or src):
            return ParsedCommand(
                action="translate",
                source_lang=src or "auto",
                target_lang=tgt,
                text=(rest or "").strip(),
            )

    # 3. Explicit colon prefix: "ja: hello" or "日语：你好"
    colon_match = re.match(
        r"^([^\s:：]+)[:：]\s*([\s\S]*)$",
        stripped,
    )
    if colon_match:
        tgt = normalize_lang(colon_match.group(1), allow_auto=False)
        if tgt:
            return ParsedCommand(
                action="translate",
                target_lang=tgt,
                text=(colon_match.group(2) or "").strip(),
            )

    # 4. Space-separated first token (strictly guarded against false positives)
    parts = re.split(r"(\s+)", stripped, maxsplit=1)
    first_token = parts[0]
    rest_text = parts[2].strip() if len(parts) == 3 else ""
    token_lower = first_token.lower()
    tgt = normalize_lang(first_token, allow_auto=False)

    if tgt:
        # Single language token when replying to a quoted message: "/fy en" or "/fy 日语"
        if not rest_text and has_reply:
            return ParsedCommand(action="translate", target_lang=tgt, text="")
        # Hyphenated codes (zh-cn, zh-tw) or short codes followed by non-ASCII body
        if rest_text and (
            token_lower in _UNAMBIGUOUS_PREFIX_CODES
            or (
                token_lower in _SAFE_SHORT_WITH_NON_ASCII
                and any(ord(ch) > 127 for ch in rest_text[:12])
            )
        ):
            return ParsedCommand(action="translate", target_lang=tgt, text=rest_text)

    return ParsedCommand(action="translate", text=stripped)


def chunk_text(text: str, max_chars: int) -> list[str]:
    """Split long text into chunks not exceeding max_chars, preserving line breaks."""
    limit = max(1, int(max_chars))
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            for i in range(0, len(line), limit):
                chunks.append(line[i : i + limit])
            continue
        if len(current) + len(line) > limit:
            chunks.append(current)
            current = line
        else:
            current += line
    if current:
        chunks.append(current)
    return chunks or [text]


def extract_json_path(payload: Any, path: str) -> Any:
    """Extract a value from nested dicts/lists via dot/bracket path like data.0.text."""
    if not path:
        return payload
    tokens = [tok for tok in re.split(r"\.|\[|\]", path) if tok != ""]
    curr = payload
    for tok in tokens:
        if isinstance(curr, list):
            if not tok.isdigit():
                return None
            idx = int(tok)
            if idx < 0 or idx >= len(curr):
                return None
            curr = curr[idx]
        elif isinstance(curr, dict):
            if tok not in curr:
                return None
            curr = curr[tok]
        else:
            return None
    return curr


_TEMPLATE_VAR_PATTERN = re.compile(r"\{\{([A-Za-z0-9_]+)\}\}")
_THINK_BLOCK_PATTERN = re.compile(r"<think>[\s\S]*?</think>\s*", re.IGNORECASE)


def clean_llm_translation(text: str) -> str:
    """Strip reasoning <think> blocks from LLM translation outputs."""
    if not text:
        return ""
    cleaned = _THINK_BLOCK_PATTERN.sub("", text).strip()
    return cleaned or text.strip()


def redact_secret(message: str, secret: str | None) -> str:
    """Mask any configured API key or its URL-encoded form in error text."""
    if not message or not secret:
        return message
    masked = message.replace(secret, "***")
    encoded = urllib.parse.quote(secret, safe="")
    if encoded and encoded != secret:
        masked = masked.replace(encoded, "***")
    return masked


def redact_config_secrets(message: str, config: dict[str, Any]) -> str:
    """Mask all credential fields configured in plugin config."""
    out = message
    for key in ("api_key", "volcengine_secret_key", "aliyun_access_secret"):
        val = str(config.get(key) or "").strip()
        if val:
            out = redact_secret(out, val)
    return out


def is_word_or_phrase(text: str) -> bool:
    """Return True when text looks like a single word or short phrase suitable for dictionary lookup."""
    s = (text or "").strip()
    if not s or "\n" in s or "\r" in s:
        return False
    if re.search(r"https?://|@", s, re.IGNORECASE):
        return False
    if re.search(r"[。！？；.!?;，,、]", s):
        return False
    if not re.search(r"[A-Za-z\u3040-\u30ff\u4e00-\u9fff\uac00-\ud7af\u0400-\u04ff]", s):
        return False
    if re.search(r"[\u3040-\u30ff\u4e00-\u9fff\uac00-\ud7af]", s):
        return len(s) <= 8
    tokens = s.split()
    return 1 <= len(tokens) <= 4 and len(s) <= 48


def _truncate_senses(raw: str, max_senses: int = 5, max_len: int = 88) -> str:
    """Keep a dictionary definition concise without cutting mid-word or mid-clause."""
    cleaned = re.sub(r"\s+", " ", raw).strip()
    if len(cleaned) <= max_len:
        return cleaned
    parts = [p.strip() for p in re.split(r"[；;]", cleaned) if p.strip()]
    if len(parts) > 1:
        kept: list[str] = []
        total = 0
        for part in parts[:max_senses]:
            add_len = len(part) + (1 if kept else 0)
            if kept and total + add_len > max_len:
                break
            kept.append(part)
            total += add_len
        if kept:
            joined = "；".join(kept)
            return joined + ("…" if len(kept) < len(parts) else "")
    return cleaned[: max_len - 1].rstrip() + "…"


def _render_template(
    template: str,
    variables: dict[str, str],
    transform: Callable[[str], str],
) -> str:
    """Substitute {{var}} placeholders in a single pass to prevent chained expansion."""
    return _TEMPLATE_VAR_PATTERN.sub(
        lambda m: transform(variables[m.group(1)]) if m.group(1) in variables else m.group(0),
        template,
    )


def render_json_template(template: str, variables: dict[str, str]) -> str:
    """Substitute {{var}} placeholders safely for JSON or text payloads."""
    stripped = template.lstrip()
    is_json_shape = stripped.startswith("{") or stripped.startswith("[")
    return _render_template(
        template,
        variables,
        lambda val: json.dumps(val, ensure_ascii=False)[1:-1] if is_json_shape else val,
    )


def render_url_template(url: str, variables: dict[str, str]) -> str:
    """Substitute {{var}} placeholders safely in URLs using percent encoding."""
    return _render_template(
        url,
        variables,
        lambda val: urllib.parse.quote(val, safe=""),
    )


def _is_local_url(url: str) -> bool:
    host = urllib.parse.urlparse(url).hostname or ""
    return host in {"127.0.0.1", "localhost", "::1"}


def _map_lang(code: str, table: dict[str, str], default: str | None = None) -> str:
    if code in table:
        return table[code]
    return default if default is not None else code


class TranslationService:
    """Async multi-engine translation dispatcher with fallback and token caching."""

    def __init__(self, session_getter: Callable[[], Awaitable[aiohttp.ClientSession]]):
        self._get_session = session_getter
        self._bing_tokens: dict[str, dict[str, Any]] = {}
        self._bing_locks: dict[str, asyncio.Lock] = {}

    def _request_proxy(self, url: str, config: dict[str, Any]) -> str | None:
        proxy = str(config.get("proxy_url") or "").strip()
        if not proxy or _is_local_url(url):
            return None
        return proxy

    def _timeout(self, config: dict[str, Any]) -> aiohttp.ClientTimeout:
        adv = config.get("advanced") or {}
        seconds = adv.get("timeout_seconds", 15)
        try:
            val = max(3, min(120, int(seconds)))
        except (TypeError, ValueError):
            val = 15
        return aiohttp.ClientTimeout(total=val)

    async def _read_json_response(
        self,
        resp: Any,
        provider_name: str,
        *,
        allow_error_json: bool = False,
    ) -> Any:
        raw = await resp.text(errors="ignore")
        if resp.status not in range(200, 300):
            if allow_error_json:
                try:
                    err_data = json.loads(raw)
                except json.JSONDecodeError:
                    err_data = None
                if isinstance(err_data, dict):
                    raw_err = err_data.get("error")
                    detail = (
                        raw_err.get("message")
                        if isinstance(raw_err, dict)
                        else (raw_err or err_data.get("message"))
                    )
                    if detail:
                        raise TranslationError(f"HTTP {resp.status}: {detail}")
            detail_tail = f": {raw[:180].strip()}" if allow_error_json and raw.strip() else ""
            raise TranslationError(f"HTTP {resp.status}{detail_tail}")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise TranslationError(f"{provider_name} 返回了非 JSON 响应") from exc

    async def _translate_chunked(
        self,
        text: str,
        max_chars: int,
        translate_chunk: Callable[[str], Awaitable[tuple[str, str]]],
    ) -> tuple[str, str]:
        parts: list[str] = []
        detected_lang = ""
        for chunk in chunk_text(text, max_chars):
            core = chunk.strip()
            if not core:
                parts.append(chunk)
                continue
            lead_len = len(chunk) - len(chunk.lstrip())
            trail_len = len(chunk) - len(chunk.rstrip())
            lead_ws = chunk[:lead_len]
            trail_ws = chunk[len(chunk) - trail_len :] if trail_len else ""
            translated_core, det = await translate_chunk(core)
            parts.append(f"{lead_ws}{translated_core.strip()}{trail_ws}")
            if det and not detected_lang:
                detected_lang = det
        return "".join(parts), detected_lang

    async def translate(
        self,
        text: str,
        config: dict[str, Any],
        source_lang: str = "auto",
        target_lang: str | None = None,
        llm_callback: Callable[..., Awaitable[str]] | None = None,
    ) -> TranslationResult:
        """Translate text via configured primary API with optional auto fallback."""
        default_target = str(config.get("default_target_lang") or "auto_zh_en")
        resolved_target = resolve_target_lang(text, default_target, target_lang)

        primary = str(config.get("api_provider") or "bing_cn")
        if primary not in PROVIDER_PRESETS:
            primary = "bing_cn"

        adv = config.get("advanced") or {}
        dict_mode = bool(adv.get("enable_dict_lookup", True)) and is_word_or_phrase(text)

        chain = [primary]
        if config.get("enable_auto_fallback", True):
            for fb in config.get("fallback_providers") or []:
                if isinstance(fb, str) and fb in PROVIDER_PRESETS and fb not in chain:
                    chain.append(fb)

        errors: list[str] = []
        for idx, provider_key in enumerate(chain):
            preset = PROVIDER_PRESETS[provider_key]
            try:
                translated, detected = await self._dispatch_provider(
                    preset=preset,
                    text=text,
                    source_lang=source_lang,
                    target_lang=resolved_target,
                    config=config,
                    use_custom_override=(idx == 0),
                    llm_callback=llm_callback,
                    dict_mode=dict_mode,
                )
                cleaned = translated.strip()
                if not cleaned:
                    raise TranslationError("返回译文为空")
                dict_entries: list[str] | None = None
                if dict_mode and preset.kind not in {"openai_compat", "astrbot_llm"}:
                    dict_entries = await self._lookup_dictionary(
                        text=text.strip(),
                        source_lang=source_lang,
                        target_lang=resolved_target,
                        detected_lang=detected,
                        config=config,
                    ) or None
                return TranslationResult(
                    translated_text=cleaned,
                    provider_key=preset.key,
                    provider_name=preset.name,
                    source_lang=source_lang,
                    target_lang=resolved_target,
                    detected_lang=detected,
                    fallback_used=(idx > 0),
                    errors=errors or None,
                    dict_entries=dict_entries,
                )
            except Exception as exc:
                safe_err = redact_config_secrets(str(exc), config)
                errors.append(f"{preset.name}: {safe_err}")

        raise TranslationError("；".join(errors) if errors else "所有翻译接口均请求失败")

    async def _dispatch_provider(
        self,
        preset: ProviderPreset,
        text: str,
        source_lang: str,
        target_lang: str,
        config: dict[str, Any],
        use_custom_override: bool,
        llm_callback: Callable[..., Awaitable[str]] | None,
        dict_mode: bool = False,
    ) -> tuple[str, str]:
        custom_url = str(config.get("custom_api_url") or "").strip() if use_custom_override else ""
        url = custom_url or preset.default_url
        api_key = str(config.get("api_key") or "").strip() if use_custom_override else ""

        if preset.kind != "astrbot_llm":
            if not url or not re.match(r"^https?://", url, re.IGNORECASE):
                raise TranslationError("未配置有效的 HTTP/HTTPS API 地址")

        dispatch_map = {
            "bing": self._translate_bing,
            "youdao_web": self._translate_youdao_web,
            "mymemory": self._translate_mymemory,
            "google_gtx": self._translate_google_gtx,
            "google_clients5": self._translate_google_clients5,
            "mozhi": self._translate_mozhi,
            "lingva": self._translate_lingva,
            "niutrans": self._translate_niutrans,
            "volcengine": self._translate_volcengine,
            "baidu_fanyi": self._translate_baidu_fanyi,
            "aliyun_mt": self._translate_aliyun_mt,
            "libretranslate": self._translate_libretranslate,
            "deeplx": self._translate_deeplx,
            "openai_compat": self._translate_openai_compat,
            "custom_http": self._translate_custom_http,
        }

        if preset.kind == "astrbot_llm":
            if llm_callback is None:
                raise TranslationError("当前环境不支持内置 LLM 回调")
            src_label = get_lang_label(source_lang)
            tgt_label = get_lang_label(target_lang)
            try:
                out = await llm_callback(text, src_label, tgt_label, dict_mode=dict_mode)
            except TypeError:
                out = await llm_callback(text, src_label, tgt_label)
            return out, ""

        handler = dispatch_map.get(preset.kind)
        if not handler:
            raise TranslationError(f"未知的接口类型: {preset.kind}")
        return await handler(
            preset=preset,
            url=url,
            text=text,
            source_lang=source_lang,
            target_lang=target_lang,
            api_key=api_key,
            config=config,
            dict_mode=dict_mode,
        )

    async def _fetch_bing_token(
        self,
        base_origin: str,
        config: dict[str, Any],
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        lock = self._bing_locks.setdefault(base_origin, asyncio.Lock())
        async with lock:
            cached = self._bing_tokens.get(base_origin)
            now = time.monotonic()
            if not force_refresh and cached and now < cached["expires_at"]:
                cached["sfx"] += 1
                return dict(cached)

            page_url = f"{base_origin}/translator"
            session = await self._get_session()
            async with session.get(
                page_url,
                headers={"User-Agent": DEFAULT_UA},
                proxy=self._request_proxy(page_url, config),
                timeout=self._timeout(config),
                allow_redirects=True,
            ) as resp:
                if resp.status != 200:
                    raise TranslationError(f"获取 Bing 会话令牌失败 (HTTP {resp.status})")
                body = await resp.text(errors="ignore")
                parsed_final = urllib.parse.urlparse(str(resp.url))
                if parsed_final.scheme and parsed_final.netloc:
                    final_origin = f"{parsed_final.scheme}://{parsed_final.netloc}"
                else:
                    final_origin = f"{resp.url.scheme}://{resp.url.host}"

            helper = re.search(
                r"params_AbusePreventionHelper\s*=\s*\[(\d+)\s*,\s*['\"]([^'\"]+)['\"]",
                body,
            )
            ig_match = re.search(r"IG\s*[:=]\s*['\"]([A-Za-z0-9]{16,64})['\"]", body)
            if not helper or not ig_match:
                raise TranslationError("解析 Bing 翻译页面令牌失败")
            iid_match = re.search(r"data-iid=['\"]([^'\"]+)['\"]", body)

            entry = {
                "origin": final_origin,
                "key": helper.group(1),
                "token": helper.group(2),
                "ig": ig_match.group(1),
                "iid": iid_match.group(1) if iid_match else "translator.5023",
                "sfx": 1,
                "expires_at": now + 2700,
            }
            self._bing_tokens[base_origin] = entry
            return dict(entry)

    async def _translate_bing(
        self,
        url: str,
        text: str,
        source_lang: str,
        target_lang: str,
        config: dict[str, Any],
        **_: Any,
    ) -> tuple[str, str]:
        parsed = urllib.parse.urlparse(url)
        base_origin = f"{parsed.scheme}://{parsed.netloc}"
        src = _map_lang(source_lang, _BING_LANG_MAP)
        tgt = _map_lang(target_lang, _BING_LANG_MAP)

        session = await self._get_session()
        explicit_ep = url if parsed.path.endswith("ttranslatev3") else ""
        return await self._translate_chunked(
            text,
            900,
            lambda part: self._bing_single_request(
                session=session,
                base_origin=base_origin,
                explicit_endpoint=explicit_ep,
                text=part,
                src=src,
                tgt=tgt,
                config=config,
            ),
        )

    async def _bing_single_request(
        self,
        session: aiohttp.ClientSession,
        base_origin: str,
        explicit_endpoint: str,
        text: str,
        src: str,
        tgt: str,
        config: dict[str, Any],
    ) -> tuple[str, str]:
        for attempt in range(2):
            tok = await self._fetch_bing_token(
                base_origin, config, force_refresh=(attempt > 0)
            )
            api_base = (
                f"{tok['origin']}/ttranslatev3"
                if tok["origin"] != base_origin
                else (explicit_endpoint or f"{tok['origin']}/ttranslatev3")
            )

            req_url = (
                f"{api_base}?isVertical=1&IG={tok['ig']}"
                f"&IID={tok['iid']}&SFX={tok['sfx']}"
            )
            headers = {
                "User-Agent": DEFAULT_UA,
                "Origin": tok["origin"],
                "Referer": f"{tok['origin']}/translator",
                "Accept": "*/*",
            }
            form = {
                "fromLang": src,
                "to": tgt,
                "text": text,
                "token": tok["token"],
                "key": tok["key"],
                "tryFetchingGenderDebiasedTranslations": "true",
            }
            async with session.post(
                req_url,
                data=form,
                headers=headers,
                proxy=self._request_proxy(req_url, config),
                timeout=self._timeout(config),
            ) as resp:
                raw = await resp.text(errors="ignore")
                if resp.status != 200:
                    if attempt == 0:
                        continue
                    raise TranslationError(f"HTTP {resp.status}")

            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                if attempt == 0:
                    continue
                raise TranslationError("Bing 返回了非 JSON 响应") from exc

            if isinstance(data, dict):
                if data.get("ShowCaptcha"):
                    raise TranslationError("触发 Bing 验证码限制")
                sc = data.get("statusCode")
                if attempt == 0 and sc != 200:
                    continue
                raise TranslationError(f"Bing 接口错误 (statusCode={sc})")

            if isinstance(data, list) and data:
                translations = data[0].get("translations") or []
                if translations:
                    translated = translations[0].get("text", "")
                    det = (data[0].get("detectedLanguage") or {}).get("language", "")
                    return translated, det

            raise TranslationError("Bing 返回结构异常")

        raise TranslationError("Bing 请求失败")

    async def _translate_youdao_web(
        self,
        url: str,
        text: str,
        source_lang: str,
        target_lang: str,
        config: dict[str, Any],
        **_: Any,
    ) -> tuple[str, str]:
        actual_src = detect_script_lang(text) if source_lang == "auto" else source_lang
        if target_lang == "zh-TW":
            raise TranslationError("有道网页免费版仅支持简体中文，暂不支持繁体中文目标语种")
        if target_lang == "zh-CN" and source_lang == "auto":
            youdao_type = "AUTO"
        else:
            src_code = _YOUDAO_ZH_PREFIX.get(actual_src)
            tgt_code = _YOUDAO_ZH_PREFIX.get(target_lang)
            if not src_code or not tgt_code:
                raise TranslationError("有道网页免费版暂不支持该语种组合")
            if src_code == tgt_code:
                youdao_type = "AUTO"
            elif src_code == "ZH_CN" or tgt_code == "ZH_CN":
                youdao_type = f"{src_code}2{tgt_code}"
            else:
                raise TranslationError("有道网页免费版仅支持中文与外语互译")

        session = await self._get_session()
        headers = {
            "User-Agent": DEFAULT_UA,
            "Referer": url,
        }

        async def _send_youdao(chunk: str) -> tuple[str, str]:
            async with session.post(
                url,
                data={"inputtext": chunk, "type": youdao_type},
                headers=headers,
                proxy=self._request_proxy(url, config),
                timeout=self._timeout(config),
            ) as resp:
                if resp.status != 200:
                    raise TranslationError(f"HTTP {resp.status}")
                body = await resp.text(errors="ignore")

            ul_match = re.search(
                r'<ul[^>]*id=["\']translateResult["\'][^>]*>(.*?)</ul>',
                body,
                flags=re.S | re.I,
            )
            if not ul_match:
                raise TranslationError("未解析到有道翻译结果")
            items = re.findall(r"<li[^>]*>(.*?)</li>", ul_match.group(1), flags=re.S | re.I)
            lines = [html.unescape(re.sub(r"<[^>]+>", "", item)).strip() for item in items]
            result = "\n".join(line for line in lines if line)
            if not result:
                raise TranslationError("有道翻译结果为空")
            return result, actual_src if source_lang == "auto" else ""

        return await self._translate_chunked(text, 900, _send_youdao)

    async def _translate_mymemory(
        self,
        url: str,
        text: str,
        source_lang: str,
        target_lang: str,
        config: dict[str, Any],
        **_: Any,
    ) -> tuple[str, str]:
        actual_src = detect_script_lang(text) if source_lang == "auto" else source_lang
        if actual_src == target_lang:
            actual_src = "en" if target_lang in {"zh-CN", "zh-TW"} else "zh-CN"

        adv = config.get("advanced") or {}
        email = str(adv.get("mymemory_email") or "").strip()
        session = await self._get_session()

        async def _send_mymemory(chunk: str) -> tuple[str, str]:
            params: dict[str, str] = {
                "q": chunk,
                "langpair": f"{actual_src}|{target_lang}",
            }
            if email:
                params["de"] = email
            async with session.get(
                url,
                params=params,
                headers={"User-Agent": DEFAULT_UA},
                proxy=self._request_proxy(url, config),
                timeout=self._timeout(config),
            ) as resp:
                if resp.status == 429:
                    raise TranslationError("MyMemory 触发速率限制 (429)")
                data = await self._read_json_response(resp, "MyMemory")

            status = data.get("responseStatus")
            if status != 200:
                detail = data.get("responseDetails") or f"status={status}"
                raise TranslationError(f"MyMemory 错误: {detail}")
            translated = (data.get("responseData") or {}).get("translatedText", "")
            return html.unescape(translated), actual_src if source_lang == "auto" else ""

        return await self._translate_chunked(text, 450, _send_mymemory)

    async def _translate_google_gtx(
        self,
        url: str,
        text: str,
        source_lang: str,
        target_lang: str,
        config: dict[str, Any],
        **_: Any,
    ) -> tuple[str, str]:
        session = await self._get_session()

        async def _send_gtx(chunk: str) -> tuple[str, str]:
            params = {
                "client": "gtx",
                "sl": source_lang,
                "tl": target_lang,
                "dt": "t",
                "q": chunk,
            }
            async with session.get(
                url,
                params=params,
                headers={"User-Agent": DEFAULT_UA},
                proxy=self._request_proxy(url, config),
                timeout=self._timeout(config),
            ) as resp:
                data = await self._read_json_response(resp, "Google GTX")

            if not isinstance(data, list) or not data or not isinstance(data[0], list):
                raise TranslationError("Google GTX 返回结构异常")
            translated = "".join(
                str(seg[0]) for seg in data[0] if isinstance(seg, list) and seg and seg[0]
            )
            detected = str(data[2]) if len(data) > 2 and isinstance(data[2], str) else ""
            return translated, detected

        return await self._translate_chunked(text, 900, _send_gtx)

    async def _translate_google_clients5(
        self,
        url: str,
        text: str,
        source_lang: str,
        target_lang: str,
        config: dict[str, Any],
        **_: Any,
    ) -> tuple[str, str]:
        session = await self._get_session()

        async def _send_c5(chunk: str) -> tuple[str, str]:
            params = {
                "client": "dict-chrome-ex",
                "sl": source_lang,
                "tl": target_lang,
                "dt": "t",
                "q": chunk,
            }
            async with session.get(
                url,
                params=params,
                headers={"User-Agent": DEFAULT_UA},
                proxy=self._request_proxy(url, config),
                timeout=self._timeout(config),
            ) as resp:
                data = await self._read_json_response(resp, "Google Clients5")

            if isinstance(data, list) and data:
                if isinstance(data[0], list):
                    seg_texts: list[str] = []
                    detected = ""
                    for item in data:
                        if isinstance(item, list) and item:
                            seg_texts.append(str(item[0]))
                            if not detected and len(item) > 1 and isinstance(item[1], str):
                                detected = item[1]
                    return "".join(seg_texts), detected
                if isinstance(data[0], str):
                    return "".join(str(x) for x in data), ""
            if isinstance(data, dict) and "sentences" in data:
                return "".join(s.get("trans", "") for s in data["sentences"]), ""
            raise TranslationError("Google Clients5 返回结构异常")

        return await self._translate_chunked(text, 900, _send_c5)

    async def _translate_mozhi(
        self,
        preset: ProviderPreset,
        url: str,
        text: str,
        source_lang: str,
        target_lang: str,
        config: dict[str, Any],
        **_: Any,
    ) -> tuple[str, str]:
        adv = config.get("advanced") or {}
        engine = str(adv.get("mozhi_engine") or "auto").strip()
        if engine == "auto":
            engine = preset.default_sub_engine or "duckduckgo"

        req_url = url.rstrip("/")
        if not req_url.endswith("/api/translate"):
            req_url = f"{req_url}/api/translate"

        lang_map = (
            {"zh-CN": "zh-CN", "zh-TW": "zh-TW"}
            if engine == "google"
            else _MOZHI_LANG_MAP
        )
        session = await self._get_session()

        async def _send_mozhi(chunk: str) -> tuple[str, str]:
            params = {
                "engine": engine,
                "from": _map_lang(source_lang, lang_map),
                "to": _map_lang(target_lang, lang_map),
                "text": chunk,
            }
            async with session.get(
                req_url,
                params=params,
                headers={"User-Agent": DEFAULT_UA, "Accept": "application/json"},
                proxy=self._request_proxy(req_url, config),
                timeout=self._timeout(config),
            ) as resp:
                data = await self._read_json_response(
                    resp, "Mozhi", allow_error_json=True
                )

            translated = str(data.get("translated-text") or "")
            detected = str(data.get("detected") or "")
            if not translated:
                raise TranslationError("Mozhi 未返回译文")
            return translated, detected

        return await self._translate_chunked(text, 800, _send_mozhi)

    async def _translate_lingva(
        self,
        url: str,
        text: str,
        source_lang: str,
        target_lang: str,
        config: dict[str, Any],
        **_: Any,
    ) -> tuple[str, str]:
        base = url.rstrip("/")
        if not base.endswith("/api/v1"):
            base = f"{base}/api/v1"

        src = _map_lang(source_lang, _LINGVA_LANG_MAP)
        tgt = _map_lang(target_lang, _LINGVA_LANG_MAP)
        session = await self._get_session()

        async def _send_lingva(chunk: str) -> tuple[str, str]:
            encoded = urllib.parse.quote(chunk, safe="")
            req_url = f"{base}/{src}/{tgt}/{encoded}"
            async with session.get(
                req_url,
                headers={"User-Agent": DEFAULT_UA, "Accept": "application/json"},
                proxy=self._request_proxy(req_url, config),
                timeout=self._timeout(config),
            ) as resp:
                data = await self._read_json_response(
                    resp, "Lingva", allow_error_json=True
                )

            if data.get("error"):
                raise TranslationError(str(data["error"]))
            info = data.get("info") or {}
            detected = str(info.get("detectedSource") or "") if isinstance(info, dict) else ""
            return str(data.get("translation") or ""), detected

        return await self._translate_chunked(text, 1000, _send_lingva)

    async def _translate_libretranslate(
        self,
        url: str,
        text: str,
        source_lang: str,
        target_lang: str,
        api_key: str,
        config: dict[str, Any],
        **_: Any,
    ) -> tuple[str, str]:
        req_url = url.rstrip("/")
        if not req_url.endswith("/translate"):
            req_url = f"{req_url}/translate"

        payload: dict[str, Any] = {
            "q": text,
            "source": _map_lang(source_lang, _LIBRE_LANG_MAP),
            "target": _map_lang(target_lang, _LIBRE_LANG_MAP),
            "format": "text",
        }
        if api_key:
            payload["api_key"] = api_key

        session = await self._get_session()
        async with session.post(
            req_url,
            json=payload,
            headers={"User-Agent": DEFAULT_UA, "Accept": "application/json"},
            proxy=self._request_proxy(req_url, config),
            timeout=self._timeout(config),
        ) as resp:
            data = await self._read_json_response(
                resp, "LibreTranslate", allow_error_json=True
            )

        translated = str(data.get("translatedText") or "")
        detected = str((data.get("detectedLanguage") or {}).get("language") or "")
        return translated, detected

    async def _translate_deeplx(
        self,
        url: str,
        text: str,
        source_lang: str,
        target_lang: str,
        api_key: str,
        config: dict[str, Any],
        **_: Any,
    ) -> tuple[str, str]:
        req_url = url.rstrip("/")
        if not req_url.endswith("/translate"):
            req_url = f"{req_url}/translate"

        tgt = _map_lang(target_lang, _DEEPLX_LANG_MAP, target_lang.split("-")[0].upper())
        payload: dict[str, Any] = {
            "text": text,
            "target_lang": tgt,
        }
        if source_lang != "auto":
            payload["source_lang"] = _map_lang(
                source_lang, _DEEPLX_LANG_MAP, source_lang.split("-")[0].upper()
            )
        else:
            payload["source_lang"] = "auto"

        headers = {"User-Agent": DEFAULT_UA, "Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        session = await self._get_session()
        async with session.post(
            req_url,
            json=payload,
            headers=headers,
            proxy=self._request_proxy(req_url, config),
            timeout=self._timeout(config),
        ) as resp:
            data = await self._read_json_response(resp, "DeepLX", allow_error_json=True)

        if data.get("code") not in (None, 200):
            raise TranslationError(str(data.get("message") or f"code={data.get('code')}"))
        translated = str(data.get("data") or "")
        if not translated:
            raise TranslationError("DeepLX 未返回译文")
        return translated, str(data.get("source_lang") or "")

    async def _translate_niutrans(
        self,
        url: str,
        text: str,
        source_lang: str,
        target_lang: str,
        api_key: str,
        config: dict[str, Any],
        **_: Any,
    ) -> tuple[str, str]:
        if not api_key:
            raise TranslationError("请先在配置页填写小牛翻译 API Key")

        req_url = url.rstrip("/")
        parsed = urllib.parse.urlparse(req_url)
        if not parsed.path:
            req_url = f"{req_url}/NiuTransServer/translation"

        src = _map_lang(source_lang, _NIUTRANS_LANG_MAP)
        tgt = _map_lang(target_lang, _NIUTRANS_LANG_MAP)
        session = await self._get_session()

        async def _send_niutrans(chunk: str) -> tuple[str, str]:
            payload = {
                "from": src,
                "to": tgt,
                "apikey": api_key,
                "src_text": chunk,
            }
            async with session.post(
                req_url,
                json=payload,
                headers={"User-Agent": DEFAULT_UA, "Accept": "application/json"},
                proxy=self._request_proxy(req_url, config),
                timeout=self._timeout(config),
            ) as resp:
                data = await self._read_json_response(
                    resp, "小牛翻译", allow_error_json=True
                )

            if data.get("error_code"):
                msg = data.get("error_msg") or data.get("error_code")
                raise TranslationError(f"小牛翻译错误 ({data.get('error_code')}): {msg}")
            translated = str(data.get("tgt_text") or "")
            if not translated:
                raise TranslationError("小牛翻译未返回译文")
            detected = str(data.get("from") or "") if source_lang == "auto" else ""
            return translated, detected

        return await self._translate_chunked(text, 1500, _send_niutrans)

    @staticmethod
    def _volc_hmac_sha256(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

    async def _translate_volcengine(
        self,
        url: str,
        text: str,
        source_lang: str,
        target_lang: str,
        api_key: str,
        config: dict[str, Any],
        **_: Any,
    ) -> tuple[str, str]:
        secret_key = str(config.get("volcengine_secret_key") or "").strip()
        if not api_key or not secret_key:
            raise TranslationError("请先在配置页填写火山引擎 AccessKey ID 与 Secret AccessKey")

        parsed = urllib.parse.urlparse(url.rstrip("/"))
        host = parsed.netloc
        path = parsed.path or "/"
        base_url = f"{parsed.scheme}://{host}{path}"
        region = "cn-north-1"
        service = "translate"
        query_params = {"Action": "TranslateText", "Version": "2020-06-01"}
        canonical_query = "&".join(
            f"{urllib.parse.quote(k, safe='-_.~')}={urllib.parse.quote(v, safe='-_.~')}"
            for k, v in sorted(query_params.items())
        )
        tgt = _map_lang(target_lang, _VOLCENGINE_LANG_MAP)
        src = _map_lang(source_lang, _VOLCENGINE_LANG_MAP) if source_lang != "auto" else ""
        session = await self._get_session()

        async def _send_volc(chunk: str) -> tuple[str, str]:
            body_obj: dict[str, Any] = {
                "TargetLanguage": tgt,
                "TextList": [chunk],
            }
            if src:
                body_obj["SourceLanguage"] = src
            body_bytes = json.dumps(body_obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

            x_date = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            short_date = x_date[:8]
            content_type = "application/json"
            payload_hash = hashlib.sha256(body_bytes).hexdigest()
            signed_headers = "content-type;host;x-content-sha256;x-date"
            canonical_headers = (
                f"content-type:{content_type}\n"
                f"host:{host}\n"
                f"x-content-sha256:{payload_hash}\n"
                f"x-date:{x_date}\n"
            )
            canonical_req = "\n".join(
                [
                    "POST",
                    path,
                    canonical_query,
                    canonical_headers,
                    signed_headers,
                    payload_hash,
                ]
            )
            credential_scope = f"{short_date}/{region}/{service}/request"
            string_to_sign = "\n".join(
                [
                    "HMAC-SHA256",
                    x_date,
                    credential_scope,
                    hashlib.sha256(canonical_req.encode("utf-8")).hexdigest(),
                ]
            )
            k_date = self._volc_hmac_sha256(secret_key.encode("utf-8"), short_date)
            k_region = self._volc_hmac_sha256(k_date, region)
            k_service = self._volc_hmac_sha256(k_region, service)
            k_signing = self._volc_hmac_sha256(k_service, "request")
            signature = self._volc_hmac_sha256(k_signing, string_to_sign).hex()

            headers = {
                "Host": host,
                "Content-Type": content_type,
                "X-Date": x_date,
                "X-Content-Sha256": payload_hash,
                "Authorization": (
                    f"HMAC-SHA256 Credential={api_key}/{credential_scope}, "
                    f"SignedHeaders={signed_headers}, Signature={signature}"
                ),
            }
            req_url = f"{base_url}?{canonical_query}"
            async with session.post(
                req_url,
                data=body_bytes,
                headers=headers,
                proxy=self._request_proxy(req_url, config),
                timeout=self._timeout(config),
            ) as resp:
                data = await self._read_json_response(
                    resp, "火山引擎机器翻译", allow_error_json=True
                )

            meta_err = (data.get("ResponseMetadata") or {}).get("Error")
            if isinstance(meta_err, dict) and (meta_err.get("Code") or meta_err.get("Message")):
                raise TranslationError(
                    f"火山引擎错误 ({meta_err.get('Code', '')}): {meta_err.get('Message', '')}"
                )
            items = data.get("TranslationList") or []
            if not items or not isinstance(items[0], dict):
                raise TranslationError("火山引擎未返回译文")
            translated = str(items[0].get("Translation") or "")
            detected = str(items[0].get("DetectedSourceLanguage") or "")
            return translated, detected

        return await self._translate_chunked(text, 1500, _send_volc)

    async def _translate_baidu_fanyi(
        self,
        url: str,
        text: str,
        source_lang: str,
        target_lang: str,
        api_key: str,
        config: dict[str, Any],
        **_: Any,
    ) -> tuple[str, str]:
        app_id = str(config.get("baidu_app_id") or "").strip()
        if not app_id or not api_key:
            raise TranslationError("请先在配置页填写百度翻译 APP ID 与密钥")

        src = _map_lang(source_lang, _BAIDU_LANG_MAP)
        tgt = _map_lang(target_lang, _BAIDU_LANG_MAP)
        session = await self._get_session()

        async def _send_baidu(chunk: str) -> tuple[str, str]:
            salt = str(random.randint(100000, 999999))
            sign = hashlib.md5(f"{app_id}{chunk}{salt}{api_key}".encode("utf-8")).hexdigest()
            form = {
                "q": chunk,
                "from": src,
                "to": tgt,
                "appid": app_id,
                "salt": salt,
                "sign": sign,
            }
            async with session.post(
                url,
                data=form,
                headers={"User-Agent": DEFAULT_UA, "Accept": "application/json"},
                proxy=self._request_proxy(url, config),
                timeout=self._timeout(config),
            ) as resp:
                data = await self._read_json_response(
                    resp, "百度翻译", allow_error_json=True
                )

            if data.get("error_code"):
                raise TranslationError(
                    f"百度翻译错误 ({data.get('error_code')}): {data.get('error_msg', '')}"
                )
            items = data.get("trans_result") or []
            if not items:
                raise TranslationError("百度翻译未返回译文")
            translated = "\n".join(str(item.get("dst") or "") for item in items if isinstance(item, dict))
            detected = str(data.get("from") or "") if source_lang == "auto" else ""
            return translated, detected

        return await self._translate_chunked(text, 1500, _send_baidu)

    @staticmethod
    def _aliyun_quote(val: str) -> str:
        return urllib.parse.quote(val, safe="-_.~")

    async def _translate_aliyun_mt(
        self,
        url: str,
        text: str,
        source_lang: str,
        target_lang: str,
        api_key: str,
        config: dict[str, Any],
        **_: Any,
    ) -> tuple[str, str]:
        access_secret = str(config.get("aliyun_access_secret") or "").strip()
        if not api_key or not access_secret:
            raise TranslationError("请先在配置页填写阿里云 AccessKey ID 与 AccessKey Secret")

        req_url = url.rstrip("/") + "/"
        src = _map_lang(source_lang, _ALIYUN_LANG_MAP)
        tgt = _map_lang(target_lang, _ALIYUN_LANG_MAP)
        session = await self._get_session()

        async def _send_aliyun(chunk: str) -> tuple[str, str]:
            params = {
                "Action": "TranslateGeneral",
                "Version": "2018-10-12",
                "Format": "JSON",
                "FormatType": "text",
                "Scene": "general",
                "SourceLanguage": src,
                "TargetLanguage": tgt,
                "SourceText": chunk,
                "AccessKeyId": api_key,
                "Timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "SignatureMethod": "HMAC-SHA1",
                "SignatureVersion": "1.0",
                "SignatureNonce": uuid.uuid4().hex,
            }
            canonical = "&".join(
                f"{self._aliyun_quote(k)}={self._aliyun_quote(v)}"
                for k, v in sorted(params.items())
            )
            string_to_sign = f"POST&%2F&{self._aliyun_quote(canonical)}"
            digest = hmac.new(
                f"{access_secret}&".encode("utf-8"),
                string_to_sign.encode("utf-8"),
                hashlib.sha1,
            ).digest()
            params["Signature"] = base64.b64encode(digest).decode("utf-8")

            async with session.post(
                req_url,
                data=params,
                headers={"User-Agent": DEFAULT_UA, "Accept": "application/json"},
                proxy=self._request_proxy(req_url, config),
                timeout=self._timeout(config),
            ) as resp:
                data = await self._read_json_response(
                    resp, "阿里云机器翻译", allow_error_json=True
                )

            code = str(data.get("Code") or "")
            if code and code != "200":
                raise TranslationError(f"阿里云翻译错误 ({code}): {data.get('Message', '')}")
            translated = str(((data.get("Data") or {}).get("Translated")) or "")
            if not translated:
                raise TranslationError("阿里云机器翻译未返回译文")
            detected = str(((data.get("Data") or {}).get("DetectedLanguage")) or "")
            return translated, detected

        return await self._translate_chunked(text, 1500, _send_aliyun)

    async def _translate_openai_compat(
        self,
        preset: ProviderPreset,
        url: str,
        text: str,
        source_lang: str,
        target_lang: str,
        api_key: str,
        config: dict[str, Any],
        dict_mode: bool = False,
        **_: Any,
    ) -> tuple[str, str]:
        req_url = url.rstrip("/")
        if not req_url.endswith("/chat/completions"):
            if re.search(r"/(?:v\d+(?:alpha|beta)?|openai)$", req_url, re.IGNORECASE):
                req_url = f"{req_url}/chat/completions"
            else:
                req_url = f"{req_url}/v1/chat/completions"

        model_key = preset.model_config_key or "openai_model"
        model = str(config.get(model_key) or preset.default_model or "gpt-4o-mini").strip()

        custom_prompt = (
            str(config.get("openai_system_prompt") or "").strip()
            if preset.key == "openai_compat"
            else ""
        )
        adv = config.get("advanced") or {}
        use_markdown = bool(adv.get("output_markdown", False))
        system_prompt = build_llm_system_prompt(
            custom_prompt,
            get_lang_label(source_lang),
            get_lang_label(target_lang),
            dict_mode=dict_mode,
            use_markdown=use_markdown,
        )

        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        payload = {
            "model": model,
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text},
            ],
        }

        session = await self._get_session()
        async with session.post(
            req_url,
            json=payload,
            headers=headers,
            proxy=self._request_proxy(req_url, config),
            timeout=self._timeout(config),
        ) as resp:
            data = await self._read_json_response(
                resp, "OpenAI 兼容接口", allow_error_json=True
            )

        content = extract_json_path(data, "choices.0.message.content")
        if isinstance(content, list):
            content = "".join(
                str(part.get("text") or "")
                for part in content
                if isinstance(part, dict)
            )
        if not isinstance(content, str) or not content.strip():
            raise TranslationError("OpenAI 兼容接口未返回有效译文")
        return clean_llm_translation(content), ""

    async def _translate_custom_http(
        self,
        url: str,
        text: str,
        source_lang: str,
        target_lang: str,
        api_key: str,
        config: dict[str, Any],
        **_: Any,
    ) -> tuple[str, str]:
        variables = {
            "text": text,
            "source": source_lang,
            "target": target_lang,
            "api_key": api_key,
        }
        req_url = render_url_template(url, variables)
        method = str(config.get("custom_http_method") or "POST").upper()

        headers: dict[str, str] = {"User-Agent": DEFAULT_UA}
        raw_headers = str(config.get("custom_http_headers") or "").strip()
        if raw_headers:
            try:
                parsed_headers = json.loads(raw_headers)
                if not isinstance(parsed_headers, dict):
                    raise TranslationError("自定义请求头必须是 JSON 对象")
                for k, v in parsed_headers.items():
                    headers[str(k)] = str(v).replace("{{api_key}}", api_key)
            except json.JSONDecodeError as exc:
                raise TranslationError("自定义请求头 JSON 格式错误") from exc

        body_data: Any = None
        if method != "GET":
            body_tpl = str(config.get("custom_http_body") or "")
            content_type = next(
                (v for k, v in headers.items() if k.lower() == "content-type"), ""
            ).lower()
            if "application/x-www-form-urlencoded" in content_type:
                body_str = _render_template(
                    body_tpl,
                    variables,
                    urllib.parse.quote_plus,
                )
                body_data = body_str.encode("utf-8")
            else:
                body_data = render_json_template(body_tpl, variables).encode("utf-8")
                if not content_type:
                    headers["Content-Type"] = "application/json"

        session = await self._get_session()
        req_kwargs: dict[str, Any] = {
            "headers": headers,
            "proxy": self._request_proxy(req_url, config),
            "timeout": self._timeout(config),
        }
        if body_data is not None:
            req_kwargs["data"] = body_data

        async with session.request(method, req_url, **req_kwargs) as resp:
            raw = await resp.text(errors="ignore")
            if resp.status not in range(200, 300):
                raise TranslationError(f"HTTP {resp.status}: {raw[:180]}")

        resp_path = str(config.get("custom_http_response_path") or "").strip()
        if not resp_path:
            return raw, ""

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise TranslationError("自定义 API 返回了非 JSON 内容") from exc

        extracted = extract_json_path(payload, resp_path)
        if extracted is None:
            raise TranslationError(f"响应中未找到路径: {resp_path}")
        if isinstance(extracted, list):
            extracted = "".join(str(x) for x in extracted)
        return str(extracted), ""

    async def _lookup_dictionary(
        self,
        text: str,
        source_lang: str,
        target_lang: str,
        detected_lang: str,
        config: dict[str, Any],
    ) -> list[str]:
        """Fetch multi-POS dictionary senses for words/phrases without failing main translation."""
        src_hint = (
            source_lang
            if source_lang != "auto"
            else (normalize_lang(detected_lang, allow_auto=False) or detect_script_lang(text))
        )
        if src_hint == target_lang:
            return []

        prefer_youdao = target_lang == "zh-CN" and src_hint in {"en", "ja", "fr"}
        try:
            if prefer_youdao:
                entries = await self._fetch_youdao_dict(text, src_hint, target_lang, config)
                if entries:
                    return entries
                return await self._fetch_bing_dict(text, src_hint, target_lang, config)

            entries = await self._fetch_bing_dict(text, src_hint, target_lang, config)
            if entries:
                return entries
            if target_lang == "zh-CN" or (src_hint == "zh-CN" and target_lang == "en"):
                return await self._fetch_youdao_dict(text, src_hint, target_lang, config)
        except Exception:
            return []
        return []

    async def _fetch_youdao_dict(
        self,
        text: str,
        src_hint: str,
        target_lang: str,
        config: dict[str, Any],
    ) -> list[str]:
        if target_lang not in {"zh-CN", "en"}:
            return []

        params: dict[str, str] = {"q": text}
        if src_hint == "ja":
            params["le"] = "jap"
        elif src_hint == "fr":
            params["le"] = "fr"

        url = "https://dict.youdao.com/jsonapi"
        session = await self._get_session()
        async with session.get(
            url,
            params=params,
            headers={"User-Agent": DEFAULT_UA, "Accept": "application/json"},
            proxy=self._request_proxy(url, config),
            timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            if resp.status != 200:
                return []
            data = json.loads(await resp.text(errors="ignore"))

        if not isinstance(data, dict):
            return []

        lines: list[str] = []
        if target_lang == "zh-CN" and src_hint == "en" and isinstance(data.get("ec"), dict):
            word_list = data["ec"].get("word") or []
            if word_list and isinstance(word_list[0], dict):
                entry = word_list[0]
                phones: list[str] = []
                if entry.get("ukphone"):
                    phones.append(f"英 /{entry['ukphone']}/")
                if entry.get("usphone"):
                    phones.append(f"美 /{entry['usphone']}/")
                if phones:
                    lines.append("  ".join(phones))

                for tr_item in (entry.get("trs") or [])[:6]:
                    inner = (tr_item.get("tr") or [{}])[0] if isinstance(tr_item, dict) else {}
                    vals = (inner.get("l") or {}).get("i") or []
                    text_val = " ".join(str(x) for x in vals if isinstance(x, str)).strip()
                    if text_val:
                        lines.append(f"• {_truncate_senses(text_val)}")

            phrs_block = (data.get("phrs") or {}).get("phrs") or []
            phrase_items: list[str] = []
            for p_item in phrs_block[:3]:
                phr = p_item.get("phr") if isinstance(p_item, dict) else None
                if not isinstance(phr, dict):
                    continue
                head = ((phr.get("headword") or {}).get("l") or {}).get("i")
                trs = phr.get("trs") or []
                meaning = ((trs[0].get("tr") or {}).get("l") or {}).get("i") if trs else None
                if isinstance(head, str) and isinstance(meaning, str) and head and meaning:
                    phrase_items.append(f"{head} ({_truncate_senses(meaning, max_senses=2, max_len=28)})")
            if phrase_items and len(lines) > 0:
                lines.append("常见搭配：" + "、".join(phrase_items))
            return lines if lines else []

        if target_lang == "zh-CN" and src_hint in {"ja", "fr"}:
            section_key = "jc" if src_hint == "ja" else "fc"
            sec = data.get(section_key)
            word_list = sec.get("word") or [] if isinstance(sec, dict) else []
            if word_list and isinstance(word_list[0], dict):
                entry = word_list[0]
                reading = ((entry.get("return-phrase") or {}).get("l") or {}).get("i")
                phone = entry.get("phone")
                header_parts: list[str] = []
                if isinstance(reading, str) and reading and reading != text:
                    sup = entry.get("phonesup") or ""
                    header_parts.append(f"{reading}{sup}")
                if isinstance(phone, str) and phone:
                    header_parts.append(f"/{phone}/")
                if header_parts:
                    lines.append("  ".join(header_parts))

                for tr_grp in (entry.get("trs") or [])[:6]:
                    if not isinstance(tr_grp, dict):
                        continue
                    pos = str(tr_grp.get("pos") or "").strip()
                    meanings: list[str] = []
                    for sub in tr_grp.get("tr") or []:
                        vals = ((sub.get("l") or {}).get("i") or []) if isinstance(sub, dict) else []
                        meanings.extend(str(v).strip() for v in vals if isinstance(v, str) and v.strip())
                    if meanings:
                        prefix = f"[{pos}] " if pos else ""
                        lines.append(f"• {prefix}{_truncate_senses('；'.join(meanings))}")
                return lines

        if target_lang == "en" and src_hint == "zh-CN" and isinstance(data.get("ce"), dict):
            word_list = data["ce"].get("word") or []
            if word_list and isinstance(word_list[0], dict):
                entry = word_list[0]
                if entry.get("phone"):
                    lines.append(f"/{entry['phone']}/")
                for tr_item in (entry.get("trs") or [])[:5]:
                    inner = (tr_item.get("tr") or [{}])[0] if isinstance(tr_item, dict) else {}
                    l_obj = inner.get("l") or {}
                    pos = str(l_obj.get("pos") or "").strip()
                    words = [
                        str(tok.get("#text")).strip()
                        for tok in (l_obj.get("i") or [])
                        if isinstance(tok, dict) and tok.get("#text")
                    ]
                    tran = str(l_obj.get("#tran") or "").strip()
                    if words:
                        prefix = f"{pos} " if pos else ""
                        gloss = f"（{_truncate_senses(tran, max_senses=2, max_len=30)}）" if tran else ""
                        lines.append(f"• {prefix}{', '.join(words)}{gloss}")
                return lines

        return []

    async def _fetch_bing_dict(
        self,
        text: str,
        src_hint: str,
        target_lang: str,
        config: dict[str, Any],
    ) -> list[str]:
        src = _map_lang(src_hint, _BING_LANG_MAP)
        tgt = _map_lang(target_lang, _BING_LANG_MAP)
        if not src or src == "auto-detect" or src == tgt:
            return []

        tok = await self._fetch_bing_token("https://cn.bing.com", config)
        req_url = (
            f"{tok['origin']}/tlookupv3?isVertical=1&IG={tok['ig']}"
            f"&IID={tok['iid']}&SFX={tok['sfx']}"
        )
        headers = {
            "User-Agent": DEFAULT_UA,
            "Origin": tok["origin"],
            "Referer": f"{tok['origin']}/translator",
            "Accept": "*/*",
        }
        form = {"from": src, "to": tgt, "text": text, "token": tok["token"], "key": tok["key"]}

        session = await self._get_session()
        async with session.post(
            req_url,
            data=form,
            headers=headers,
            proxy=self._request_proxy(req_url, config),
            timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            if resp.status != 200:
                return []
            data = json.loads(await resp.text(errors="ignore"))

        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            return []
        translations = data[0].get("translations") or []
        if not translations:
            return []

        grouped: dict[str, list[str]] = {}
        for item in translations:
            if not isinstance(item, dict):
                continue
            target_word = str(item.get("displayTarget") or item.get("normalizedTarget") or "").strip()
            if not target_word:
                continue
            raw_pos = str(item.get("posTag") or "OTHER").upper()
            pos_label = _BING_POS_LABELS.get(raw_pos, raw_pos.lower())
            bucket = grouped.setdefault(pos_label, [])
            if target_word not in bucket and len(bucket) < 6:
                bucket.append(target_word)

        return [f"• {pos} {'；'.join(words)}" for pos, words in list(grouped.items())[:6]]
