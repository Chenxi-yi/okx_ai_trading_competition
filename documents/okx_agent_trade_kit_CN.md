# OKX Agent Trade Kit — 完整参考指南

> 版本 1.2.7 | GitHub: https://github.com/okx/agent-trade-kit

---

## 它是什么？

OKX Agent Trade Kit 是一个 **Node.js 执行层工具，负责在 OKX 上下单，并自动给每一笔订单打上 broker 标签**，OKX 后台会将该标签映射为账单记录中的 `"agentTradeKit"`。

工具包含两个 npm 包，底层逻辑相同：

| 包名 | 用途 |
|---|---|
| `@okx_ai/okx-trade-cli` | 终端命令或 Python subprocess 调用 — **不需要大模型** |
| `@okx_ai/okx-trade-mcp` | 供 AI Agent 使用的 MCP 服务器（Claude、Cursor 等） |

**标签在执行层无条件注入，与信号来源无关。** 不管是你自己的 Python 量化策略、机器学习模型还是人工逻辑，只要调用 CLI 下单，标签就会自动附上。

### 它不是什么

- **不是**信号生成器 — 内部没有任何交易策略
- **不是**机器学习模型 — 不预测价格
- **不提供**清算热力图数据或情绪数据 — 这些是需要外部接入的工具
- **不需要** AI Agent — 完全可以通过 Python subprocess 纯程序化使用

---

## 安装

```bash
npm install -g @okx_ai/okx-trade-cli @okx_ai/okx-trade-mcp
```

运行要求：Node.js ≥ 18

---

## 身份认证

创建配置文件 `~/.okx/config.toml`：

```toml
default_profile = "demo"

[demo]
api_key    = "你的模拟盘 API Key"
secret_key = "你的模拟盘 Secret Key"
passphrase = "你的模拟盘 Passphrase"
demo       = true

[live]
api_key    = "你的实盘 API Key"
secret_key = "你的实盘 Secret Key"
passphrase = "你的实盘 Passphrase"
```

或使用交互式向导：
```bash
okx config init
```

验证连接：
```bash
okx diagnose --all
```

---

## 功能一览

工具覆盖 **OKX 全部交易功能**，共 12 个模块、约 107 个工具：

| 模块 | 功能描述 |
|---|---|
| **market（市场数据）** | 价格、K 线、订单簿、资金费率、未平仓量、20+ 指标（RSI、MACD、BB、EMA…）— **无需登录** |
| **account（账户）** | 余额、持仓、成交记录、手续费、账户间划转 |
| **swap（永续合约）** | 下单/撤单/改单、设置杠杆、平仓、附带止盈止损、批量下单 |
| **spot（现货）** | 与 swap 相同，适用于现货市场 |
| **futures（交割合约）** | 交割合约下单 |
| **option（期权）** | 期权下单、希腊值查询 |
| **bot.grid（网格机器人）** | 创建/停止/监控网格交易 |
| **bot.dca（定投机器人）** | 创建/停止/监控 DCA 策略 |
| **earn（理财）** | 简单赚币、双币投资、链上质押、自动理财 |

### 内置技术指标（`okx market indicator`）

`ma`、`ema`、`rsi`、`macd`、`bb`（布林带）、`kdj`、`supertrend`、`halftrend`、`alphatrend`、`stoch-rsi`、`qqe` 等，无需额外编写指标计算代码，直接 CLI 查询。

---

## 使用方式 A — 纯程序化调用（无需大模型）

这是我们当前的架构。Python 引擎生成信号，CLI 负责执行。

### 快速验证

```bash
okx market ticker BTC-USDT-SWAP            # 无需登录
okx --profile demo account balance          # 需要配置认证
```

### Python subprocess 调用模板

