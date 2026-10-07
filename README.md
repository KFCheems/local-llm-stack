# local-llm-stack

把消费级 GPU（单卡 16-24GB）上的本地大模型，变成一套**按需启动、闲置自动关闭、
可从局域网/公网安全调用、并能给 AI 编码代理当"子代理/手"** 的完整服务栈。

纯 Python 标准库 + llama.cpp + Windows，无需 Docker，无需数据库。

## 架构

```
                                ┌─────────────────────────────── 这台机器 ──────────────────────────────┐
 手机 / 其它 PC                  │                                                                       │
   │                            │  ┌──────────────┐   8090/8092 门卫    ┌─────────────────────────────┐ │
   │  https://api.xxx.com  ─────┼──► Responses shim │──► (按需启动/闲置停止) ──► llama-server × N slots │ │
   │  (Cloudflare Tunnel+Access)│  │  :8088        │   8091/8093 引擎   │  160K 上下文 · 全力独占      │ │
   │  http://192.168.x.x:8088 ──┼──►  Bearer 鉴权    │   (互斥保护)        └─────────────────────────────┘ │
   │                            │  └──────────────┘                    GPU 20GB，同一时刻只有一个槽在跑  │
   │  https://pc.xxx.com  ──────┼──► manager :9090（面板：状态/重启/一键停引擎）                          │
   └────────────────────────────┘                                                                       │
                                                                                                        │
 本机 AI 代理（pi / dsh / Claude Code 等）── 直接调 127.0.0.1:8088/v1/responses ─────────────────────────┘
```

核心设计：

- **按需启动 + 闲置自动停止** —— 模型不常驻，不费电，显存随用随还
  （每槽 `idle_stop_sec` 可调）
- **槽位互斥 + 闲置让位** —— 20GB 显卡同一时刻全力只跑一个模型；其它槽来请求时，
  对方闲置超过 `exclusive_idle_kill_sec`（默认 60s）就自动让位切换，
  在忙则明确拒绝并提示
- **Responses 协议适配器** —— 把 Chat Completions 引擎翻译成 OpenAI Responses API
  （工具调用、思考内容、SSE 流式 + 冷启动心跳，支持 pi 等 Responses 客户端）
- **双层安全** —— 公网走 Cloudflare Access（邮箱 OTP + 服务令牌），局域网走 Bearer；
  本机回环免鉴权
- **"脑子 + 手"分工** —— dsh/Claude Code 等代理的主模型用云端旗舰（脑子），
  把子代理委派到本地 27B（手），省下最贵的旗舰输出 token

## 目录

```
config/stack.json.example   唯一配置文件（端口/路由/鉴权/槽位）
scripts/gatekeeper.py       槽位门卫：按需启动、闲置自停、互斥、鉴权
scripts/responses_shim.py   OpenAI Responses ⇄ Chat Completions 适配器
scripts/manager.py          管理面板（状态 + 重启 + 一键停引擎）
scripts/dl.ps1              分段断点续传下载器（hf-mirror 友好）
scripts/install_firewall.ps1          防火墙放行 shim 端口
scripts/make_startup_shortcuts.ps1    登录自启快捷方式
cloudflare/provision_hostname.ps1     一条命令：域名→隧道→Access→DNS
cloudflare/tunnel_http2.ps1           隧道连接器切 http2（国内网络提速）
cloudflare/cf_edge_pick.ps1           大陆边缘优选（客户端 hosts 钉定，可逆）
launchers/start_slot1.bat.example     llama-server 启动模板（含显存算账注释）
clients/*.example                     pi / dsh / cc-switch 客户端配置模板
docs/setup.md            从零部署完整流程
docs/benchmarks.md       实测数据（上下文悬崖、投机解码、子代理对比）
```

## 快速开始

> 前提：Windows + NVIDIA/AMD GPU + 已装 [llama.cpp](https://github.com/ggml-org/llama.cpp)
> 的 `llama-server.exe`，以及一个能跑的 GGUF 模型。Python 3.10+，纯标准库。

```powershell
git clone https://github.com/<you>/local-llm-stack.git
cd local-llm-stack

# 1. 配置：复制示例并编辑（模型路径、端口、鉴权 token）
Copy-Item config\stack.json.example config\stack.json
Copy-Item launchers\start_slot1.bat.example launchers\start_slot1.bat   # 编辑模型路径

# 2. 下载模型（可选，分段断点续传，走 hf-mirror）
powershell -File scripts\dl.ps1 -Url "https://hf-mirror.com/<org>/<repo>/resolve/main/<model>.gguf" `
  -Out C:\models\mymodel.gguf -Parts 4

# 3. 启动三件套（两个门卫 + 适配器）
Start-Process python -ArgumentList "scripts\gatekeeper.py --slot slot1" -WindowStyle Hidden
Start-Process python -ArgumentList "scripts\gatekeeper.py --slot slot2" -WindowStyle Hidden
Start-Process python -ArgumentList "scripts\responses_shim.py" -WindowStyle Hidden

# 4. 试一发（第一条请求会自动拉起模型，冷启动需等待）
curl http://127.0.0.1:8088/v1/responses `
  -H "Content-Type: application/json" `
  -d '{"model":"qwen38-27b-unc","input":"你好","stream":false}'
```

客户端接入（本机 pi / dsh / 任意 OpenAI 客户端）见 `clients/*.example`；
公网发布（Cloudflare Tunnel + Access 鉴权，一条命令）见 `docs/setup.md`；
登录自启与防火墙见 `scripts/make_startup_shortcuts.ps1` / `install_firewall.ps1`。

## 配置要点（config/stack.json）

| 键 | 作用 |
|---|---|
| `auth_token` | 局域网/公网调用必须的 Bearer（本机回环免鉴权） |
| `slots[].gate_port / engine_port` | 门卫端口 / llama-server 端口 |
| `slots[].start_bat` | 该槽的 llama-server 启动脚本（模型、上下文、量化全在这） |
| `slots[].exclusive_with` | 与哪些槽互斥（显存装不下两个就互相登记） |
| `slots[].idle_stop_sec` | 该槽引擎闲置多少秒自动停止（默认 300） |
| `exclusive_idle_kill_sec` | 互斥让位阈值：对方槽闲置超过该秒数则驱逐而非拒绝（默认 60） |
| `upstreams.models` | 模型 ID → 槽位 的路由表（`/v1/models` 列表也来自这里） |
| `upstreams.default_model` | 未知模型 ID 的兜底路由 |

## 安全模型

- 公网入口必须套 Cloudflare Access（脚本会自动建 Access 应用：
  人类 = 邮箱 OTP，机器 = 服务令牌请求头）；**不要裸奔无审查模型**
- 局域网 = Bearer token（`config/stack.json` 的 `auth_token`）
- 本机回环 = 免鉴权（隧道连接器从本机转发，Access 已在上游把关）
- 面板有独立 token，改 `manager_token`

## 实测结论（详见 docs/benchmarks.md）

- 单卡 20GB 跑 27B 稠密 IQ4：**160K 上下文是速度悬崖前的最大档位**（全速 35 tok/s；
  176K 起算子分档掉到 12 tok/s）
- MTP/ngram 投机解码在稠密+全 GPU 场景**无收益**（实测负优化）
- 子代理"手"选型对比：6 项任务实测，可靠性与思考开销差异显著（数据在 docs）

## License

MIT
