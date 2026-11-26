"""Standalone unit and integration tests for astrbot_plugin_translate (no AstrBot required)."""

from __future__ import annotations

import asyncio
import http.cookiejar
import importlib
import inspect
import json
import os
import pathlib
import sys
import tempfile
import types
import unittest
from unittest import mock
import urllib.error
import urllib.parse
import urllib.request

PKG_DIR = pathlib.Path(__file__).resolve().parent.parent
WORKSPACE_DIR = PKG_DIR.parent


def _ensure_stubs():
    """Register minimal aiohttp and astrbot SDK stubs when running standalone."""
    if "aiohttp" not in sys.modules:
        class ClientTimeout:
            def __init__(self, total=15):
                self.total = total

        aiohttp_mod = types.ModuleType("aiohttp")
        aiohttp_mod.ClientTimeout = ClientTimeout
        aiohttp_mod.ClientSession = object
        sys.modules["aiohttp"] = aiohttp_mod

    if "astrbot" in sys.modules:
        return

    astrbot_pkg = types.ModuleType("astrbot")
    astrbot_api = types.ModuleType("astrbot.api")
    astrbot_api_event = types.ModuleType("astrbot.api.event")
    astrbot_api_star = types.ModuleType("astrbot.api.star")
    astrbot_api_comp = types.ModuleType("astrbot.api.message_components")
    astrbot_core = types.ModuleType("astrbot.core")
    astrbot_core_star = types.ModuleType("astrbot.core.star")
    astrbot_core_filter = types.ModuleType("astrbot.core.star.filter")
    astrbot_core_cmd = types.ModuleType("astrbot.core.star.filter.command")

    class AstrBotConfig(dict):
        pass

    class FakeLogger:
        def info(self, *args, **kwargs):
            pass

        def warning(self, *args, **kwargs):
            pass

        def error(self, *args, **kwargs):
            pass

        def debug(self, *args, **kwargs):
            pass

    class GreedyStr(str):
        pass

    class Plain:
        def __init__(self, text: str = ""):
            self.text = text

    class Reply:
        def __init__(self, id: str = "1", message_str: str = "", chain=None, text: str = ""):
            self.id = id
            self.message_str = message_str
            self.chain = chain or []
            self.text = text

    class At:
        def __init__(self, qq: str = "0"):
            self.qq = qq

    class Image:
        def __init__(self, file: str = "", url: str = "", path: str = "", b64: str = ""):
            self.file = file
            self.url = url
            self.path = path
            self._b64 = b64

        async def convert_to_base64(self):
            if self._b64:
                return self._b64
            raise ValueError("no base64")

    class Record:
        def __init__(self, file: str = "", path: str = "", text: str | None = None):
            self.file = file
            self.path = path
            self.text = text

        @staticmethod
        def fromFileSystem(path: str, **kwargs):
            return Record(file=path, path=path, **kwargs)

    class File:
        def __init__(self, name: str, file: str = "", url: str = ""):
            self.name = name
            self.file = file
            self.url = url

    class Context:
        pass

    class Star:
        def __init__(self, context, config=None):
            self.context = context

    class AstrMessageEvent:
        def __init__(
            self,
            msg_str: str = "",
            messages=None,
            bot=None,
            umo: str = "umo:test",
            is_at_or_wake_command: bool = False,
        ):
            self._msg_str = msg_str
            self._messages = messages or []
            self.bot = bot
            self.unified_msg_origin = umo
            self.is_at_or_wake_command = is_at_or_wake_command
            self._temporary_local_files = []
            self._extras = {}

        def get_message_str(self):
            return self._msg_str

        def get_messages(self):
            return self._messages

        def set_extra(self, key, value):
            self._extras[key] = value

        def get_extra(self, key=None, default=None):
            if key is None:
                return self._extras
            return self._extras.get(key, default)

        def plain_result(self, text: str):
            return text

        def chain_result(self, chain):
            return types.SimpleNamespace(chain=chain)

        def track_temporary_local_file(self, path: str):
            self._temporary_local_files.append(path)

    class CustomFilter:
        def __init__(self, raise_error: bool = True, **kwargs):
            self.raise_error = raise_error

    _CustomFilterCls = CustomFilter

    class _Filter:
        CustomFilter = _CustomFilterCls

        @staticmethod
        def command(name, alias=None):
            def decorator(fn):
                fn._command_meta = (name, alias or set())
                return fn

            return decorator

        @staticmethod
        def custom_filter(cls, *args, **kwargs):
            def decorator(fn):
                fn._custom_filter = cls
                return fn

            return decorator

    astrbot_api.AstrBotConfig = AstrBotConfig
    astrbot_api.logger = FakeLogger()
    astrbot_api_event.AstrMessageEvent = AstrMessageEvent
    astrbot_api_event.filter = _Filter
    astrbot_api_star.Context = Context
    astrbot_api_star.Star = Star
    astrbot_api_comp.Plain = Plain
    astrbot_api_comp.Reply = Reply
    astrbot_api_comp.At = At
    astrbot_api_comp.Image = Image
    astrbot_api_comp.Record = Record
    astrbot_api_comp.File = File
    astrbot_core_cmd.GreedyStr = GreedyStr

    for name, mod in {
        "astrbot": astrbot_pkg,
        "astrbot.api": astrbot_api,
        "astrbot.api.event": astrbot_api_event,
        "astrbot.api.star": astrbot_api_star,
        "astrbot.api.message_components": astrbot_api_comp,
        "astrbot.core": astrbot_core,
        "astrbot.core.star": astrbot_core_star,
        "astrbot.core.star.filter": astrbot_core_filter,
        "astrbot.core.star.filter.command": astrbot_core_cmd,
    }.items():
        sys.modules[name] = mod


_ensure_stubs()
if str(WORKSPACE_DIR) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_DIR))

Comp = sys.modules["astrbot.api.message_components"]
AstrMessageEvent = sys.modules["astrbot.api.event"].AstrMessageEvent
GreedyStr = sys.modules["astrbot.core.star.filter.command"].GreedyStr

translator = importlib.import_module("astrbot_plugin_translate.translator")
plugin_main = importlib.import_module("astrbot_plugin_translate.main")


class MockResponse:
    def __init__(self, status: int, body, url: str = "https://cn.bing.com/translator"):
        self.status = status
        self._body = body
        self.url = url

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def text(self, errors="ignore"):
        if isinstance(self._body, str):
            return self._body
        if isinstance(self._body, (bytes, bytearray)):
            return bytes(self._body).decode("utf-8", errors=errors)
        return json.dumps(self._body, ensure_ascii=False)

    async def json(self, content_type=None):
        if isinstance(self._body, str):
            return json.loads(self._body)
        if isinstance(self._body, (bytes, bytearray)):
            return json.loads(bytes(self._body).decode("utf-8", errors="ignore"))
        return self._body

    async def read(self):
        if isinstance(self._body, (bytes, bytearray)):
            return bytes(self._body)
        if isinstance(self._body, str):
            return self._body.encode("utf-8")
        return json.dumps(self._body, ensure_ascii=False).encode("utf-8")


class MockSession:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []
        self.closed = False

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self.handler("GET", url, kwargs)

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.handler("POST", url, kwargs)

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.handler(method, url, kwargs)

    async def close(self):
        self.closed = True