```python
import subprocess
import json

def okx_place_swap(inst_id, side, sz, pos_side, td_mode="cross", profile="demo"):
    """
    通过 Agent Trade Kit CLI 下永续合约订单。
    "agentTradeKit" 标签自动注入，无需手动添加。
    """
    cmd = [
        "okx", "--profile", profile, "--json",
        "swap", "place",
        "--instId",   inst_id,    # 例如 "BTC-USDT-SWAP"
        "--side",     side,       # "buy" | "sell"
        "--ordType",  "market",
        "--sz",       str(sz),    # 单位：张（合约数）
        "--posSide",  pos_side,   # "long" | "short"
        "--tdMode",   td_mode,    # "cross" | "isolated"
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"下单失败: {result.stderr or result.stdout}")
    return json.loads(result.stdout)

# 示例：做多 1 张 BTC
order = okx_place_swap("BTC-USDT-SWAP", "buy", 1, "long")
print(order)
```

### 常用 CLI 命令

```bash
# 行情查询
okx market ticker BTC-USDT-SWAP
okx market orderbook BTC-USDT-SWAP --sz 10
okx market candles BTC-USDT-SWAP --bar 1H --limit 100
okx market funding-rate BTC-USDT-SWAP
okx market indicator rsi BTC-USDT-SWAP --bar 1H --params 14

# 账户查询
okx --profile demo account balance
okx --profile demo account positions

# 市价开仓（做多）
okx --profile demo swap place --instId BTC-USDT-SWAP --side buy \
    --ordType market --sz 1 --posSide long --tdMode cross

# 限价单 + 止盈止损
okx --profile demo swap place --instId BTC-USDT-SWAP --side buy \
    --ordType limit --px 65000 --sz 1 --posSide long --tdMode cross \
    --tpTriggerPx 70000 --tpOrdPx -1 \
    --slTriggerPx 62000 --slOrdPx -1

# 平仓
okx --profile demo swap close --instId BTC-USDT-SWAP \
    --mgnMode cross --posSide long

# 设置杠杆
okx --profile demo swap set-leverage --instId BTC-USDT-SWAP \
    --lever 3 --mgnMode cross

# 撤单
okx --profile demo swap cancel --instId BTC-USDT-SWAP --ordId <订单ID>

# 查询当前挂单
okx --profile demo swap orders --instId BTC-USDT-SWAP --status open
```

### instId 格式说明

| 合约类型 | 格式 | 示例 |
|---|---|---|
| 永续合约 | `标的-USDT-SWAP` | `BTC-USDT-SWAP` |
| 现货 | `标的-USDT` | `BTC-USDT` |
| 交割合约 | `标的-USDT-到期日` | `BTC-USDT-250627` |

### sz（合约张数）说明

OKX 永续合约的 `sz` 是**张数**，不是币的数量：
- BTC-USDT-SWAP：1 张 = 0.01 BTC
- ETH-USDT-SWAP：1 张 = 0.1 ETH
- SOL-USDT-SWAP：1 张 = 1 SOL

```python
def 币量转张数(exchange, symbol, 数量_以币计):
    """将币的数量转换为 OKX 合约张数"""
    market = exchange.market(f"{symbol}:USDT")
    ct_val = float(market.get("contractSize", 1))
    return max(1, round(数量_以币计 / ct_val))
```

### 批量下单

```bash
okx --profile demo swap batch --action place --orders '[
  {"instId":"BTC-USDT-SWAP","side":"buy","ordType":"market","sz":"1","posSide":"long","tdMode":"cross"},
  {"instId":"ETH-USDT-SWAP","side":"buy","ordType":"market","sz":"5","posSide":"long","tdMode":"cross"}
]'
```

---

## 使用方式 B — 配合 AI Agent（Skill.md 模式）

这是比赛官方设计的"AI Skills"模式 — 你编写一个 Skill.md 文件，指导 Claude 或其他大模型何时、如何交易。Agent Trade Kit MCP 给大模型提供所有交易工具的访问权限。

### 第一步：将 MCP 服务器注册到 Claude Code

在 Claude Desktop 配置中添加：
```json
{
  "mcpServers": {
    "okx-demo": {
      "command": "npx",
      "args": ["-y", "@okx_ai/okx-trade-mcp", "--profile", "demo"]
    }
  }
}
```

或自动注册：
```bash
okx setup --client claude-desktop
```

### 第二步：编写 Skill.md（给 AI Agent 的系统提示）

