# XiaoAi-LLM-Router — Huffman 维护版

[English](README_EN.md) · [原项目](https://github.com/FelixQiu1/XiaoAi-LLM-Router)

通过小米云读取小爱音箱的对话文字，将符合调用条件的问题交给大模型，再让提问的音箱播出回答。
本仓库是 FelixQiu1/XiaoAi-LLM-Router 的 Fork，保留原作者历史与 MIT 许可证，集中维护设备接入、长回答播报和用量记录的改进。

## 当前能力

- 使用 MiService Python 库登录、列出设备；小米云对话历史作为接收来源。
- 支持多台设备轮询，分别维护上下文和去重状态。
- 通过 LiteLLM 调用 DeepSeek、Ollama 等模型。
- MIoT 播报按硬件型号选择指令；长回答按字数分段、按保守语速等待后依次提交。
- 可按设备配置播报前停止音乐，避免旧音乐影响语音回复。
- 可记录供应商实际返回的输入、输出、缓存及推理 token 用量；不在用量文件中保存对话正文或密钥。
- 以短句语音问答、MIoT 播报验证过 LX04 和 L05B；这不代表所有设备或所有问题都可用。

## 已知限制

- **这不是小米官方授权的集成，也不是面向收费用户的成熟服务。** 开源许可证不代表获得小米接口使用或商业运营授权。请另行核实相关服务协议。
- 接收依赖小米云历史记录。音箱屏幕显示完整问题，不保证该问题出现在云端接口；实测部分长问题未进入记录，短句可以正常处理，具体限制尚未确定。
- 即使用 Ollama 本地运行模型，音箱接入和控制仍依赖小米云，不能称为完全离线。
- 各设备上下文独立；默认保留最近 10 轮，闲置 10 分钟清空；程序重启也会清空。
- 旧CLI模式每次提问或追问都需要触发词，例如“深思”或“问问AI”。网页平台默认采用小爱失败后AI补答，也可改为固定触发词模式；具体边界见[平台说明](docs/platform.md)。
- 分段等待是按语速估算，不是可靠的播放完成回调；会有段间停顿。目前推荐 `stream: false`。尚无完善的语音打断或跨设备并行播放调度。
- **用量记录不是预算控制。** 当前没有用户限额、费用熔断或单次输出 token 的强制限额；失败请求没有供应商用量时记为未知，不推断为零。
- 云端实际用量可能包含重试等记录无法完整解释的情况，最终费用以模型服务商账单为准。

## 本地安装

需要 Python 3.11 或 3.12，以及可登录的自有小米账号和在线音箱。

```bash
git clone https://github.com/Huffmanliu/XiaoAi-LLM-Router.git
cd XiaoAi-LLM-Router
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.example.yaml config.yaml
cp .env.example .env
```

在 `.env` 中填写 `MI_USERNAME`、`MI_PASSWORD` 和所选模型的 API Key。环境变量优先于配置文件。

```bash
.venv/bin/python main.py --list-devices
```

首次登录可能要求短信验证码，请在交互终端中完成验证。将设备列表中的 **MiNA deviceID** 填入配置文件的 `mi.device_id` 或 `mi.devices`；它与数字形式的 MIoT DID 不同。

```bash
.venv/bin/python main.py --config config.yaml
```

示例：“小爱同学，深思，洗车该开车还是走路？”

## 容器部署

配置好 `.env` 和 `config.yaml` 后，把配置中的令牌路径改为持久卷目录：

```yaml
mi:
  token_path: /home/router/.local/state/xiaoai-router/mi.token
# 如需用量统计，启用下面的配置：
usage:
  log_path: /home/router/.local/state/xiaoai-router/usage.jsonl
```

首次使用交互终端完成登录，再启动后台服务：

```bash
docker compose build
docker compose run --rm router python main.py --list-devices
docker compose up -d
docker compose logs -f router
```

凭据、登录令牌和用量文件不得提交到 Git。容器配置不开放额外的公网入站端口。

## 开发与版本管理

```bash
.venv/bin/pip install pytest flake8
.venv/bin/python -m pytest tests/ -q
.venv/bin/flake8 main.py miservice_glue.py llm_router.py memory.py mi_tts.py mi_conversation.py usage_ledger.py
```

测试不需要真实账号、音箱或 API Key。GitHub Actions 检查 Python 3.11/3.12 的测试与代码规范，并验证容器构建及运行模块导入。

- `origin`：你的 Fork；`upstream`：原作者仓库。
- 功能和修复在独立分支完成，经测试及 PR 检查后合并到自己的 `main`。
- 部署时记录明确的标签或提交编号；升级前备份运行配置和状态，回滚时只更换代码。
- 私人服务器地址、设备编号、凭据和实际对话记录保留在部署环境，不放在公共仓库。

详细改进见 [CHANGELOG.md](CHANGELOG.md)。

## 许可证与依赖

MIT，保留原作者 FelixQiu1 的版权声明。依赖 [MiService](https://github.com/Yonsm/MiService) 与 [LiteLLM](https://github.com/BerriAI/litellm)。

## 用户前台与管理后台（邀请试用）

本机可运行 `.venv/bin/python -m platform_app`，在浏览器访问 <http://127.0.0.1:8765>。
支持网页小米绑定与验证码、设备选择、多用户隔离、额度和加密历史。
完整说明见 [网页平台使用说明](docs/platform.md)。

生产部署和回退流程见 [部署说明](docs/deployment/production.md)。