class MetadataAndSchemaTests(unittest.TestCase):
    def test_schema_and_presets_alignment(self):
        schema = json.loads((PKG_DIR / "_conf_schema.json").read_text(encoding="utf-8"))
        allowed_types = {
            "int",
            "float",
            "bool",
            "string",
            "text",
            "list",
            "file",
            "object",
            "template_list",
            "dict",
        }

        def check_items(items, prefix=""):
            for key, meta in items.items():
                path = f"{prefix}.{key}" if prefix else key
                self.assertIn(meta.get("type"), allowed_types, path)
                if "options" in meta:
                    opts = meta["options"]
                    self.assertTrue(isinstance(opts, list) and opts, path)
                    if "labels" in meta:
                        self.assertEqual(len(meta["labels"]), len(opts), path)
                    if "subtitles" in meta:
                        self.assertLessEqual(len(meta["subtitles"]), len(opts), path)
                    if "default" in meta:
                        if meta["type"] == "string":
                            self.assertIn(meta["default"], opts, path)
                        elif meta["type"] == "list":
                            for val in meta["default"]:
                                self.assertIn(val, opts, path)
                if "condition" in meta:
                    for cond_key in meta["condition"]:
                        self.assertIn(cond_key, schema, f"{path} condition {cond_key}")
                if meta.get("type") == "object":
                    self.assertIsInstance(meta.get("items"), dict, path)
                    check_items(meta["items"], path)

        check_items(schema)

        for opt in schema["api_provider"]["options"]:
            self.assertIn(opt, translator.PROVIDER_PRESETS)
        for opt in schema["fallback_providers"]["options"]:
            self.assertIn(opt, translator.PROVIDER_PRESETS)

        # Channel name display must default to False, and dict lookup must default to True
        adv_items = schema["advanced"]["items"]
        self.assertIs(adv_items["show_engine_name"]["default"], False)
        self.assertIs(adv_items["enable_dict_lookup"]["default"], True)
        self.assertIs(adv_items["output_markdown"]["default"], False)

        # AI settings, wake settings, and voice pronunciation defaults
        ai_items = schema["ai_settings"]["items"]
        self.assertEqual(ai_items["ai_provider_id"]["default"], "")
        self.assertEqual(ai_items["ai_provider_id"].get("_special"), "select_provider")
        self.assertEqual(ai_items["ai_provider_manual"]["default"], "")
        self.assertIs(ai_items["enable_image_translation"]["default"], True)

        wake_items = schema["wake_settings"]["items"]
        self.assertIs(wake_items["enable_default_fy"]["default"], True)
        self.assertEqual(wake_items["custom_wake_words"]["default"], [])
        self.assertEqual(wake_items["custom_ai_wake_words"]["default"], [])

        voice_items = schema["voice_settings"]["items"]
        self.assertIs(voice_items["enable_voice"]["default"], True)
        self.assertIs(voice_items["auto_voice_for_words"]["default"], False)
        self.assertEqual(voice_items["tts_provider"]["default"], "auto")
        self.assertEqual(voice_items["voice_target_mode"]["default"], "smart")

    def test_metadata_yaml_required_fields(self):
        raw_lines = (PKG_DIR / "metadata.yaml").read_text(encoding="utf-8").splitlines()
        parsed = {}
        for line in raw_lines:
            self.assertNotIn("\t", line)
            if ":" in line and not line.startswith(" "):
                k, v = line.split(":", 1)
                parsed[k.strip()] = v.strip()
        for req in ("name", "display_name", "desc", "version", "author", "repo"):
            self.assertTrue(parsed.get(req), f"Missing {req}")
        self.assertTrue(parsed["name"].isidentifier())
        self.assertFalse(parsed["version"].startswith("v"))
        self.assertTrue(parsed["repo"].startswith("https://github.com/"))

    def test_command_signature_eval_str(self):
        sig = inspect.signature(plugin_main.TranslatePlugin.translate_command, eval_str=True)
        self.assertIs(sig.parameters["text"].annotation, GreedyStr)
        sig_ai = inspect.signature(plugin_main.TranslatePlugin.translate_ai_command, eval_str=True)
        self.assertIs(sig_ai.parameters["text"].annotation, GreedyStr)


class ParserAndHelperTests(unittest.TestCase):
    def test_parse_command_args(self):
        self.assertEqual(translator.parse_command_args("", has_reply=False).action, "help")
        self.assertEqual(translator.parse_command_args("", has_reply=True).action, "translate")
        self.assertEqual(translator.parse_command_args("--help").action, "help")
        self.assertEqual(translator.parse_command_args("-h").action, "help")
        self.assertEqual(translator.parse_command_args("--status").action, "status")
        self.assertEqual(translator.parse_command_args("--langs").action, "langs")

        p = translator.parse_command_args("help")
        self.assertEqual((p.action, p.text), ("translate", "help"))

        p = translator.parse_command_args("日语 你好世界")
        self.assertEqual((p.action, p.target_lang, p.text), ("translate", "ja", "你好世界"))

        p = translator.parse_command_args("英语 今天天气不错")
        self.assertEqual((p.action, p.target_lang, p.text), ("translate", "en", "今天天气不错"))

        p = translator.parse_command_args("en 你好世界")
        self.assertEqual((p.action, p.target_lang, p.text), ("translate", "en", "你好世界"))

        p = translator.parse_command_args("en route to work")
        self.assertEqual((p.action, p.target_lang, p.text), ("translate", None, "en route to work"))
        p = translator.parse_command_args("de la vida")
        self.assertEqual((p.action, p.target_lang, p.text), ("translate", None, "de la vida"))

        p = translator.parse_command_args("en", has_reply=True)
        self.assertEqual((p.action, p.target_lang, p.text), ("translate", "en", ""))

        p = translator.parse_command_args("->ja 你好\n世界")
        self.assertEqual((p.action, p.target_lang, p.text), ("translate", "ja", "你好\n世界"))
        p = translator.parse_command_args("en->zh Hello world")
        self.assertEqual(
            (p.action, p.source_lang, p.target_lang, p.text),
            ("translate", "en", "zh-CN", "Hello world"),
        )
        p = translator.parse_command_args("日语：早上好")
        self.assertEqual((p.action, p.target_lang, p.text), ("translate", "ja", "早上好"))
        p = translator.parse_command_args("-t ko 你好")
        self.assertEqual((p.action, p.target_lang, p.text), ("translate", "ko", "你好"))
        p = translator.parse_command_args("--to=zh-TW 你好")
        self.assertEqual((p.action, p.target_lang, p.text), ("translate", "zh-TW", "你好"))

        # Media & voice flags
        p = translator.parse_command_args("", has_media=True)
        self.assertEqual((p.action, p.target_lang, p.text, p.voice), ("translate", None, "", False))
        p = translator.parse_command_args("日语", has_media=True)
        self.assertEqual((p.action, p.target_lang, p.text, p.voice), ("translate", "ja", "", False))
        p = translator.parse_command_args("-v", has_media=True)
        self.assertEqual((p.action, p.target_lang, p.text, p.voice), ("translate", None, "", True))
        p = translator.parse_command_args("-v", has_reply=False, has_media=False)
        self.assertEqual(p.action, "help")

        p = translator.parse_command_args("-v apple")
        self.assertEqual((p.action, p.target_lang, p.text, p.voice), ("translate", None, "apple", True))
        p = translator.parse_command_args("apple --voice")
        self.assertEqual((p.action, p.target_lang, p.text, p.voice), ("translate", None, "apple", True))
        p = translator.parse_command_args("日语 -v 你好")
        self.assertEqual((p.action, p.target_lang, p.text, p.voice), ("translate", "ja", "你好", True))
        p = translator.parse_command_args("->ja 你好 --tts")
        self.assertEqual((p.action, p.target_lang, p.text, p.voice), ("translate", "ja", "你好", True))
        p = translator.parse_command_args("-v en route to work")
        self.assertEqual((p.action, p.target_lang, p.text, p.voice), ("translate", None, "en route to work", True))

        self.assertEqual(translator.parse_trigger_list("/tl, tr；!fy"), ["/tl", "tr", "!fy"])
        self.assertEqual(translator.parse_trigger_list([" /tl ", "tr", "/tl"]), ["/tl", "tr"])
        self.assertEqual(
            translator.resolve_ai_provider_choice(
                {"ai_settings": {"ai_provider_id": "sel-1", "ai_provider_manual": "manual-1"}}
            ),
            "manual-1",
        )
        self.assertEqual(
            translator.resolve_ai_provider_choice({"ai_settings": {"ai_provider_id": "sel-1"}}),
            "sel-1",
        )
        self.assertEqual(translator.resolve_ai_provider_choice({}), "")

    def test_is_word_or_phrase(self):
        self.assertTrue(translator.is_word_or_phrase("run"))
        self.assertTrue(translator.is_word_or_phrase("look up"))
        self.assertTrue(translator.is_word_or_phrase("well-known"))
        self.assertTrue(translator.is_word_or_phrase("state of the art"))
        self.assertTrue(translator.is_word_or_phrase("矛盾"))
        self.assertTrue(translator.is_word_or_phrase("食べる"))
        self.assertFalse(translator.is_word_or_phrase("Hello world. How are you?"))
        self.assertFalse(translator.is_word_or_phrase("line1\nline2"))
        self.assertFalse(translator.is_word_or_phrase("https://example.com"))
        self.assertFalse(translator.is_word_or_phrase("12345"))
        self.assertFalse(translator.is_word_or_phrase("今天天气真不错我们去散步吧"))

    def test_lang_detection_and_target_resolution(self):
        self.assertEqual(translator.resolve_target_lang("你好世界", "auto_zh_en"), "en")
        self.assertEqual(translator.resolve_target_lang("Hello world", "auto_zh_en"), "zh-CN")
        self.assertEqual(translator.resolve_target_lang("こんにちは", "auto_zh_en"), "zh-CN")
        self.assertEqual(translator.resolve_target_lang("안녕하세요", "auto_zh_en"), "zh-CN")
        self.assertEqual(translator.resolve_target_lang("帮我看看这个Error", "auto_zh_en"), "en")
        self.assertEqual(translator.resolve_target_lang("你好", "ja"), "ja")
        self.assertEqual(translator.detect_script_lang("〒9300322 富山県 中新川郡 上市町 栄町"), "ja")
        self.assertEqual(translator.detect_script_lang("〒1000001 東京都千代田区"), "ja")

        mixed_addr = (
            "〒9300322 富山県 中新川郡　上市町 栄町\n"
            "SAKAECHO,NAKANIIKAWA GUN KAMIICHI MACHI,TOYAMA KEN,〒9300322"
        )
        blocks = translator._split_mixed_script_blocks(mixed_addr)
        self.assertIsNotNone(blocks)
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0][0], "ja")
        self.assertEqual(blocks[1][0], "latin")
        self.assertIsNone(translator._split_mixed_script_blocks("Hello world\nHow are you?"))

        self.assertEqual(
            translator.clean_tts_text("**Hello** `world`\n- line 2", max_chars=50),
            "Hello world line 2",
        )
        self.assertEqual(
            translator.select_speech_target(
                "apple",
                translator.TranslationResult("苹果", "bing_cn", "Bing", "auto", "zh-CN", "en"),
                mode="smart",
            ),
            ("apple", "en"),
        )
        self.assertEqual(
            translator.select_speech_target(
                "苹果",
                translator.TranslationResult("apple", "bing_cn", "Bing", "auto", "en", "zh-CN"),
                mode="smart",
            ),
            ("apple", "en"),
        )
        self.assertEqual(
            translator.select_speech_target(
                "翻成英文",
                translator.TranslationResult("Hello menu", "astrbot_llm", "LLM", "image", "en", "image"),
                mode="smart",
            ),
            ("Hello menu", "en"),
        )

    def test_template_single_pass_and_secret_redaction(self):
        rendered = translator.render_json_template(
            '{"q": "{{text}}", "key": "{{api_key}}"}',
            {"text": 'hello {{api_key}} "world"\nline2', "api_key": "MY_SECRET"},
        )
        parsed = json.loads(rendered)
        self.assertEqual(parsed["q"], 'hello {{api_key}} "world"\nline2')
        self.assertEqual(parsed["key"], "MY_SECRET")

        url = translator.render_url_template(
            "https://example.com/tr?q={{text}}&key={{api_key}}",
            {"text": "hi {{api_key}}", "api_key": "SEC/123"},
        )
        self.assertEqual(url, "https://example.com/tr?q=hi%20%7B%7Bapi_key%7D%7D&key=SEC%2F123")
        self.assertEqual(
            translator.redact_config_secrets(
                "Error ak=AK1 sk=SK_SECRET&k=SEC%2F123",
                {"api_key": "AK1", "volcengine_secret_key": "SK_SECRET", "aliyun_access_secret": "SEC/123"},
            ),
            "Error ak=*** sk=***&k=***",
        )

    def test_clean_llm_translation_and_chunk_text(self):
        self.assertEqual(
            translator.clean_llm_translation("<think>reasoning here</think>\n你好世界"),
            "你好世界",
        )
        chunks = translator.chunk_text("line1\n\nline2\nline3", 8)
        self.assertEqual("".join(chunks), "line1\n\nline2\nline3")
        self.assertTrue(all(len(c) <= 8 for c in chunks))


class ServiceAndPluginMockTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_mocked_backends_and_dict_lookup(self):
        bing_calls = {"token": 0, "post": 0}

        def handler(method, url, kw):
            if "cn.bing.com/translator" in url:
                bing_calls["token"] += 1
                html_page = (
                    "<script>var params_AbusePreventionHelper = [12345,\"tok_abc\",3600000];"
                    "IG:\"ABCDEF1234567890ABCDEF\";</script>"
                    "<div data-iid=\"translator.5023\"></div>"
                )
                return MockResponse(200, html_page, url="https://cn.bing.com:8443/translator")
            if "cn.bing.com:8443/ttranslatev3" in url:
                bing_calls["post"] += 1
                if bing_calls["post"] == 1:
                    return MockResponse(200, {"statusCode": 401})
                txt = kw["data"]["text"]
                from_lang = kw["data"].get("fromLang", "auto-detect")
                if txt == "instant":
                    # Simulate real Bing behavior: cn.bing.com detects "instant" as "fr"
                    # even when fromLang="en" is explicitly sent.
                    text_out = "瞬间的" if from_lang == "auto-detect" else "瞬间"
                    det = "fr"
                elif txt == "gift":
                    if from_lang == "en":
                        text_out = "礼物"
                        det = "en"
                    else:
                        text_out = "毒药"
                        det = "de"
                elif txt == "bonjour":
                    text_out = "你好"
                    det = "fr"
                elif txt == "矛盾":
                    text_out = f"BING:{txt}"
                    det = "zh-Hans"
                else:
                    text_out = f"BING:{txt}"
                    det = "en"
                return MockResponse(
                    200,
                    [{"translations": [{"text": text_out}], "detectedLanguage": {"language": det}}],
                )
            if "cn.bing.com:8443/tlookupv3" in url:
                return MockResponse(
                    200,
                    [
                        {
                            "translations": [
                                {"posTag": "NOUN", "displayTarget": "contradiction"},
                                {"posTag": "NOUN", "displayTarget": "conflict"},
                                {"posTag": "ADJ", "displayTarget": "contradictory"},
                                {"posTag": "VERB", "displayTarget": "contradict"},
                            ]
                        }
                    ],
                )
            if "dict.youdao.com/jsonapi" in url:
                q = kw.get("params", {}).get("q", "")
                if q == "instant":
                    return MockResponse(
                        200,
                        {
                            "ec": {
                                "exam_type": ["CET4", "CET6", "IELTS"],
                                "word": [
                                    {
                                        "ukphone": "ˈɪnstənt",
                                        "usphone": "ˈɪnstənt",
                                        "trs": [
                                            {"tr": [{"l": {"i": ["adj. 立即的；紧迫的；速溶的"]}}]},
                                            {"tr": [{"l": {"i": ["n. 瞬间，片刻；咖啡"]}}]},
                                        ],
                                    }
                                ],
                            },
                            "fc": {
                                "word": [
                                    {
                                        "phone": "ɛ̃stɑ̃, ɑ̃:t",
                                        "trs": [{"tr": [{"l": {"i": ["[m.] 瞬间，片刻"]}}]}],
                                    }
                                ]
                            },
                        },
                    )
                if q == "gift":
                    return MockResponse(
                        200,
                        {
                            "ec": {
                                "exam_type": ["CET4"],
                                "word": [
                                    {
                                        "ukphone": "ɡɪft",
                                        "usphone": "ɡɪft",
                                        "trs": [
                                            {"tr": [{"l": {"i": ["n. 礼物，赠品；天赋"]}}]},
                                            {"tr": [{"l": {"i": ["v. 赠送；赋予"]}}]},
                                        ],
                                    }
                                ],
                            }
                        },
                    )
                if q == "bonjour":
                    return MockResponse(
                        200,
                        {
                            "ec": {
                                "word": [
                                    {
                                        "trs": [
                                            {"tr": [{"l": {"i": ["int. （法）你好，早安"]}}]},
                                        ]
                                    }
                                ]
                            },
                            "fc": {
                                "word": [
                                    {
                                        "phone": "bɔ̃ʒu:r",
                                        "trs": [
                                            {"tr": [{"l": {"i": ["[法语] 你好，早安"]}}]},
                                        ],
                                    }
                                ]
                            },
                        },
                    )
                return MockResponse(
                    200,
                    {
                        "ec": {
                            "word": [
                                {
                                    "ukphone": "rʌn",
                                    "usphone": "rʌn",
                                    "trs": [
                                        {"tr": [{"l": {"i": ["v. 跑，奔跑；运行；经营"]}}]},
                                        {"tr": [{"l": {"i": ["n. 跑步；赛跑；旅程"]}}]},
                                    ],
                                }
                            ]
                        },
                        "phrs": {
                            "phrs": [
                                {
                                    "phr": {
                                        "headword": {"l": {"i": "in the long run"}},
                                        "trs": [{"tr": {"l": {"i": "长远；终究"}}}],
                                    }
                                }
                            ]
                        },
                    },
                )
            if "translate.googleapis.com" in url:
                q = kw["params"]["q"]
                return MockResponse(200, [[[f"GTX:{q}", q]], None, "en"])
            if "clients5.google.com" in url:
                return MockResponse(200, [["你好", "en"], ["世界", "en"]])
            if "lingva.ml" in url:
                return MockResponse(200, {"translation": "你好世界", "info": {"detectedSource": "en"}})
            if "api.niutrans.com" in url:
                body = kw["json"]
                self.assertEqual(body["apikey"], "NIU_KEY")
                return MockResponse(200, {"from": "en", "to": "zh", "tgt_text": "小牛译文"})
            if "translate.volcengineapi.com" in url:
                self.assertIn("HMAC-SHA256 Credential=VOLC_AK/", kw["headers"]["Authorization"])
                self.assertIn("X-Date", kw["headers"])
                self.assertIn("X-Content-Sha256", kw["headers"])
                body = json.loads(kw["data"].decode("utf-8"))
                self.assertEqual(body["TargetLanguage"], "zh")
                return MockResponse(
                    200,
                    {"TranslationList": [{"Translation": "火山译文", "DetectedSourceLanguage": "en"}]},
                )
            if "fanyi-api.baidu.com" in url:
                self.assertEqual(kw["data"]["appid"], "BD_APP")
                self.assertEqual(kw["data"]["to"], "jp")
                self.assertEqual(len(kw["data"]["sign"]), 32)
                return MockResponse(
                    200,
                    {"from": "zh", "to": "jp", "trans_result": [{"dst": "百度訳文"}]},
                )
            if "mt.cn-hangzhou.aliyuncs.com" in url:
                self.assertEqual(kw["data"]["Action"], "TranslateGeneral")
                self.assertEqual(kw["data"]["Version"], "2018-10-12")
                self.assertTrue(kw["data"]["Signature"])
                return MockResponse(200, {"Code": "200", "Data": {"Translated": "阿里译文", "DetectedLanguage": "en"}})
            if "open.bigmodel.cn" in url:
                self.assertEqual(kw["json"]["model"], "GLM-4-Flash-250414")
                sys_prompt = kw["json"]["messages"][0]["content"]
                tag = "DICT" if "bilingual dictionary" in sys_prompt else "TEXT"
                return MockResponse(200, {"choices": [{"message": {"content": f"{tag}:智谱译文"}}]})
            if "api.siliconflow.cn" in url:
                self.assertEqual(kw["json"]["model"], "tencent/Hunyuan-MT-7B")
                return MockResponse(200, {"choices": [{"message": {"content": "硅基译文"}}]})
            if "api.groq.com" in url:
                self.assertEqual(kw["json"]["model"], "openai/gpt-oss-120b")
                return MockResponse(200, {"choices": [{"message": {"content": "Groq译文"}}]})
            if "libretranslate.com" in url:
                return MockResponse(502, "<html>502 Bad Gateway</html>")
            if "lt.local" in url:
                return MockResponse(200, {"translatedText": "你好LT", "detectedLanguage": {"language": "en"}})
            if "127.0.0.1:1188" in url:
                return MockResponse(200, {"code": 200, "data": "你好DeepLX", "source_lang": "EN"})
            if "mozhi.example" in url:
                self.assertEqual(kw["params"]["engine"], "google")
                self.assertEqual(kw["params"]["to"], "zh-CN")
                return MockResponse(200, {"translated-text": "你好Mozhi", "detected": "en"})
            if "open.bigmodel.test" in url:
                self.assertEqual(url, "https://open.bigmodel.test/api/paas/v4/chat/completions")
                return MockResponse(
                    200,
                    {"choices": [{"message": {"content": [{"type": "text", "text": "<think>hmm</think>\n你好Compat"}]}}]},
                )
            if "custom.example.com" in url:
                body = json.loads(kw["data"].decode("utf-8"))
                self.assertEqual(body["q"], "hello {{api_key}}")
                return MockResponse(200, {"data": {"result": "定制译文"}})
            if "api.mymemory.translated.net" in url:
                return MockResponse(429, {"responseStatus": 429})
            if "m.youdao.com" in url:
                return MockResponse(200, '<ul id="translateResult"><li>回退成功</li></ul>')
            return MockResponse(500, "err")

        sess = MockSession(handler)
        svc = translator.TranslationService(lambda: asyncio.sleep(0, result=sess))

        # Bing sentence (no dict entries) vs word (Youdao dict entries) vs zh->en word (Bing dict entries)
        res_sent = await svc.translate(
            "Hello world, this is a full sentence.",
            {"api_provider": "bing_cn", "enable_auto_fallback": False},
        )
        self.assertEqual(res_sent.translated_text, "BING:Hello world, this is a full sentence.")
        self.assertIsNone(res_sent.dict_entries)
        self.assertEqual(bing_calls, {"token": 2, "post": 2})

        res_word = await svc.translate(
            "run",
            {"api_provider": "bing_cn", "enable_auto_fallback": False},
        )
        self.assertEqual(res_word.translated_text, "BING:run")
        self.assertIsNotNone(res_word.dict_entries)
        joined_dict = "\n".join(res_word.dict_entries or [])
        self.assertIn("英 /rʌn/", joined_dict)
        self.assertIn("v. 跑，奔跑", joined_dict)
        self.assertIn("n. 跑步", joined_dict)
        self.assertIn("in the long run", joined_dict)

        res_zh_word = await svc.translate(
            "矛盾",
            {"api_provider": "bing_cn", "enable_auto_fallback": False},
        )
        self.assertIsNotNone(res_zh_word.dict_entries)
        joined_zh_dict = "\n".join(res_zh_word.dict_entries or [])
        self.assertIn("n. contradiction；conflict", joined_zh_dict)
        self.assertIn("adj. contradictory", joined_zh_dict)
        self.assertIn("v. contradict", joined_zh_dict)

        # Ambiguous ASCII English word misdetected as French by Bing ("instant")
        res_instant = await svc.translate(
            "instant",
            {"api_provider": "bing_cn", "enable_auto_fallback": False},
        )
        self.assertEqual(res_instant.detected_lang, "en")
        self.assertEqual(res_instant.translated_text, "瞬间")
        joined_instant_dict = "\n".join(res_instant.dict_entries or [])
        self.assertIn("英 /ˈɪnstənt/", joined_instant_dict)
        self.assertNotIn("ɛ̃stɑ̃", joined_instant_dict)

        # False friend misdetected as German ("gift" -> "毒药") must re-translate as English ("礼物")
        res_gift = await svc.translate(
            "gift",
            {"api_provider": "bing_cn", "enable_auto_fallback": False},
        )
        self.assertEqual(res_gift.detected_lang, "en")
        self.assertEqual(res_gift.translated_text, "礼物")

        # Real French loanword ("bonjour") must remain detected as French
        res_bonjour = await svc.translate(
            "bonjour",
            {"api_provider": "bing_cn", "enable_auto_fallback": False},
        )
        self.assertEqual(res_bonjour.detected_lang, "fr")
        self.assertEqual(res_bonjour.translated_text, "你好")
        joined_bonjour_dict = "\n".join(res_bonjour.dict_entries or [])
        self.assertIn("bɔ̃ʒu:r", joined_bonjour_dict)

        # New LLM presets + dictionary mode vs sentence mode
        self.assertEqual(
            (await svc.translate("run", {"api_provider": "zhipu_free", "api_key": "ZK", "enable_auto_fallback": False})).translated_text,
            "DICT:智谱译文",
        )
        self.assertEqual(
            (await svc.translate("This is a complete sentence.", {"api_provider": "zhipu_free", "api_key": "ZK", "enable_auto_fallback": False})).translated_text,
            "TEXT:智谱译文",
        )
        self.assertEqual(
            (await svc.translate("Hello", {"api_provider": "siliconflow_free", "api_key": "SK", "enable_auto_fallback": False})).translated_text,
            "硅基译文",
        )
        self.assertEqual(
            (await svc.translate("Hello", {"api_provider": "groq_free", "api_key": "GK", "enable_auto_fallback": False})).translated_text,
            "Groq译文",
        )

        # New signed/keyed MT backends (pass sentence to isolate translation call without dict call)
        sentence = "Hello world, testing multiple providers."
        self.assertEqual(
            (await svc.translate(sentence, {"api_provider": "niutrans", "api_key": "NIU_KEY", "enable_auto_fallback": False})).translated_text,
            "小牛译文",
        )
        self.assertEqual(
            (await svc.translate(sentence, {"api_provider": "volcengine", "api_key": "VOLC_AK", "volcengine_secret_key": "VOLC_SK", "enable_auto_fallback": False})).translated_text,
            "火山译文",
        )
        self.assertEqual(
            (await svc.translate("今天天气很好，出去走走。", {"api_provider": "baidu_fanyi", "api_key": "BD_SK", "baidu_app_id": "BD_APP", "enable_auto_fallback": False}, target_lang="ja")).translated_text,
            "百度訳文",
        )
        self.assertEqual(
            (await svc.translate(sentence, {"api_provider": "aliyun_mt", "api_key": "ALI_AK", "aliyun_access_secret": "ALI_SK", "enable_auto_fallback": False})).translated_text,
            "阿里译文",
        )

        # GTX multi-chunk preserves blank lines across chunks
        gtx_text = ("A" * 800) + "\n\n" + ("B" * 800)
        res = await svc.translate(gtx_text, {"api_provider": "google_free", "enable_auto_fallback": False})
        self.assertIn("\n\n", res.translated_text)

        # Clients5, Lingva, DeepLX, Mozhi(Google), OpenAI Compat, Custom HTTP
        self.assertEqual(
            (await svc.translate(sentence, {"api_provider": "google_clients5", "enable_auto_fallback": False})).translated_text,
            "你好世界",
        )
        self.assertEqual(
            (await svc.translate(sentence, {"api_provider": "lingva_ml", "enable_auto_fallback": False})).translated_text,
            "你好世界",
        )
        self.assertEqual(
            (await svc.translate(sentence, {"api_provider": "deeplx", "api_key": "K", "enable_auto_fallback": False})).translated_text,
            "你好DeepLX",
        )
        self.assertEqual(
            (
                await svc.translate(
                    sentence,
                    {
                        "api_provider": "mozhi_adminforge",
                        "custom_api_url": "https://mozhi.example",
                        "advanced": {"mozhi_engine": "google"},
                        "enable_auto_fallback": False,
                    },
                )
            ).translated_text,
            "你好Mozhi",
        )
        self.assertEqual(
            (
                await svc.translate(
                    sentence,
                    {
                        "api_provider": "openai_compat",
                        "custom_api_url": "https://open.bigmodel.test/api/paas/v4",
                        "api_key": "ZKEY",
                        "openai_system_prompt": 'To {target_lang} {"ok": true}',
                        "enable_auto_fallback": False,
                    },
                )
            ).translated_text,
            "你好Compat",
        )
        self.assertEqual(
            (
                await svc.translate(
                    "hello {{api_key}}",
                    {
                        "api_provider": "custom_http",
                        "custom_api_url": "https://custom.example.com/tr",
                        "api_key": "SECRET_TOK",
                        "custom_http_method": "POST",
                        "custom_http_headers": '{"Authorization": "Bearer {{api_key}}"}',
                        "custom_http_body": '{"q": "{{text}}", "target": "{{target}}"}',
                        "custom_http_response_path": "data.result",
                        "enable_auto_fallback": False,
                    },
                )
            ).translated_text,
            "定制译文",
        )

        # LibreTranslate non-JSON 502 error handling
        with self.assertRaises(translator.TranslationError) as ctx:
            await svc.translate(sentence, {"api_provider": "libretranslate", "enable_auto_fallback": False})
        self.assertIn("HTTP 502", str(ctx.exception))

        self.assertEqual(
            (
                await svc.translate(
                    sentence,
                    {
                        "api_provider": "libretranslate",
                        "custom_api_url": "https://lt.local",
                        "enable_auto_fallback": False,
                    },
                )
            ).translated_text,
            "你好LT",
        )

        # Fallback: mymemory 429 -> youdao_web
        fb = await svc.translate(
            sentence,
            {
                "api_provider": "mymemory",
                "enable_auto_fallback": True,
                "fallback_providers": ["youdao_web"],
            },
        )
        self.assertTrue(fb.fallback_used)
        self.assertEqual(fb.provider_key, "youdao_web")
        self.assertEqual(fb.translated_text, "回退成功")

    async def test_translate_plugin_hides_channel_and_formats_dict(self):
        class ModernContext:
            def get_config(self, umo=None):
                return {"wake_prefix": ["/"]}

            async def get_current_chat_provider_id(self, umo=None):
                return "prov-1"

            async def llm_generate(self, chat_provider_id, prompt, system_prompt):
                mode = "DICT" if "bilingual dictionary" in system_prompt else "TEXT"
                return types.SimpleNamespace(
                    role="assistant",
                    completion_text=f"<think>plan</think>{mode}:{prompt}",
                )

        # Default config: show_engine_name omitted -> must NOT display provider name
        plugin = plugin_main.TranslatePlugin(
            ModernContext(),
            {
                "api_provider": "astrbot_llm",
                "default_target_lang": "auto_zh_en",
                "enable_auto_fallback": False,
                "advanced": {
                    "show_detected_lang": True,
                    "show_original": False,
                    "max_text_length": 200,
                },
            },
        )

        out_word = [
            x
            async for x in plugin.translate_command(
                AstrMessageEvent("fy run", [Comp.Plain("/fy run")]),
                GreedyStr("run"),
            )
        ]
        self.assertIn("DICT:run", out_word[0])
        self.assertNotIn("AstrBot", out_word[0])

        # Non-LLM _format_output with dict_entries hides channel name
        formatted = plugin._format_output(
            "run",
            translator.TranslationResult(
                translated_text="跑",
                provider_key="bing_cn",
                provider_name="Bing 翻译 (国内版)",
                source_lang="auto",
                target_lang="zh-CN",
                detected_lang="en",
                dict_entries=["英 /rʌn/", "• v. 跑，奔跑；运行", "• n. 跑步；旅程"],
            ),
        )
        self.assertIn("[英语 → 简体中文]", formatted)
        self.assertIn("词典释义：", formatted)
        self.assertIn("• v. 跑，奔跑；运行", formatted)
        self.assertNotIn("Bing", formatted)

        # Explicit source_lang="en" overrides Bing's erroneous detected_lang="fr" in header
        formatted_explicit_en = plugin._format_output(
            "instant",
            translator.TranslationResult(
                translated_text="瞬间",
                provider_key="bing_cn",
                provider_name="Bing 翻译 (国内版)",
                source_lang="en",
                target_lang="zh-CN",
                detected_lang="fr",
                dict_entries=["英 /ˈɪnstənt/", "• adj. 立即的"],
            ),
        )
        self.assertIn("[英语 → 简体中文]", formatted_explicit_en)
        self.assertNotIn("法语", formatted_explicit_en)

        # Markdown mode formatting and MessageEventResult.use_markdown(True/False)
        class RichResult(str):
            def __new__(cls, text):
                obj = super().__new__(cls, text)
                obj.use_markdown_ = None
                return obj

            def use_markdown(self, flag=True):
                self.use_markdown_ = flag
                return self

        class RichEvent(AstrMessageEvent):
            def plain_result(self, text: str):
                return RichResult(text)

        md_plugin = plugin_main.TranslatePlugin(
            ModernContext(),
            {
                "api_provider": "astrbot_llm",
                "default_target_lang": "auto_zh_en",
                "enable_auto_fallback": False,
                "advanced": {
                    "output_markdown": True,
                    "show_detected_lang": True,
                    "show_original": True,
                },
            },
        )
        md_formatted = md_plugin._format_output(
            "run",
            translator.TranslationResult(
                translated_text="跑",
                provider_key="bing_cn",
                provider_name="Bing 翻译 (国内版)",
                source_lang="auto",
                target_lang="zh-CN",
                detected_lang="en",
                dict_entries=[
                    "英 /rʌn/  美 /rʌn/",
                    "• v. 跑，奔跑；运行",
                    "常见搭配：in the long run (长远；终究)",
                ],
            ),
        )
        self.assertIn("**[英语 → 简体中文]**", md_formatted)
        self.assertIn("**词典释义**", md_formatted)
        self.assertIn("- **发音**：英 `/rʌn/`  美 `/rʌn/`", md_formatted)
        self.assertIn("- **v.** 跑，奔跑；运行", md_formatted)
        self.assertIn("- **常见搭配**：`in the long run` (长远；终究)", md_formatted)
        self.assertNotIn("Bing", md_formatted)

        md_out = [
            x
            async for x in md_plugin.translate_command(
                RichEvent("fy run", [Comp.Plain("/fy run")]),
                GreedyStr("run"),
            )
        ]
        self.assertIs(md_out[0].use_markdown_, True)

        plain_out = [
            x
            async for x in plugin.translate_command(
                RichEvent("fy run", [Comp.Plain("/fy run")]),
                GreedyStr("run"),
            )
        ]
        self.assertIs(plain_out[0].use_markdown_, False)

        md_status = [
            x
            async for x in md_plugin.translate_command(
                RichEvent("fy --status", [Comp.Plain("/fy --status")]),
                GreedyStr("--status"),
            )
        ]
        self.assertIn("**输出格式**：Markdown", md_status[0])
        await md_plugin.terminate()

        # Help text also hides channel name
        out_help = [
            x
            async for x in plugin.translate_command(
                AstrMessageEvent("fy", [Comp.Plain("/fy")]),
                GreedyStr(""),
            )
        ]
        self.assertNotIn("AstrBot 内置 LLM", out_help[0])

        # /fy 翻译 recovers literal argument "翻译"
        out1 = [
            x
            async for x in plugin.translate_command(
                AstrMessageEvent("fy", [Comp.Plain("/fy 翻译")]),
                GreedyStr(""),
            )
        ]
        self.assertIn("DICT:翻译", out1[0])

        # Quote extraction via OneBot bot.api.call_action("get_msg")
        class FakeAPI:
            async def call_action(self, action, **kwargs):
                return {"data": {"message": [{"type": "text", "data": {"text": "This is a quoted sentence."}}]}}

        ev2 = AstrMessageEvent(
            "fy en",
            [Comp.Reply(id="99"), Comp.At("123"), Comp.Plain(" /fy en")],
            bot=types.SimpleNamespace(api=FakeAPI()),
        )
        out2 = [x async for x in plugin.translate_command(ev2, GreedyStr("en"))]
        self.assertIn("TEXT:This is a quoted sentence.", out2[0])
        self.assertIn("英语", out2[0])

        # --langs, --status
        out_langs = [
            x
            async for x in plugin.translate_command(
                AstrMessageEvent("fy --langs", [Comp.Plain("/fy --langs")]),
                GreedyStr("--langs"),
            )
        ]
        self.assertIn("简体中文 (zh-CN)", out_langs[0])

        out_status = [
            x
            async for x in plugin.translate_command(
                AstrMessageEvent("翻译 --status", [Comp.Plain("/翻译 --status")]),
                GreedyStr("--status"),
            )
        ]
        self.assertIn("AstrBot 当前内置大模型", out_status[0])
        self.assertIn("默认目标：智能中英互译", out_status[0])

        await plugin.terminate()

    async def test_mixed_script_block_translation(self):
        seen_calls = []

        def handler(method, url, kw):
            if "cn.bing.com/translator" in url:
                html_page = (
                    "<script>var params_AbusePreventionHelper = [12345,\"tok_abc\",3600000];"
                    "IG:\"ABCDEF1234567890ABCDEF\";</script>"
                    "<div data-iid=\"translator.5023\"></div>"
                )
                return MockResponse(200, html_page)
            if "ttranslatev3" in url:
                from_lang = kw["data"].get("fromLang", "")
                txt = kw["data"].get("text", "")
                seen_calls.append((from_lang, txt))
                if from_lang == "ja":
                    return MockResponse(200, [{"detectedLanguage": {"language": "ja"}, "translations": [{"text": "〒9300322 富山县中新川郡上市町荣町"}]}])
                return MockResponse(200, [{"detectedLanguage": {"language": "en"}, "translations": [{"text": "日本富山县上市町中新川郡荣町，邮编9300322"}]}])
            return MockResponse(404, {})

        sess = MockSession(handler)
        svc = translator.TranslationService(lambda: asyncio.sleep(0, result=sess))
        sample = (
            "〒9300322 富山県 中新川郡　上市町 栄町\n"
            "SAKAECHO,NAKANIIKAWA GUN KAMIICHI MACHI,TOYAMA KEN,〒9300322"
        )
        res = await svc.translate(
            text=sample,
            config={"api_provider": "bing_cn", "enable_auto_fallback": False},
        )
        self.assertEqual(len(seen_calls), 2)
        self.assertEqual(seen_calls[0][0], "ja")
        self.assertEqual(seen_calls[1][0], "auto-detect")
        self.assertEqual(res.detected_lang, "ja+en")
        self.assertIn("日本富山县上市町中新川郡荣町", res.translated_text)
        self.assertEqual(translator.get_lang_label(res.detected_lang), "日语、英语")

    async def test_image_translation_and_plugin_flow(self):
        recorded_llm_calls = []

        class VisionContext:
            def get_config(self, umo=None):
                return {"wake_prefix": ["/"]}

            async def get_current_chat_provider_id(self, umo=None):
                return "vision-prov"

            async def llm_generate(self, chat_provider_id, prompt, system_prompt, image_urls=None):
                recorded_llm_calls.append(
                    {
                        "provider_id": chat_provider_id,
                        "prompt": prompt,
                        "system_prompt": system_prompt,
                        "image_urls": image_urls,
                    }
                )
                return types.SimpleNamespace(
                    role="assistant",
                    completion_text="<think>ocr</think>拉面菜单：叉烧面 980日元",
                )

        plugin = plugin_main.TranslatePlugin(
            VisionContext(),
            {
                "api_provider": "bing_cn",
                "default_target_lang": "auto_zh_en",
                "enable_auto_fallback": False,
                "advanced": {"show_original": True},
            },
        )

        # Direct image attachment + target language
        img = Comp.Image(b64="iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ")
        ev_img = AstrMessageEvent("fy 日语", [Comp.Plain("/fy 日语"), img])
        out_img = [x async for x in plugin.translate_command(ev_img, GreedyStr("日语"))]
        self.assertEqual(len(out_img), 1)
        self.assertIn("[图片识别 → 日语]", out_img[0])
        self.assertIn("拉面菜单：叉烧面 980日元", out_img[0])
        self.assertNotIn("原文：", out_img[0])
        self.assertEqual(recorded_llm_calls[-1]["image_urls"], ["base64://iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"])

        # Quoted image via OneBot get_msg fallback
        class FakeImgAPI:
            async def call_action(self, action, **kwargs):
                return {
                    "data": {
                        "message": [
                            {"type": "image", "data": {"url": "https://example.com/sign.png"}}
                        ]
                    }
                }

        ev_quote_img = AstrMessageEvent(
            "fy",
            [Comp.Reply(id="88"), Comp.Plain("/fy")],
            bot=types.SimpleNamespace(api=FakeImgAPI()),
        )
        out_quote = [x async for x in plugin.translate_command(ev_quote_img, GreedyStr(""))]
        self.assertIn("[图片识别 → 智能中英互译]", out_quote[0])
        self.assertEqual(recorded_llm_calls[-1]["image_urls"], ["https://example.com/sign.png"])

        # OpenAI-compatible vision endpoint + auto fallback when OpenAI fails
        openai_requests = []

        def openai_handler(method, url, kw):
            openai_requests.append(kw["json"])
            if len(openai_requests) == 1:
                return MockResponse(
                    200,
                    {"choices": [{"message": {"content": "OpenAI识图译文"}}]},
                )
            return MockResponse(400, {"error": {"message": "not a VLM"}})

        plugin_oai = plugin_main.TranslatePlugin(
            VisionContext(),
            {
                "api_provider": "openai_compat",
                "custom_api_url": "https://api.example.com/v1/chat/completions",
                "api_key": "SK_TEST",
                "openai_model": "gpt-4o-mini",
                "image_translation": {"image_llm_source": "auto"},
            },
        )
        plugin_oai._service = translator.TranslationService(
            lambda: asyncio.sleep(0, result=MockSession(openai_handler))
        )
        ev_oai = AstrMessageEvent("fy", [Comp.Plain("/fy"), img])
        out_oai = [x async for x in plugin_oai.translate_command(ev_oai, GreedyStr(""))]
        self.assertIn("OpenAI识图译文", out_oai[0])
        user_content = openai_requests[0]["messages"][1]["content"]
        self.assertTrue(user_content[1]["image_url"]["url"].startswith("data:image/png;base64,"))

        # Second call fails OpenAI vision -> auto falls back to AstrBot built-in multimodal LLM
        out_oai_fb = [x async for x in plugin_oai.translate_command(ev_oai, GreedyStr(""))]
        self.assertIn("拉面菜单：叉烧面 980日元", out_oai_fb[0])

        # New ai_settings: manual "openai_compat" forces OpenAI-compat vision even if api_provider is bing_cn
        plugin_ai_img = plugin_main.TranslatePlugin(
            VisionContext(),
            {
                "api_provider": "bing_cn",
                "custom_api_url": "https://api.example.com/v1/chat/completions",
                "api_key": "SK_TEST",
                "openai_model": "gpt-4o-mini",
                "ai_settings": {"ai_provider_manual": "openai_compat"},
            },
        )
        plugin_ai_img._service = translator.TranslationService(
            lambda: asyncio.sleep(0, result=MockSession(lambda m, u, kw: MockResponse(200, {"choices": [{"message": {"content": "AI设置识图"}}]})))
        )
        out_ai_img = [x async for x in plugin_ai_img.translate_command(ev_oai, GreedyStr(""))]
        self.assertIn("AI设置识图", out_ai_img[0])
        await plugin_ai_img.terminate()

        await plugin.terminate()
        await plugin_oai.terminate()

    async def test_voice_pronunciation_and_tts_fallback(self):
        fake_mp3 = b"\xff\xf3\x84\xc4" + (b"\x00" * 256)
        fake_wav = b"RIFF" + (b"\x00" * 4) + b"WAVE" + (b"\x00" * 256)
        tts_calls = []

        def tts_handler(method, url, kw):
            tts_calls.append(url)
            if "cn.bing.com/translator" in url:
                html_page = (
                    "<script>var params_AbusePreventionHelper = [12345,\"tok_abc\",3600000];"
                    "IG:\"ABCDEF1234567890ABCDEF\";</script>"
                )
                return MockResponse(200, html_page)
            if "ttranslatev3" in url:
                return MockResponse(
                    200,
                    [{"detectedLanguage": {"language": "en"}, "translations": [{"text": "苹果"}]}],
                )
            if "tfettts" in url:
                return MockResponse(500, b"")
            if "dict.youdao.com/dictvoice" in url:
                return MockResponse(200, fake_mp3)
            if "sug.so.360.cn" in url or "dict.youdao.com/jsonapi" in url:
                return MockResponse(200, {})
            return MockResponse(404, {})

        class NoopContext:
            def get_config(self, umo=None):
                return {"wake_prefix": ["/"]}

        sess = MockSession(tts_handler)
        plugin = plugin_main.TranslatePlugin(
            NoopContext(),
            {
                "api_provider": "bing_cn",
                "enable_auto_fallback": True,
                "voice_settings": {
                    "enable_voice": True,
                    "tts_provider": "auto",
                    "voice_target_mode": "smart",
                },
            },
        )
        plugin._service = translator.TranslationService(lambda: asyncio.sleep(0, result=sess))

        # 1. With ffmpeg available -> sends Comp.Record after text translation
        ev_rec = AstrMessageEvent("fy -v apple", [Comp.Plain("/fy -v apple")])
        with mock.patch("shutil.which", return_value="/usr/bin/ffmpeg"):
            out_rec = [x async for x in plugin.translate_command(ev_rec, GreedyStr("-v apple"))]
        self.assertEqual(len(out_rec), 2)
        self.assertIn("苹果", out_rec[0])
        self.assertIsInstance(out_rec[1].chain[0], Comp.Record)
        self.assertEqual(out_rec[1].chain[0].text, "apple")
        self.assertEqual(len(ev_rec._temporary_local_files), 1)
        self.assertTrue(os.path.exists(ev_rec._temporary_local_files[0]))
        os.remove(ev_rec._temporary_local_files[0])
        self.assertTrue(any("tfettts" in u for u in tts_calls))
        self.assertTrue(any("dictvoice" in u for u in tts_calls))

        # 2. Without ffmpeg -> MP3 gracefully falls back to Comp.File
        ev_file = AstrMessageEvent("fy -v apple", [Comp.Plain("/fy -v apple")])
        with mock.patch("shutil.which", return_value=None):
            out_file = [x async for x in plugin.translate_command(ev_file, GreedyStr("-v apple"))]
        self.assertEqual(len(out_file), 2)
        self.assertIsInstance(out_file[1].chain[0], Comp.File)
        self.assertEqual(out_file[1].chain[0].name, "pronunciation_en.mp3")
        os.remove(ev_file._temporary_local_files[0])

        # 3. WAV audio from AstrBot built-in TTS sends Comp.Record even without ffmpeg
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
            tf.write(fake_wav)
            wav_path = tf.name
        try:
            class TtsContext(NoopContext):
                async def get_using_tts_provider_async(self, umo=None):
                    return types.SimpleNamespace(
                        get_audio=lambda txt: asyncio.sleep(0, result=wav_path)
                    )

            plugin_astr_tts = plugin_main.TranslatePlugin(
                TtsContext(),
                {
                    "api_provider": "bing_cn",
                    "enable_auto_fallback": False,
                    "voice_settings": {"tts_provider": "astrbot_tts"},
                },
            )
            plugin_astr_tts._service = translator.TranslationService(
                lambda: asyncio.sleep(0, result=sess)
            )
            ev_wav = AstrMessageEvent("fy -v apple", [Comp.Plain("/fy -v apple")])
            with mock.patch("shutil.which", return_value=None):
                out_wav = [x async for x in plugin_astr_tts.translate_command(ev_wav, GreedyStr("-v apple"))]
            self.assertEqual(len(out_wav), 2)
            self.assertIsInstance(out_wav[1].chain[0], Comp.Record)
            os.remove(ev_wav._temporary_local_files[0])
            await plugin_astr_tts.terminate()
        finally:
            if os.path.exists(wav_path):
                os.remove(wav_path)

        # 4. Explicit -v error yields friendly failure message; auto_voice failure stays quiet
        def broken_tts_handler(method, url, kw):
            if "cn.bing.com/translator" in url:
                return MockResponse(200, "<script>var params_AbusePreventionHelper = [1,\"t\",3600000];IG:\"ABCDEF1234567890ABCDEF\";</script>")
            if "ttranslatev3" in url:
                return MockResponse(200, [{"detectedLanguage": {"language": "en"}, "translations": [{"text": "苹果"}]}])
            return MockResponse(500, b"")

        plugin_err = plugin_main.TranslatePlugin(
            NoopContext(),
            {
                "api_provider": "bing_cn",
                "enable_auto_fallback": False,
                "voice_settings": {
                    "enable_voice": True,
                    "auto_voice_for_words": True,
                    "tts_provider": "bing_tts",
                },
            },
        )
        plugin_err._service = translator.TranslationService(
            lambda: asyncio.sleep(0, result=MockSession(broken_tts_handler))
        )
        out_explicit_err = [
            x
            async for x in plugin_err.translate_command(
                AstrMessageEvent("fy -v apple", [Comp.Plain("/fy -v apple")]),
                GreedyStr("-v apple"),
            )
        ]
        self.assertEqual(len(out_explicit_err), 2)
        self.assertIn("语音生成失败", out_explicit_err[1])

        out_auto_err = [
            x
            async for x in plugin_err.translate_command(
                AstrMessageEvent("fy apple", [Comp.Plain("/fy apple")]),
                GreedyStr("apple"),
            )
        ]
        self.assertEqual(len(out_auto_err), 1)

        await plugin.terminate()
        await plugin_err.terminate()

    async def test_manual_fyai_provider_selection_and_custom_wake_words(self):
        calls = []

        class MultiProviderContext:
            def get_config(self, umo=None):
                return {"wake_prefix": ["/"]}

            def get_all_providers(self):
                return [
                    types.SimpleNamespace(
                        meta=lambda: types.SimpleNamespace(id="prov-default"),
                        provider_config={"id": "prov-default", "provider_source_id": "openai"},
                        get_model=lambda: "gpt-4o-mini",
                    ),
                    types.SimpleNamespace(
                        meta=lambda: types.SimpleNamespace(id="prov-deepseek"),
                        provider_config={"id": "prov-deepseek", "provider_source_id": "deepseek_main"},
                        get_model=lambda: "deepseek-chat",
                    ),
                ]

            async def get_current_chat_provider_id(self, umo=None):
                return "prov-default"

            async def llm_generate(self, chat_provider_id, prompt, system_prompt, image_urls=None):
                calls.append((chat_provider_id, prompt, system_prompt))
                return types.SimpleNamespace(
                    role="assistant",
                    completion_text=f"AI({chat_provider_id}):{prompt}",
                )

        # 1. api_provider=bing_cn, but /fyai forces AI using blank (current) -> select_provider -> manual override
        plugin = plugin_main.TranslatePlugin(
            MultiProviderContext(),
            {
                "api_provider": "bing_cn",
                "enable_auto_fallback": False,
                "ai_settings": {
                    "ai_provider_id": "",
                    "ai_provider_manual": "",
                },
                "wake_settings": {
                    "enable_default_fy": True,
                    "custom_wake_words": ["/tl", "!tr", "翻译："],
                    "custom_ai_wake_words": ["/aitl"],
                },
            },
        )
        # Mock Bing HTTP session so /fy hits Bing while /fyai hits AI
        def bing_handler(method, url, kw):
            if "cn.bing.com/translator" in url:
                return MockResponse(200, "<script>var params_AbusePreventionHelper = [1,\"t\",3600000];IG:\"ABCDEF1234567890ABCDEF\";</script>")
            if "ttranslatev3" in url:
                return MockResponse(200, [{"detectedLanguage": {"language": "en"}, "translations": [{"text": "BING_OK"}]}])
            return MockResponse(200, {})

        plugin._service = translator.TranslationService(
            lambda: asyncio.sleep(0, result=MockSession(bing_handler))
        )

        # /fy uses Bing
        out_fy = [
            x
            async for x in plugin.translate_command(
                AstrMessageEvent("fy Hello world today", [Comp.Plain("/fy Hello world today")]),
                GreedyStr("Hello world today"),
            )
        ]
        self.assertIn("BING_OK", out_fy[0])
        self.assertEqual(len(calls), 0)

        # /fyai with blank ai_settings -> uses current active provider ("prov-default")
        out_fyai_default = [
            x
            async for x in plugin.translate_ai_command(
                AstrMessageEvent("fyai Hello world today", [Comp.Plain("/fyai Hello world today")]),
                GreedyStr("Hello world today"),
            )
        ]
        self.assertIn("AI(prov-default):Hello world today", out_fyai_default[0])

        # ai_provider_id set via select_provider dropdown
        plugin.config["ai_settings"]["ai_provider_id"] = "prov-deepseek"
        out_fyai_sel = [
            x
            async for x in plugin.translate_ai_command(
                AstrMessageEvent("fyai 日语\n早上好", [Comp.Plain("/fyai 日语\n早上好")]),
                GreedyStr("日语 早上好"),
            )
        ]
        self.assertIn("AI(prov-deepseek):早上好", out_fyai_sel[0])

        # ai_provider_manual overrides ai_provider_id and resolves by model or source name
        plugin.config["ai_settings"]["ai_provider_id"] = "prov-default"
        plugin.config["ai_settings"]["ai_provider_manual"] = "deepseek-chat"
        out_fyai_manual = [
            x
            async for x in plugin.translate_ai_command(
                AstrMessageEvent("fyai Test manual", [Comp.Plain("/fyai Test manual")]),
                GreedyStr("Test manual"),
            )
        ]
        self.assertIn("AI(prov-deepseek):Test manual", out_fyai_manual[0])

        # Manual custom provider ID not in get_all_providers() passes straight through
        plugin.config["ai_settings"]["ai_provider_manual"] = "custom-vlm-provider"
        out_fyai_raw = [
            x
            async for x in plugin.translate_ai_command(
                AstrMessageEvent("fyai Raw ID", [Comp.Plain("/fyai Raw ID")]),
                GreedyStr("Raw ID"),
            )
        ]
        self.assertIn("AI(custom-vlm-provider):Raw ID", out_fyai_raw[0])

        # Custom wake words via _DynamicWakeFilter + custom_wake_translate
        wake_filter = plugin_main._DynamicWakeFilter()

        ev_tl = AstrMessageEvent("tl Hello world today", [Comp.Plain("/tl Hello world today")], is_at_or_wake_command=True)
        ev_tl.set_extra("_consumed_wake_prefix", "/")
        self.assertTrue(wake_filter.filter(ev_tl, {"wake_prefix": ["/"]}))
        out_tl = [x async for x in plugin.custom_wake_translate(ev_tl)]
        self.assertIn("BING_OK", out_tl[0])

        ev_bang = AstrMessageEvent("!tr Hello world today", [Comp.Plain("!tr Hello world today")], is_at_or_wake_command=False)
        self.assertTrue(wake_filter.filter(ev_bang, {"wake_prefix": ["/"]}))
        out_bang = [x async for x in plugin.custom_wake_translate(ev_bang)]
        self.assertIn("BING_OK", out_bang[0])

        ev_colon = AstrMessageEvent("翻译：Hello world today", [Comp.Plain("翻译：Hello world today")], is_at_or_wake_command=False)
        self.assertTrue(wake_filter.filter(ev_colon, {"wake_prefix": ["/"]}))
        out_colon = [x async for x in plugin.custom_wake_translate(ev_colon)]
        self.assertIn("BING_OK", out_colon[0])

        ev_aitl = AstrMessageEvent("aitl Trigger AI", [Comp.Plain("/aitl Trigger AI")], is_at_or_wake_command=True)
        ev_aitl.set_extra("_consumed_wake_prefix", "/")
        self.assertTrue(wake_filter.filter(ev_aitl, {"wake_prefix": ["/"]}))
        out_aitl = [x async for x in plugin.custom_wake_translate(ev_aitl)]
        self.assertIn("AI(custom-vlm-provider):Trigger AI", out_aitl[0])

        # Disabling default /fy stops /fy in custom filter while keeping /tl
        plugin.config["wake_settings"]["enable_default_fy"] = False
        ev_default_fy = AstrMessageEvent("fy Hello", [Comp.Plain("/fy Hello")], is_at_or_wake_command=True)
        ev_default_fy.set_extra("_consumed_wake_prefix", "/")
        self.assertFalse(wake_filter.filter(ev_default_fy, {"wake_prefix": ["/"]}))

        # Status output displays AI provider and wake commands
        out_status = [
            x
            async for x in plugin.translate_command(
                AstrMessageEvent("fy --status", [Comp.Plain("/fy --status")]),
                GreedyStr("--status"),
            )
        ]
        self.assertIn("custom-vlm-provider", out_status[0])
        self.assertIn("/tl", out_status[0])
        self.assertIn("/aitl", out_status[0])

        await plugin.terminate()


