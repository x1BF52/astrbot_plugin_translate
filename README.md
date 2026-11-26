# astrbot_plugin_translate

AstrBot 多引擎翻译插件，使用 `/fy` 或 `/翻译` 指令触发翻译。配置页内置 20+ 翻译 API 预设（含零密钥直连接口、国内/海外免费大模型与免费开放平台额度预设），支持自定义接口、失败自动回退，并在翻译单词或短语时自动展现音标、词性与多种释义。

## 功能特性

- **双命令触发**：支持 `/fy` 与 `/翻译`（受 AstrBot `wake_prefix` 配置控制，默认 `/`）。
- **对话回复纯净简洁**：默认不在聊天回复中展示翻译渠道名称，仅呈现语种方向与译文（可在高级设置按需开启）。
- **单词 / 短语词典模式**：输入单个单词或短语（如 `/fy run`、`/fy look up`、`/fy 矛盾`、`/fy 食べる`）时，自动附带音标/读音、多词性分类释义与常见搭配。
- **智能中英互译**：默认模式下，中文自动翻译为英语，外文自动翻译为简体中文。
- **多种目标语指定方式**：
  - `/fy 日语 你好` 或 `/翻译 英语 今天天气不错`
  - `/fy ->ja 你好` 或 `/fy en->zh Hello`
  - `/fy ja: 你好` 或 `/fy -t ko 你好`
- **引用消息翻译**：回复/引用任意消息并发送 `/fy`、`/翻译` 或 `/fy en` 即可翻译被引用消息的正文内容。
- **失败自动回退**：主 API 超时、限流或返回异常时，按备用列表顺序自动重试其他免费接口。

## 内置 API 预设

### 1. 免密钥直连免费接口（开箱即用）
- **Bing 翻译（国内直连 / 国际版）**：`https://cn.bing.com/ttranslatev3`（默认）、`https://www.bing.com/ttranslatev3`
- **有道翻译（网页免费版）**：`https://m.youdao.com/translate`
- **Mozhi 公共实例**：`https://mozhi.adminforge.de/api/translate`、`https://mozhi.aryak.me/api/translate`、`https://mozhi.pussthecat.org/api/translate`
- **MyMemory 免费接口**：`https://api.mymemory.translated.net/get`
- **Google 翻译（GTX / Chrome 扩展接口）**：`https://translate.googleapis.com/translate_a/single`、`https://clients5.google.com/translate_a/t`
- **Lingva 接口**：`https://lingva.ml/api/v1`（公共实例易限流，可在配置页覆盖为自建地址）

### 2. 免费大模型 & 开放平台预设（填写对应平台免费 Key 即可用）
- **智谱 AI（`zhipu_free`）**：`https://open.bigmodel.cn/api/paas/v4/chat/completions`，默认免费模型 `GLM-4-Flash-250414`（亦可选 `GLM-4.7-Flash`）
- **硅基流动（`siliconflow_free`）**：`https://api.siliconflow.cn/v1/chat/completions`，默认免费翻译模型 `tencent/Hunyuan-MT-7B`
- **Groq（`groq_free`）**：`https://api.groq.com/openai/v1/chat/completions`，默认免费开源模型 `openai/gpt-oss-120b`
- **小牛翻译（`niutrans`）**：`https://api.niutrans.com/NiuTransServer/translation`（每日免费 20 万字符，超额不自动扣费）
- **火山引擎机器翻译（`volcengine`）**：`https://translate.volcengineapi.com`（每月前 200 万字符免费；请在火山控制台开启余额预警避免超额后付费）
- **百度翻译开放平台（`baidu_fanyi`）**：`https://fanyi-api.baidu.com/api/trans/vip/translate`（未认证 5 万/月，个人认证 100 万/月）
- **阿里云机器翻译（`aliyun_mt`）**：`https://mt.cn-hangzhou.aliyuncs.com`（通用版每月前 100 万字符免费）

### 3. 自建 / 通用扩展接口
- **LibreTranslate / DeepLX**：支持本地或自建无密钥实例
- **OpenAI 兼容接口**：可填任意中转站或本地 Ollama（`http://127.0.0.1:11434/v1/chat/completions`）
- **AstrBot 当前内置大模型**：直接复用 AstrBot 对话模型
- **自定义 HTTP API**：支持任意 GET/POST 模板与 JSON 路径提取

## 安装方法

### 方式一：通过 AstrBot WebUI 从 GitHub 安装（推荐）

1. 打开 AstrBot 管理面板，进入 **插件 → 已安装**（或右下角 **安装插件 `+`** 按钮）。
2. 选择通过仓库链接安装，输入本仓库地址：
   ```text
   https://github.com/x1BF52/astrbot_plugin_translate
   ```
3. 安装完成后，在插件卡片点击 **配置** 即可选择预设或填写密钥。

### 方式二：手动放置到插件目录

1. 克隆本仓库到 AstrBot 的 `data/plugins/astrbot_plugin_translate` 目录：
   ```bash
   git clone https://github.com/x1BF52/astrbot_plugin_translate
   ```
2. 若环境未安装依赖，确保已安装 `requirements.txt` 中的 `aiohttp>=3.9.0`。
3. 在 AstrBot 管理面板重载插件或重启 AstrBot。

## 常用指令

| 指令示例 | 说明 |
| --- | --- |
| `/fy <文本>` 或 `/翻译 <文本>` | 翻译文本；单词或短语自动展示词性与多释义 |
| `/fy 日语 <文本>` | 翻译为指定语言（支持 `英语`、`日语`、`韩语`、`法语`、`德语`、`西班牙语`、`俄语`、`繁体中文` 等） |
| `/fy ->ja <文本>` / `/fy en->zh <文本>` | 使用箭头语法指定目标语言或源语言→目标语言 |
| `/fy ja: <文本>` / `/fy -t ja <文本>` | 使用冒号或 `-t`/`--to` 参数指定目标语言 |
| 引用消息 + `/fy` 或 `/fy en` | 翻译被引用的历史消息内容 |
| `/fy --langs` | 查看插件支持的目标语言代码与中文名称 |
| `/fy --status` | 查看当前生效的主翻译接口、接口地址、默认目标语、备用回退链与代理状态 |
| `/fy --help` | 查看插件帮助信息 |

## 本地测试（无需安装 AstrBot）

```bash
# 离线单元测试 + 模拟集成测试
python -m unittest discover -s tests -v

# 包含实网免费接口与词典联调测试
RUN_LIVE_TESTS=1 python -m unittest discover -s tests -v
```

## 运行依赖

- Python 包依赖见 [requirements.txt](requirements.txt)：`aiohttp>=3.9.0`。

## AI 声明 / AI Disclosure

本插件的代码实现、配置 Schema 及说明文档均由 AI 辅助生成与整理。  
使用前请注意：
1. 本插件包含部分公开免费的第三方非官方网页/社区接口（如 Bing、有道网页版、MyMemory、Google 免费接口、Mozhi 等），其可用性与速率限制取决于对应服务商或公共实例状态，不作 SLA 稳定性保证。
2. 火山引擎、百度翻译、阿里云机器翻译等商业云平台在免费额度耗尽后可能触发后付费计费，使用前请在对应云厂商控制台设置余额/用量预警或配额上限。
3. 开源代码请自行审查后在可信环境中运行，作者与贡献者不对第三方接口变动、费用或外部服务限制承担责任。