这是"策略即提示词"的方式。AI 读取 Skill.md，解读市场数据，调用 MCP 工具执行。

```markdown
# 我的交易策略

## 第一步 — 数据采集
- 获取 BTC-USDT-SWAP 1小时 K 线（最近 100 根）
- 获取 RSI(14) 1小时数据
- 获取当前资金费率

## 第二步 — 信号判断
- 若 RSI < 30 且资金费率 < -0.01%：强烈做多信号
- 若 RSI > 70 且资金费率 > 0.01%：强烈做空信号
- 其他情况：持仓不动 / 空仓等待

## 第三步 — 执行
- 仓位大小：50 USDT 名义价值
- 杠杆：全仓 3 倍
- 通过 swap_place_order 下市价单
- 附带止损：入场价下方 3%（slOrdPx = -1 表示市价止损）

## 第四步 — 风控规则
- 同时最多持有 1 个仓位
- 回撤超过 10%：清仓，停止交易
- 每次开仓前先检查持仓状态
```

### Skill.md vs 纯代码 — 什么时候用哪个？

| 场景 | 推荐方式 |
|---|---|
| 量化算法，信号逻辑精确明确 | **纯代码 → CLI subprocess** |
| 需要 AI 解读定性信号（新闻、情绪、图形形态） | **Skill.md + MCP** |
| 需要可回测、可复现的确定性行为 | **纯代码** |
| 比赛需要提交 Skills 文件 | **Skill.md**（比赛提交必须） |
| 追求最低延迟执行 | **纯代码** |

**本次比赛的建议：用纯代码执行策略，写 Skill.md 用于提交。** Skill.md 是交给评委看的，代码是真正跑的。

---

## 重要实现细节

1. **模拟盘 vs 实盘**：传 `--profile demo` 使用 OKX 模拟盘（模拟交易）；传 `--profile live` 使用真实资金。

2. **持仓模式设置**：首次交易前需设置为买卖模式（net）：
   ```bash
   okx --profile demo account set-position-mode net
   ```

3. **关于标签**：永远不需要手动添加标签，CLI/MCP 总是自动注入。如果直接调用 OKX API（如通过 ccxt），标签**不会**被添加 — 这类订单不会计入比赛得分。

4. **退出码**：`returncode == 0` 代表成功。即使 HTTP 200，批量操作中任何一笔失败都会返回 1，务必检查。

5. **频率限制**：私有接口 10 次/秒；公开行情 20 次/2秒。

6. **OKX 模拟盘**：OKX 模拟盘不是独立的 testnet URL（不像 Binance），而是同一套 API，通过 config 中的 `demo: true` 标记区分。使用模拟盘密钥时无需额外配置。

---

## 我们的架构总结

```
Python 信号引擎（你的策略逻辑）
        │
        ▼
  信号：做多 BTC，1 张，3 倍杠杆
        │
        ▼
subprocess.run(["okx", "--profile", "demo", "swap", "place", ...])
        │
        ▼
  OKX 交易所 ← 订单已打上 "agentTradeKit" 标签 ✓
```

subprocess 调用之前的所有逻辑都由你控制。CLI 负责身份认证、API 格式化、频率限制和标签注入。

---

## 常见问题

**Q：信号必须由 AI 生成才能打上标签吗？**
A：不需要。标签是 CLI/MCP 在执行层自动注入的，与信号来源无关。

**Q：可以通过 ccxt 直接调用 OKX API 并手动加 tag 吗？**
A：技术上可以写入 tag 字段，但该字段是 OKX 的 broker 注册码，不是你随意设置的字符串。只有通过 Agent Trade Kit（CLI 或 MCP）下的单，才会被映射为 "agentTradeKit"。

**Q：Quadcode 清算热力图和情绪数据是 Agent Trade Kit 内置的吗？**
A：不是。这些是外部工具，需要单独的 API 接入。Agent Trade Kit 只负责执行，不提供第三方数据。

**Q：比赛 Skill.md 提交的是代码还是提示词？**
A：提示词（Markdown 格式的自然语言指令集）。代码跑在你的服务器上，Skill.md 是展示你的 AI 交易思路给评委看的文件。
