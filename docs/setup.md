# 从零部署完整流程

> 本文是实测走过的完整路径（Windows 11 + RX 7900 XT 20GB + 9800X3D + 32GB RAM）。
> 所有组件均可替换为你的实际值。

## 0. 前提

- Windows 10/11，Python 3.10+（纯标准库，无需 pip install）
- [llama.cpp](https://github.com/ggml-org/llama.cpp) 构建（`llama-server.exe`），
  放在如 `C:\LLM\llama\`
- 一个 Cloudflare 托管的域名（公网发布用；纯局域网使用可跳过第 5 节）
- （可选）HF token：`hf-mirror.com` 大多无需鉴权；官方 HF 需要

## 1. 模型获取

用仓库自带的分段断点续传下载器（走 hf-mirror，绕开系统代理）：

```powershell
powershell -File scripts\dl.ps1 `
  -Url "https://hf-mirror.com/<org>/<repo>/resolve/main/<model>.gguf" `
  -Out "C:\models\<model>.gguf" -Parts 4
# -Expected <字节数> 可选：严格校验大小，防止把错误页当模型存下来（实测踩过）
# -AuthFile hf_token.txt 可选：需要鉴权的仓库
```

选型建议（20GB 卡、子代理用途）：
- 27B 稠密 IQ4_XS（~14.5GB）：质量高，速度 ~35 tok/s（带宽极限）
- 30B-A3B / 35B-A3B MoE IQ4（~15-17GB）：3B 激活，~130-160 tok/s，适合高频子任务
- 量化越大越聪明，但 VRAM = 权重 + KV，见下节算账

## 2. 配置

```powershell
Copy-Item config\stack.json.example config\stack.json
# 编辑：auth_token / manager_token 换成自己的随机串；
#       slots 的 start_bat 指向你的启动脚本；upstreams.models 写你的模型 ID
Copy-Item launchers\start_slot1.bat.example launchers\start_slot1.bat
# 编辑：-m 模型路径、-c 上下文、-ctk/-ctv KV 量化、--port 对应 engine_port
```

**上下文算账**（重要，实测数据在 docs/benchmarks.md）：
`VRAM ≈ 权重 + 1.8GB 运行时 + 上下文 × 每token KV`。
Qwen3.8-27B 级稠密模型 q4_0 KV ≈ 19.5KB/token、q8_0 ≈ 36KB/token。
20GB 卡 + 14.5GB 权重 → q4 KV 实测 160K 全速（35 tok/s），176K 起速度悬崖（12 tok/s）。
**一定要实测你的档位**，不要信推算。

## 3. 启动与自愈

```powershell
Start-Process python -ArgumentList "scripts\gatekeeper.py --slot slot1" -WindowStyle Hidden
Start-Process python -ArgumentList "scripts\gatekeeper.py --slot slot2" -WindowStyle Hidden
Start-Process python -ArgumentList "scripts\responses_shim.py"      -WindowStyle Hidden
Start-Process python -ArgumentList "scripts\manager.py"             -WindowStyle Hidden
```

- 客户端第一条请求会自动拉起对应槽的引擎（冷启动 = WSL/页缓存冷时 1-4 分钟，
  热时 10-25 秒），期间流式请求会收到 SSE 心跳保活
- 闲置自动杀引擎释放显存（每槽 `idle_stop_sec`，默认 300 秒）
- 互斥：槽 A 在跑时，槽 B 的启动请求先看 A 的心跳——A 闲置超过
  `exclusive_idle_kill_sec`（默认 60 秒）会自动驱逐 A 并启动 B（切换 ≈ 一次冷启动）；
  A 在忙则拒绝（503 指名道姓），稍后重试即可
- 状态随时查：`http://127.0.0.1:8090/gatekeeper/status`（每个槽）或面板
- 日志：`logs/` 目录

登录自启：

```powershell
powershell -File scripts\make_startup_shortcuts.ps1 `
  -PythonExe "C:\path\to\python.exe" -StackDir "C:\path\to\local-llm-stack"
```

（pythonw 静默运行；端口被占时自动退出，重复登录不会起双份。）

## 4. 客户端接入

> **统一网关（可选）**：也可以在栈前面套一层本机统一网关（如 Magpie :3425，
> 自带 key 管理与多提供商路由）。此时客户端只记一个入口，模型 id 变为
> `qqking/<model>`，公网 ingress 指向统一网关，鉴权 = Access 服务令牌 +
> 网关 key 双层；本机回环调用统一网关通常免 key。栈自身（shim :8088）仍是
> 引擎侧的唯一直连入口，两种用法可并存。

### 本机（pi / dsh / 任意 OpenAI 客户端）

Base URL：`http://127.0.0.1:8088/v1`（Responses 协议）。
pi 的 models.json 模板见 `clients/pi-models.json.example`。

### 局域网其它机器

Base URL：`http://<本机内网IP>:8088/v1`，请求头 `Authorization: Bearer <auth_token>`。
先放行防火墙（仅私网/域 profile）：`powershell -File scripts\install_firewall.ps1 -Port 8088`。
建议在路由器上给本机绑静态 IP。

### 公网（Cloudflare Tunnel + Access，一条命令）

前置：装好 [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/)并建一条 dashboard 托管的隧道（或 `cloudflared tunnel create`）；环境变量
`CLOUDFLARE_TOKEN` 放一个有 Access/Tunnel/DNS 编辑权限的 API 令牌。

```powershell
powershell -File cloudflare\provision_hostname.ps1 `
  -Hostname "api.your-domain.com" -Port 8088 -AppPrefix "Local API" `
  -OwnerEmails "you@example.com" -CreateToken
```

这条命令完成：Access 应用（邮箱 OTP + 服务令牌双策略）→ 隧道 ingress → DNS CNAME。
API 客户端带三条头即可直调（服务令牌见 -CreateToken 的输出，有效期一年）：

```
CF-Access-Client-Id / CF-Access-Client-Secret / Authorization: Bearer <auth_token>
```

> 个人自用可以省掉 Access 这层：在 Zero Trust 控制台删掉对应的 Access 应用后，
> 公网只凭网关/Bearer key 鉴权（key 是高熵随机串即可接受；想恢复随时重跑本命令）。
> 保留 Access 的好处是 key 单独泄露不会直接暴露，且自带 bot 防护。

国内网络优化：连接器切 http2（QUIC 常被限速，实测首请求 25.7s → 1.4s）：

```powershell
powershell -File cloudflare\tunnel_http2.ps1
```

面板同理发布：`-Hostname "pc.your-domain.com" -Port 9090 -AppPrefix "Panel"`。

### 中国大陆延迟优化（实测 2026-10-07）

CF 免费版在大陆没有本陆节点，默认 DNS 常落到远端边缘（电信家宽实测落到
0.4s 握手的 PoP）。实测对比：

| 路径 | TLS 握手 | 单请求 TTFB |
|---|---|---|
| 默认 DNS 边缘（104.19.0.1） | 0.40-0.55s | 0.86-2.44s（抖动大） |
| 优选边缘（108.162.192.1） | **0.16s** | **~0.95s（稳定）** |
| 引擎就绪后的纯隧道开销 | - | ~0.9s/请求（结构性下限） |

三层手段，按代价从小到大：

1. **客户端 Clash 加直连规则**：如果代理了这个域名，流量会先出境绕代理节点
   再回大陆 origin（跨太平洋两趟）。规则：`DOMAIN,api.qqking.top,DIRECT`
2. **边缘优选**（纯客户端、可逆、不动 Access 鉴权）：

   ```powershell
   powershell -File cloudflare\cf_edge_pick.ps1            # 探测 24 个边缘并写 hosts（管理员）
   powershell -File cloudflare\cf_edge_pick.ps1 -DryRun    # 只测不写
   powershell -File cloudflare\cf_edge_pick.ps1 -Restore   # 撤销 hosts 钉定
   ```

   SNI 路由使任播任意边缘都能服务本域名，实测握手 391ms→144ms。
   每台客户端各自跑一次即可。
3. **自有 PC 走 Tailscale 组网（推荐，近乎局域网）**：两端 `tailscale up` 后，
   客户端 Base URL 改 `http://<LLM机的Tailscale IP>:8088/v1`（Bearer 照带）。
   P2P 打洞成功时延迟 ≈ 物理 RTT；失败走 DERP 中继。CF 隧道保留给手机/他人。
   有国内 VPS 的话 frp/WireGuard 中转是最优解（10-40ms）。

> 排障记录：ingress 是 token 托管模式时规则在 CF 云端不在本地；若公网 401
> 带 "Invalid or missing API key" 而本机 8088 正常，先查隧道路由是否指错端口。

## 5. 接入 AI 编码代理（"脑子 + 手"）

**dsh（DeepSeek Harness）**：主模型保持云端旗舰，把 `subagent` 工具委派强制指向
本栈（`clients/dsh-brain-hands.yml.example`）。合并 `dsh-remote-overlay.yml.example`
的 provider 定义后：

```yaml
- id: tool-subagent
  name: "@deepseek-ai/dsh-tool-subagent"
  config:
    provider: spawn
    toolName: subagent
    backgroundMode: continuable
    agentOptions:
      provider: remote-stack
      model: qwen38-27b-unc
```

效果：主代理（脑子，云端旗舰）只出编排指令和短摘要，所有大额代码生成/文件操作
由本地 27B（手）完成 —— 实测一个 10 子任务的会话可省 70-85% 的旗舰 API 费用。
前提：交给手的任务要机械可验证，且结果要回给脑子复核。

**pi**：多 provider 配置见 `clients/pi-models.json.example`，`--model` 切换。

## 6. 排障速查

| 症状 | 检查 |
|---|---|
| 请求 502 "启动超时" | `logs/gatekeeper_*.log`；若报"另一槽占卡"= 对方还在忙（闲置<60s），稍等重试；对方闲置超阈值会自动让位 |
| 模型永不停机 | netstat 输出编码（GBK）解析失败 —— 本仓库已修，别用 text=True |
| 冷启动时请求报错 | 引擎在装，等 30 秒重发即可（已唤醒） |
| 公网首请求 20 秒+ | 连接器还在 QUIC，切 http2 |
| 公网长请求 100 秒断 | 老版本 shim 无 SSE 心跳，本仓库已修 |
