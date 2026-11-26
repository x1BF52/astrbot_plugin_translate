# astrbot_plugin_translate

AstrBot 多引擎翻译插件，使用 `/fy` 或 `/翻译` 指令触发翻译。配置页内置多种常见免费翻译 API 地址选项，同时支持自定义 API 地址、OpenAI 兼容接口、AstrBot 内置大模型及自定义 HTTP 模板，并支持主接口失败时自动切换到备用免费 API。

## 功能特性

- **双命令触发**：支持 `/fy` 与 `/翻译`（受 AstrBot `wake_prefix` 配置控制，默认 `/`）。
- **智能中英互译**：默认模式下，中文自动翻译为英语，外文自动翻译为简体中文。
- **多种目标语指定方式**：
  - `/fy 日语 你好` 或 `/翻译 英语 今天天气不错`
  - `/fy ->ja 你好` 或 `/fy en->zh Hello`
  - `/fy ja: 你好` 或 `/fy -t ko 你好`
- **引用消息翻译**：回复/引用任意消息并发送 `/fy`、`/翻译` 或 `/fy en` 即可翻译被引用消息的正文内容（兼容 AstrBot 引用解析与 OneBot `get_msg` 回查）。
- **配置页内置常见免费 API 选项**：
  - Bing 翻译（国内直连）：`https://cn.bing.com/ttranslatev3`（默认）
  - Bing 翻译（国际版）：`https://www.bing.com/ttranslatev3`
  - 有道翻译（网页免费版）：`https://m.youdao.com/translate`
  - MyMemory 免费接口：`https://api.mymemory.translated.net/get`
  - Google 翻译（GTX 免费接口）：`https://translate.googleapis.com/translate_a/single`
  - Google 翻译（Chrome 扩展接口）：`https://clients5.google.com/translate_a/t`
  - Mozhi 公共实例：`https://mozhi.adminforge.de/api/translate`、`https://mozhi.aryak.me/api/translate`、`https://mozhi.pussthecat.org/api/translate`
  - Lingva 接口：`https://lingva.ml/api/v1`（公共实例易限流，可在配置页覆盖为自建实例地址）
  - LibreTranslate 接口：`https://libretranslate.com/translate`（支持自建/免密镜像或填写 Key）
  - DeepLX 接口：`http://127.0.0.1:1188/translate`
  - OpenAI 兼容接口（SiliconFlow / DeepSeek / Groq / Ollama / 智谱等）与 AstrBot 当前内置大模型
  - 自定义 HTTP 翻译 API（支持 GET/POST、自定义请求头/请求体模板与 JSON 路径提取）
- **失败自动回退**：主 API 超时、限流或返回异常时，按备用列表顺序自动重试其他免费接口。

## 安装方法

### 方式一：通过 AstrBot WebUI 从 GitHub 安装（推荐）

1. 打开 AstrBot 管理面板，进入 **插件 → 已安装**（或插件页右下角 **安装插件 `+`** 按钮）。
2. 选择通过仓库链接安装，输入本仓库地址：
   ```text
   https://github.com/x1BF52/astrbot_plugin_translate
   ```
3. 安装完成后，在插件卡片点击 **配置** 即可选择或自定义翻译 API。

### 方式二：手动放置到插件目录

1. 克隆或下载本仓库到 AstrBot 的 `data/plugins/astrbot_plugin_translate` 目录：
   ```bash
   git clone https://github.com/x1BF52/astrbot_plugin_translate
   ```
2. 若环境未安装依赖，确保已安装 `requirements.txt` 中的 `aiohttp>=3.9.0`。
3. 在 AstrBot 管理面板重载插件或重启 AstrBot。

## 常用指令

| 指令示例 | 说明 |
| --- | --- |
| `/fy <文本>` 或 `/翻译 <文本>` | 使用配置页中的默认目标语言进行翻译（默认为智能中英互译） |
| `/fy 日语 <文本>` | 翻译为指定语言（支持 `英语`、`日语`、`韩语`、`法语`、`德语`、`西班牙语`、`俄语`、`繁体中文` 等） |
| `/fy ->ja <文本>` / `/fy en->zh <文本>` | 使用箭头语法指定目标语言或源语言→目标语言 |
| `/fy ja: <文本>` / `/fy -t ja <文本>` | 使用冒号或 `-t`/`--to` 参数指定目标语言 |
| 引用消息 + `/fy` 或 `/fy en` | 翻译被引用的历史消息内容 |
| `/fy --langs` | 查看插件支持的目标语言代码与中文名称 |
| `/fy --status` | 查看当前生效的主翻译接口、接口地址、默认目标语、备用回退链与代理状态 |
| `/fy --help` | 查看插件帮助信息 |

## 配置说明

在 AstrBot WebUI **插件 → 多引擎翻译插件 → 配置** 中可配置：

- **主翻译 API (`api_provider`)**：从预设免费接口或扩展接口中选择。
- **自定义 API 地址 (`custom_api_url`)**：留空时使用所选预设的默认地址；填入完整 URL 可覆盖预设地址（如自建 Lingva / LibreTranslate / DeepLX 或第三方 OpenAI 兼容接口）。
- **API Key / 访问令牌 (`api_key`)**：免密接口留空即可；需要鉴权时填写（WebUI 中以密码框遮罩显示）。
- **默认目标语言 (`default_target_lang`)**：默认 `auto_zh_en`（中文→英语，外文→简体中文），也可固定为指定语言。
- **失败自动切换备用 API (`enable_auto_fallback` / `fallback_providers`)**：开启后按顺序兜底重试。
- **HTTP 代理地址 (`proxy_url`)**：可选，用于访问海外接口。
- **高级设置 (`advanced`)**：含 Mozhi 后端引擎、MyMemory 邮箱参数、是否展示引擎/语种/原文、请求超时与最大字符数限制。

## 运行依赖

- Python 包依赖见 [requirements.txt](requirements.txt)：`aiohttp>=3.9.0`。

## AI 声明 / AI Disclosure

本插件的代码实现、配置 Schema 及说明文档均由 AI 辅助生成与整理。  
使用前请注意：
1. 本插件包含部分公开免费的第三方非官方网页/社区接口（如 Bing、有道网页版、MyMemory、Google 免费接口、Mozhi 等），其可用性与速率限制取决于对应服务商或公共实例状态，不作 SLA 稳定性保证。
2. 如需用于高并发或生产场景，建议在配置页切换为您自己的 OpenAI 兼容接口、AstrBot 内置大模型或自建翻译服务（如自建 LibreTranslate / DeepLX / Lingva）。
3. 开源代码请自行审查后在可信环境中运行，作者与贡献者不对第三方接口变动或外部服务限制承担责任。
