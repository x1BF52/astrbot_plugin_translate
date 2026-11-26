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
import types
import unittest
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
        ):
            self._msg_str = msg_str
            self._messages = messages or []
            self.bot = bot
            self.unified_msg_origin = umo

        def get_message_str(self):
            return self._msg_str

        def get_messages(self):
            return self._messages

        def plain_result(self, text: str):
            return text

    class _Filter:
        @staticmethod
        def command(name, alias=None):
            def decorator(fn):
                fn._command_meta = (name, alias or set())
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
        return json.dumps(self._body, ensure_ascii=False)

    async def json(self, content_type=None):
        if isinstance(self._body, str):
            return json.loads(self._body)
        return self._body


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


class ParserAndHelperTests(unittest.TestCase):
    def test_parse_command_args(self):
        self.assertEqual(translator.parse_command_args("", has_reply=False).action, "help")
        self.assertEqual(translator.parse_command_args("", has_reply=True).action, "translate")
        self.assertEqual(translator.parse_command_args("--help").action, "help")
        self.assertEqual(translator.parse_command_args("-h").action, "help")
        self.assertEqual(translator.parse_command_args("--status").action, "status")
        self.assertEqual(translator.parse_command_args("--langs").action, "langs")

        # Literal "help" should translate instead of showing help
        p = translator.parse_command_args("help")
        self.assertEqual((p.action, p.text), ("translate", "help"))

        p = translator.parse_command_args("日语 你好世界")
        self.assertEqual((p.action, p.target_lang, p.text), ("translate", "ja", "你好世界"))

        p = translator.parse_command_args("英语 今天天气不错")
        self.assertEqual((p.action, p.target_lang, p.text), ("translate", "en", "今天天气不错"))

        p = translator.parse_command_args("en 你好世界")
        self.assertEqual((p.action, p.target_lang, p.text), ("translate", "en", "你好世界"))

        # Guarded short ASCII language codes should not hijack English/Spanish/French sentences
        p = translator.parse_command_args("en route to work")
        self.assertEqual((p.action, p.target_lang, p.text), ("translate", None, "en route to work"))
        p = translator.parse_command_args("de la vida")
        self.assertEqual((p.action, p.target_lang, p.text), ("translate", None, "de la vida"))

        # Quoted reply with target language only
        p = translator.parse_command_args("en", has_reply=True)
        self.assertEqual((p.action, p.target_lang, p.text), ("translate", "en", ""))

        # Arrow, colon, flag syntaxes
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

    def test_lang_detection_and_target_resolution(self):
        self.assertEqual(translator.resolve_target_lang("你好世界", "auto_zh_en"), "en")
        self.assertEqual(translator.resolve_target_lang("Hello world", "auto_zh_en"), "zh-CN")
        self.assertEqual(translator.resolve_target_lang("こんにちは", "auto_zh_en"), "zh-CN")
        self.assertEqual(translator.resolve_target_lang("안녕하세요", "auto_zh_en"), "zh-CN")
        self.assertEqual(translator.resolve_target_lang("帮我看看这个Error", "auto_zh_en"), "en")
        self.assertEqual(translator.resolve_target_lang("你好", "ja"), "ja")

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
            translator.redact_secret("Error at https://example.com?key=SEC%2F123", "SEC/123"),
            "Error at https://example.com?key=***",
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
    async def test_all_mocked_backends_and_chunk_whitespace(self):
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
                return MockResponse(
                    200,
                    [{"translations": [{"text": f"BING:{txt}"}], "detectedLanguage": {"language": "en"}}],
                )
            if "translate.googleapis.com" in url:
                q = kw["params"]["q"]
                return MockResponse(200, [[[f"GTX:{q}", q]], None, "en"])
            if "clients5.google.com" in url:
                return MockResponse(200, [["你好", "en"], ["世界", "en"]])
            if "lingva.ml" in url:
                return MockResponse(200, {"translation": "你好世界", "info": {"detectedSource": "en"}})
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
            if "open.bigmodel.cn" in url:
                self.assertEqual(url, "https://open.bigmodel.cn/api/paas/v4/chat/completions")
                return MockResponse(
                    200,
                    {
                        "choices": [
                            {
                                "message": {
                                    "content": [
                                        {"type": "text", "text": "<think>hmm</think>\n你好Zhipu"}
                                    ]
                                }
                            }
                        ]
                    },
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

        # Bing token refresh retry + port preservation
        res = await svc.translate(
            "Hello",
            {"api_provider": "bing_cn", "enable_auto_fallback": False},
        )
        self.assertEqual(res.translated_text, "BING:Hello")
        self.assertEqual(bing_calls, {"token": 2, "post": 2})

        # Google GTX multi-chunk preserves blank lines across chunks
        gtx_text = ("A" * 800) + "\n\n" + ("B" * 800)
        res = await svc.translate(gtx_text, {"api_provider": "google_free", "enable_auto_fallback": False})
        self.assertIn("\n\n", res.translated_text)

        # Clients5, Lingva, DeepLX, Mozhi(Google), OpenAI, Custom HTTP
        self.assertEqual(
            (await svc.translate("Hi", {"api_provider": "google_clients5", "enable_auto_fallback": False})).translated_text,
            "你好世界",
        )
        self.assertEqual(
            (await svc.translate("Hi", {"api_provider": "lingva_ml", "enable_auto_fallback": False})).translated_text,
            "你好世界",
        )
        self.assertEqual(
            (await svc.translate("Hi", {"api_provider": "deeplx", "api_key": "K", "enable_auto_fallback": False})).translated_text,
            "你好DeepLX",
        )
        self.assertEqual(
            (
                await svc.translate(
                    "Hi",
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
                    "Hi",
                    {
                        "api_provider": "openai_compat",
                        "custom_api_url": "https://open.bigmodel.cn/api/paas/v4",
                        "api_key": "ZKEY",
                        "openai_system_prompt": 'To {target_lang} {"ok": true}',
                        "enable_auto_fallback": False,
                    },
                )
            ).translated_text,
            "你好Zhipu",
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
            await svc.translate("Hi", {"api_provider": "libretranslate", "enable_auto_fallback": False})
        self.assertIn("HTTP 502", str(ctx.exception))

        # LibreTranslate success
        self.assertEqual(
            (
                await svc.translate(
                    "Hi",
                    {
                        "api_provider": "libretranslate",
                        "custom_api_url": "https://lt.local",
                        "enable_auto_fallback": False,
                    },
                )
            ).translated_text,
            "你好LT",
        )

        # Youdao rejects zh-TW cleanly and falls back
        with self.assertRaises(translator.TranslationError):
            await svc.translate(
                "Hello",
                {"api_provider": "youdao_web", "enable_auto_fallback": False},
                target_lang="zh-TW",
            )

        # Fallback: mymemory 429 -> youdao_web
        fb = await svc.translate(
            "Hello",
            {
                "api_provider": "mymemory",
                "enable_auto_fallback": True,
                "fallback_providers": ["youdao_web"],
            },
        )
        self.assertTrue(fb.fallback_used)
        self.assertEqual(fb.provider_key, "youdao_web")
        self.assertEqual(fb.translated_text, "回退成功")

    async def test_translate_plugin_commands_and_replies(self):
        class ModernContext:
            def get_config(self, umo=None):
                return {"wake_prefix": ["/"]}

            async def get_current_chat_provider_id(self, umo=None):
                return "prov-1"

            async def llm_generate(self, chat_provider_id, prompt, system_prompt):
                return types.SimpleNamespace(
                    role="assistant",
                    completion_text=f"<think>plan</think>LLM:{prompt}->{system_prompt[:10]}",
                )

        plugin = plugin_main.TranslatePlugin(
            ModernContext(),
            {
                "api_provider": "astrbot_llm",
                "default_target_lang": "auto_zh_en",
                "enable_auto_fallback": False,
                "advanced": {
                    "show_engine_name": True,
                    "show_detected_lang": True,
                    "show_original": False,
                    "max_text_length": 200,
                },
            },
        )

        # /fy 翻译 recovers literal argument "翻译"
        ev1 = AstrMessageEvent("fy", [Comp.Plain("/fy 翻译")])
        out1 = [x async for x in plugin.translate_command(ev1, GreedyStr(""))]
        self.assertIn("LLM:翻译->", out1[0])

        # Quote extraction via OneBot bot.api.call_action("get_msg")
        class FakeAPI:
            async def call_action(self, action, **kwargs):
                return {"data": {"message": [{"type": "text", "data": {"text": "Quoted text"}}]}}

        ev2 = AstrMessageEvent(
            "fy en",
            [Comp.Reply(id="99"), Comp.At("123"), Comp.Plain(" /fy en")],
            bot=types.SimpleNamespace(api=FakeAPI()),
        )
        out2 = [x async for x in plugin.translate_command(ev2, GreedyStr("en"))]
        self.assertIn("LLM:Quoted text->", out2[0])
        self.assertIn("英语", out2[0])

        # --langs, --status, --help
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

        # Length limit guard
        out_long = [
            x
            async for x in plugin.translate_command(
                AstrMessageEvent("fy " + "x" * 250, [Comp.Plain("/fy " + "x" * 250)]),
                GreedyStr("x" * 250),
            )
        ]
        self.assertIn("超过上限", out_long[0])

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


class _AsyncContextCall:
    def __init__(self, fn):
        self._fn = fn

    async def __aenter__(self):
        return await asyncio.to_thread(self._fn)

    async def __aexit__(self, *args):
        return False


@unittest.skipUnless(os.getenv("RUN_LIVE_TESTS") == "1", "Set RUN_LIVE_TESTS=1 for live API tests")
class LiveBackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_free_backends(self):
        sess = _UrllibAsyncSession()
        svc = translator.TranslationService(lambda: asyncio.sleep(0, result=sess))
        cases = [
            ("bing_cn", "Hello world!\nGood morning.", None),
            ("bing_cn", "今天天气真好，我们去散步吧。", "ja"),
            ("bing_global", "Artificial intelligence is evolving rapidly.", None),
            ("youdao_web", "Line 1: Hello world\n\nLine 2: Good morning", None),
            ("mozhi_adminforge", "Hello world", None),
            ("mozhi_aryak", "Hello world", None),
        ]
        for provider_key, text, target_lang in cases:
            with self.subTest(provider=provider_key, target=target_lang):
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
                if "\n\n" in text:
                    self.assertIn("\n\n", res.translated_text)


if __name__ == "__main__":
    unittest.main()