class _UrllibResp:
    def __init__(self, status: int, body_bytes: bytes, final_url: str):
        self.status = status
        self._body = body_bytes
        self.url = final_url

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def text(self, errors="ignore"):
        return self._body.decode("utf-8", errors=errors)

    async def json(self, content_type=None):
        return json.loads(self._body.decode("utf-8", errors="ignore"))

    async def read(self):
        return self._body


class _AsyncContextCall:
    def __init__(self, fn):
        self._fn = fn

    async def __aenter__(self):
        return await asyncio.to_thread(self._fn)

    async def __aexit__(self, *args):
        return False


class _UrllibAsyncSession:
    def __init__(self):
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )

    def _do(self, method, url, params=None, data=None, json_data=None, headers=None, timeout=15):
        if params:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}{urllib.parse.urlencode(params)}"
        body = None
        hdrs = dict(headers or {})
        if json_data is not None:
            body = json.dumps(json_data).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        elif data is not None:
            if isinstance(data, dict):
                body = urllib.parse.urlencode(data).encode("utf-8")
            elif isinstance(data, str):
                body = data.encode("utf-8")
            else:
                body = data
        req = urllib.request.Request(url, data=body, headers=hdrs, method=method)

        def _send():
            try:
                with self.opener.open(req, timeout=getattr(timeout, "total", 15)) as resp:
                    return _UrllibResp(resp.status, resp.read(), resp.geturl())
            except urllib.error.HTTPError as exc:
                return _UrllibResp(exc.code, exc.read(), url)

        return _AsyncContextCall(_send)

    def get(self, url, params=None, headers=None, proxy=None, timeout=15, allow_redirects=True):
        return self._do("GET", url, params=params, headers=headers, timeout=timeout)

    def post(self, url, data=None, json=None, headers=None, proxy=None, timeout=15):
        return self._do("POST", url, data=data, json_data=json, headers=headers, timeout=timeout)


@unittest.skipUnless(os.getenv("RUN_LIVE_TESTS") == "1", "Set RUN_LIVE_TESTS=1 for live API tests")
class LiveBackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_free_backends_and_dictionary(self):
        sess = _UrllibAsyncSession()
        svc = translator.TranslationService(lambda: asyncio.sleep(0, result=sess))
        cases = [
            ("bing_cn", "Hello world!\nGood morning, everyone.", None, False),
            ("bing_cn", "今天天气真好，我们去散步吧。", "ja", False),
            ("bing_cn", "run", None, True),
            ("bing_cn", "instant", None, True),
            ("bing_cn", "bonjour", None, True),
            ("bing_cn", "矛盾", None, True),
            ("bing_cn", "食べる", None, True),
            ("youdao_web", "look up", None, True),
            ("bing_global", "Artificial intelligence is evolving rapidly.", None, False),
            ("mozhi_adminforge", "Hello world, how are you?", None, False),
            ("mozhi_aryak", "Hello world, how are you?", None, False),
        ]
        for provider_key, text, target_lang, expect_dict in cases:
            with self.subTest(provider=provider_key, text=text, target=target_lang):
                res = await svc.translate(
                    text=text,
                    config={
                        "api_provider": provider_key,
                        "default_target_lang": "auto_zh_en",
                        "enable_auto_fallback": False,
                    },
                    target_lang=target_lang,
                )
                self.assertTrue(res.translated_text.strip(), provider_key)
                if expect_dict:
                    self.assertTrue(res.dict_entries, f"Expected dict_entries for {text!r}")
                if text == "instant":
                    self.assertEqual(res.detected_lang, "en")
                    self.assertTrue(
                        any("英 /" in line for line in (res.dict_entries or [])),
                        f"Expected English phonetics for 'instant', got: {res.dict_entries}",
                    )
                if text == "bonjour":
                    self.assertEqual(res.detected_lang, "fr")

        # Mixed Japanese Kanji + Romaji address live check
        mixed_addr = (
            "〒9300322 富山県 中新川郡　上市町 栄町\n"
            "SAKAECHO,NAKANIIKAWA GUN KAMIICHI MACHI,TOYAMA KEN,〒9300322"
        )
        res_mixed = await svc.translate(
            text=mixed_addr,
            config={"api_provider": "bing_cn", "default_target_lang": "auto_zh_en", "enable_auto_fallback": False},
        )
        self.assertIn("富山县", res_mixed.translated_text)
        self.assertNotIn("TOYAMA 県", res_mixed.translated_text)

        # Live TTS pronunciation check (Bing + Youdao)
        sp_bing = await svc.synthesize_speech(
            text="Hello world",
            lang="en",
            config={"voice_settings": {"tts_provider": "bing_tts"}, "enable_auto_fallback": False},
        )
        self.assertGreater(len(sp_bing.audio_bytes), 1000)

        sp_yd = await svc.synthesize_speech(
            text="こんにちは",
            lang="ja",
            config={"voice_settings": {"tts_provider": "youdao_tts"}, "enable_auto_fallback": False},
        )
        self.assertGreater(len(sp_yd.audio_bytes), 1000)


if __name__ == "__main__":
    unittest.main()
