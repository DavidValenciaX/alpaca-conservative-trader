# Conservative US Equity Trading Bot

An **autonomous, conservative** trading bot for US equities and ETFs, built on the Alpaca API. It uses a mean-reversion strategy to buy oversold assets and sell when they revert to the mean — prioritizing capital preservation over high-risk gains.

> ⚠️ **WARNING**: This software is for **educational purposes only**. Trading real money carries substantial financial risk. Past performance does not guarantee future results. The authors are not financial advisors. **Use at your own risk.**

---

## Project Structure

```
trading_bot/
├── main.py          # Entry point, event loop, scheduler
├── config.py        # Environment variables & strategy parameters
├── data_feed.py     # Market data from Alpaca (historical + real-time)
├── strategy.py      # Mean-reversion signal generation
├── risk_manager.py  # Hard risk limits (gatekeeper before every order)
├── executor.py      # Order placement, modification, cancellation
├── portfolio.py     # Position & P&L tracking
├── logger.py        # Loguru setup (console + rotating file)
├── .env.example     # Template for configuration
├── requirements.txt # Python dependencies
└── logs/            # Rotating log files (auto-created)
```

---

## Setup

### 1. Prerequisites

- Python 3.11 or later
- An [Alpaca Markets](https://alpaca.markets) account (free paper trading account is sufficient)

### 2. Clone and install

```bash
cd trading_bot
python -m venv venv
source venv/bin/activate   # On Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Configure

```bash
cp .env.example .env
```

Edit `.env` with your Alpaca API keys:

| Variable | Description |
|---|---|
| `ALPACA_API_KEY` | Your Alpaca API key (paper trading key) |
| `ALPACA_SECRET_KEY` | Your Alpaca secret key |
| `ALPACA_BASE_URL` | Paper: `https://paper-api.alpaca.markets/v2` (default) |
| `PAPER_MODE` | `true` = paper trading, `false` = **LIVE MONEY** |

All strategy and risk parameters can be tuned in `.env` — no code changes needed.

---

## Running

### Paper trading (default — SAFE)

```bash
cd trading_bot
python main.py
```

The bot will:
1. Load configuration from `.env`
2. Connect to Alpaca paper trading
3. Start the signal-check loop (every 5 min by default)
4. Log everything to console and `logs/trading_bot.log`

### Live trading ⚠️

Set `PAPER_MODE=false` in `.env`, restart the bot. **Only do this after extensive paper trading and full understanding of the risks.**

---

## Running with PM2 (recommended for production)

```bash
# Install PM2 globally
npm install -g pm2

# Create an ecosystem file (ecosystem.config.js):
cat > ecosystem.config.js << 'EOF'
module.exports = {
  apps: [{
    name: 'trading-bot',
    script: 'main.py',
    cwd: '/path/to/trading_bot',
    interpreter: 'python3',
    watch: false,
    max_memory_restart: '500M',
    log_date_format: 'YYYY-MM-DD HH:mm:ss',
    error_file: 'logs/pm2_error.log',
    out_file: 'logs/pm2_out.log',
    merge_logs: true,
    autorestart: true,
  }]
};
EOF

# Start
pm2 start ecosystem.config.js

# Save PM2 process list so it auto-starts on reboot
pm2 save
pm2 startup
```

### Running with systemd

```ini
[Unit]
Description=Trading Bot
After=network.target

[Service]
Type=simple
User=your_user
WorkingDirectory=/path/to/trading_bot
ExecStart=/path/to/trading_bot/venv/bin/python main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

---

## Trading Strategy: Conservative Multi-factor Mean Reversion

### Core idea

When an asset's price deviates significantly from its recent average, it tends to revert. The bot buys when prices are "too low" and sells when they recover.

### Indicators

- **20-period SMA** — short-term trend
- **50-period SMA** — medium-term trend (macro filter)
- **RSI(14)** — momentum / overbought-oversold
- **Bollinger Bands (20, 2.0)** — volatility envelope

### BUY signal

The default `score` mode combines independent evidence instead of requiring
every indicator to be extreme on the same bar:

- Bollinger: below middle band = 1 point; below lower band = 3
- RSI: near oversold = 1 point; oversold (`RSI < 40`) = 3
- Macro trend: price above SMA(50) = 2 points

A BUY requires at least 5/8 points, agreement from two indicator families, the
macro uptrend (enabled by default), and no existing position. This permits
either a strong Bollinger pullback or a strongly oversold RSI pullback while
retaining the trend guard. Set `BUY_SIGNAL_MODE=strict` to restore the original
BB + RSI + SMA all-at-once rule.

### SELL signal (ANY is sufficient)

1. Price **crosses above the middle Bollinger Band** (mean reversion complete)
2. **RSI > 70** (overbought — take profits early)
3. **Stop-loss hit** (automatically handled by bracket order at -2.0%)
4. **Take-profit hit** (automatically handled by bracket order at +3.5%)

### Execution timing

- Signal check runs every **5 minutes** on confirmed **15-minute bars** by default
- Market open/close is checked against **Alpaca's clock**, so **holidays and early-close (half) days** are respected automatically (falls back to a local ET-hours check if the clock API is unreachable)
- **No trading** in the first 15 minutes after market open (high volatility)
- **No trading** in the last 15 minutes before the actual market close (position risk overnight; correct even on early-close days)

---

## Risk Rules (never bypassed)

The risk manager sits between every signal and every order. If any rule is violated, the order is **blocked** and the reason is logged.

| Rule | Limit | What happens |
|---|---|---|
| **Max position size** | 7.5% of portfolio per asset | Prevents over-concentration in a single position |
| **Max total exposure** | 30% of portfolio in all positions | Caps aggregate exposure |
| **Stop-loss** | 2.0% below entry (bracket) | Caps downside per trade. Placed automatically with every order |
| **Take-profit** | 3.5% above entry (bracket) | Locks in gains automatically |
| **Max daily loss** | 3% portfolio drop in one day | Blocks new entries for the rest of the day; exits remain enabled |
| **Max consecutive losses** | 3 losing trades in a row | **2-hour cooldown** enforced. Logs a warning |
| **Buying power** | Checked before every entry | Prevents orders larger than available buying power |
| **Bracket orders** | Always | Every entry has attached stop-loss and take-profit — no naked positions |

> Values shown are the `conservador` profile defaults — the active trading
> mode (below) may replace them.

---

## Trading Modes (Risk Profiles)

The bot can switch between six risk profiles with a single name instead of
retuning parameters one by one. A mode auto-configures the risk limits and
signal aggressiveness it manages; `BOT_MODE=custom` disables profiles and
keeps every parameter coming from its own env var.

| Mode | Position ≤ | Exposure ≤ | SL / TP | Daily loss ≤ | Entry strictness |
|---|---|---|---|---|---|
| `preservacion` | 3% | 10% | 1.0% / 1.5% | 1% | Only the deepest oversold signals in an uptrend (score ≥ 6) |
| `conservador` | 7.5% | 30% | 2.0% / 3.5% | 3% | Original default tuning (score ≥ 5, uptrend required) |
| `balanceado` | 10% | 45% | 2.5% / 4.5% | 4% | Looser entries (score ≥ 4) |
| `crecimiento` | 12.5% | 60% | 3.0% / 6.0% | 5% | Uptrend no longer required |
| `agresivo` | 15% | 80% | 3.5% / 8.0% | 7% | Weak pullbacks qualify (score ≥ 3) |
| `especulativo` | 20% | 100% | 5.0% / 12.0% | 10% | Very loose entries, wide stops — expect severe drawdowns |

See `modes.py` for the full parameter set of each profile (RSI thresholds,
consecutive-loss limits, cooldowns).

### Switching modes

Set the startup mode in `.env`:

```env
BOT_MODE=conservador
```

To switch **without restarting**, point `BOT_MODE_FILE` at a text file and
write a mode name into it — the file is re-read every cycle and wins over
`BOT_MODE`:

```powershell
echo agresivo > mode.txt
```

Notes:
- A named mode **overrides** the env vars it manages (`STOP_LOSS_PCT`,
  `MAX_TOTAL_EXPOSURE_PCT`, `RSI_OVERSOLD`, …). Startup logs a warning for
  each pinned env var the mode replaces. Use `BOT_MODE=custom` to manage all
  parameters manually.
- A mode change applies to **new decisions only**: bracket orders already
  open keep their original SL/TP legs, while signal-based exits use the new
  thresholds immediately.
- English aliases also work (`preservation`, `conservative`, `balanced`,
  `growth`, `aggressive`, `speculative`).

---

## Logging & Observability

All events are logged with **loguru** to both:
- **Console** — colorized, human-readable
- **Rotating file** — `logs/trading_bot.log`, max 10 MB, kept for 7 days, auto-compressed

Logged events:
- Every signal evaluated (asset, indicators, result, reason)
- Every order placed (type, asset, quantity, price)
- Every order filled or rejected (with full rejection reason)
- Risk manager blocks (which rule, what values)
- Portfolio snapshot every hour (cash, positions, total value, daily P&L)
- System startup and shutdown

---

## Configuration Reference

All parameters live in `.env`. Here's what each controls:

### Bot Mode

| Variable | Default | Description |
|---|---|---|
| `BOT_MODE` | `custom` | Risk profile: `preservacion`, `conservador`, `balanceado`, `crecimiento`, `agresivo`, `especulativo`, or `custom` (every parameter comes from its own env var) |
| `BOT_MODE_FILE` | empty | Optional file with the mode name, re-read every cycle for hot-switching without restart |

A named mode overrides the env vars it manages (risk limits, RSI thresholds,
`BUY_MIN_SCORE`, `RSI_NEAR_OVERSOLD_MARGIN`, `REQUIRE_UPTREND`).

### Strategy

| Variable | Default | Description |
|---|---|---|
| `ASSETS` | `SPY,QQQ,GLD,IWM` | Comma-separated symbols to trade |
| `ASSETS_FILE` | empty | Optional runtime universe file, re-read every cycle |
| `SMA_SHORT` | `20` | Short SMA period |
| `SMA_LONG` | `50` | Long SMA period (macro trend filter) |
| `RSI_PERIOD` | `14` | RSI calculation period |
| `RSI_OVERSOLD` | `40` | RSI threshold for BUY signal |
| `RSI_OVERBOUGHT` | `70` | RSI threshold for SELL signal |
| `BB_PERIOD` | `20` | Bollinger Bands period |
| `BB_STD_DEV` | `1.8` | Bollinger Bands standard deviations |
| `BUY_SIGNAL_MODE` | `score` | `score` multi-factor or original `strict` rule |
| `BUY_MIN_SCORE` | `5` | Minimum scored-entry threshold (maximum 8) |
| `RSI_NEAR_OVERSOLD_MARGIN` | `10` | Width of the weaker RSI evidence band |
| `REQUIRE_UPTREND` | `true` | Require price above SMA long for scored BUY |
| `BAR_TIMEFRAME` | `15Min` | Bar size for indicators |
| `HISTORY_DAYS` | `10` | Calendar days fetched for indicator warm-up |
| `CHECK_INTERVAL_MINUTES` | `5` | How often to evaluate signals |
| `USE_CLOSED_BARS_ONLY` | `true` | Ignore the still-forming current bar |
| `USE_LIMIT_ENTRY` | `true` | `true` = limit entry (caps slippage), `false` = market entry |
| `ENTRY_LIMIT_BUFFER_PCT` | `0.25` | Buffer above signal price for BUY limit entries (%) |

### Risk

| Variable | Default | Description |
|---|---|---|
| `MAX_POSITION_SIZE_PCT` | `7.5` | Max % of portfolio per position |
| `MAX_TOTAL_EXPOSURE_PCT` | `30.0` | Max % in all positions combined |
| `STOP_LOSS_PCT` | `2.0` | Stop-loss % below entry |
| `TAKE_PROFIT_PCT` | `3.5` | Take-profit % above entry |
| `MAX_DAILY_LOSS_PCT` | `3.0` | Daily loss limit before halt |
| `MAX_CONSECUTIVE_LOSSES` | `3` | Losses before cooldown |
| `CONSECUTIVE_LOSS_COOLDOWN_MINUTES` | `120` | Cooldown duration |

### Fundamental news agent

The optional fundamental layer complements, but does not replace, the technical
strategy. It is disabled by default and is configured to `shadow` mode, where it
collects data and records assessments without changing orders. In `overlay` mode
it can only veto a new technical BUY when the validated view is negative with
confidence at least `FUNDAMENTAL_VETO_CONFIDENCE` or when event risk is `high`.
It cannot create BUY signals, size positions, change bracket prices, bypass
`RiskManager`, or affect SELL/stops/take-profits.

The first implementation uses Alpaca News for asset headlines, official BLS,
Federal Reserve and BEA RSS feeds for macro events, and optional FRED series for
structured observations. It sends only bounded headlines, summaries, metadata
and macro observations to a configurable OpenAI-compatible LLM. It does not
scrape full articles or attempt full company valuation.

| Variable | Default | Description |
|---|---|---|
| `FUNDAMENTAL_ENABLED` | `false` | Start the background fundamental worker |
| `FUNDAMENTAL_MODE` | `shadow` | `shadow` logs only; `overlay` may veto new BUYs |
| `FUNDAMENTAL_POLL_INTERVAL_MINUTES` | `15` | Ingestion cadence during the active ET window |
| `FUNDAMENTAL_MIN_INFERENCE_GAP_MINUTES` | `30` | Minimum spacing between LLM calls |
| `FUNDAMENTAL_STATE_TTL_MINUTES` | `120` | Maximum age of an assessment for overlay use |
| `FUNDAMENTAL_FAIL_OPEN` | `true` | Continue technical-only when state is stale/unavailable |
| `FUNDAMENTAL_VETO_CONFIDENCE` | `0.75` | Confidence threshold for a negative veto |
| `FUNDAMENTAL_STATE_FILE` | `logs/fundamental_state.json` | Cache of the last validated assessment |
| `FUNDAMENTAL_MAX_NEWS` | `10` | Maximum recent news items included in each prompt |
| `FUNDAMENTAL_NEWS_MAX_CHARS` | `700` | Maximum characters per news headline/summary |
| `FUNDAMENTAL_LLM_MAX_ATTEMPTS` | `2` | Maximum attempts per inference, including the first request |
| `FUNDAMENTAL_LLM_CIRCUIT_MINUTES` | `30` | Cooldown after repeated inference failure |
| `LLM_BASE_URL` | `https://api.deepseek.com` | OpenAI-compatible endpoint |
| `LLM_API_KEY` | empty | Provider key; keep it only in local `.env` |
| `LLM_MODEL` | `deepseek-v4-flash` | Configurable model name |
| `LLM_TIMEOUT_SECONDS` | `45` | Strict LLM request timeout |
| `LLM_MAX_TOKENS` | `2048` | Maximum generated JSON/reasoning tokens |
| `LLM_THINKING_ENABLED` | `false` | Enable DeepSeek reasoning mode; disabled for faster bounded classification |
| `FUNDAMENTAL_RSS_URLS` | BLS/Fed/BEA | Comma-separated official RSS/Atom URLs |
| `FRED_API_KEY` | empty | Optional free FRED key |
| `FRED_SERIES` | CPI/unemployment/rates/GDP/yields/PCE | Comma-separated FRED series |

The technical loop remains every five minutes. The fundamental worker polls
roughly every 15 minutes between 07:30 and 18:00 ET and every hour outside that
window. It infers only when news or macro data changed, the cache is stale, or a
pre-market refresh is needed; new high-relevance news is grouped with other
pending items into one inference. RSS headlines and summaries have HTML/entities
removed and whitespace normalized before they are truncated. Each prompt is bounded by
`FUNDAMENTAL_MAX_NEWS` and `FUNDAMENTAL_NEWS_MAX_CHARS`, while each LLM response
is bounded by `LLM_MAX_TOKENS`. DeepSeek thinking is disabled by default for
this short JSON classification. Provider errors, timeouts, rate limits,
malformed XML and invalid model JSON are isolated. Only transient provider
failures and empty responses are retried; a valid assessment is kept until its
TTL, then the bot continues with `technical_only` under fail-open.

At startup, the bot logs the effective LLM timeout, token limit, attempt count
and circuit duration. Each failed attempt also logs its elapsed time and
exception type, which makes an outdated VPS `.env` or deployment easy to
distinguish from provider latency. The worker status exposes in-process
attempt, success, failure and success-rate counters.

On the VPS, run log diagnostics from the deployed application directory because
the configured log path is relative to the process working directory:

```bash
cd /home/ubuntu/trading_bot
grep -E "Fundamental inference failed|LLM returned an empty response|ReadTimeout|429|400|402|422|500|503|insufficient_system_resource" \
  logs/trading_bot_$(date +%F).log | tail -n 100
```

Rollout should be shadow first, then overlay in paper trading. Review veto rate,
staleness, errors, latency and the subsequent performance of vetoed technical
signals before any manual live activation.

---

## Adding and Removing Assets

`ASSETS` remains the static fallback. For changes without restarting PM2, set:

```env
ASSETS_FILE=assets.txt
```

Then edit `assets.txt` using either commas or one symbol per line. The file is
read on every cycle. Symbols are trimmed, uppercased, and deduplicated. Removing
a symbol prevents future entries, but if that symbol is currently held the bot
continues analyzing it until the position closes.

## Fundamental and News Analysis

The fundamental worker is the implementation described above. “Fundamental” in
this first version means current macroeconomic regime and news/event context,
not a valuation model for every company held by an ETF. Missing data is never
treated as positive evidence.

---

## Module Architecture

```
main.py
  ├── config.py        → loads & validates .env
  ├── logger.py        → sets up loguru sinks
  ├── data_feed.py     → fetches bars from Alpaca
  ├── strategy.py      → computes indicators & signals
  ├── news_feed.py     → Alpaca News and official RSS adapters
  ├── macro_data.py    → optional FRED/ALFRED-compatible macro snapshots
  ├── fundamental_agent.py → strict JSON LLM analysis
  ├── fundamental_overlay.py → background worker and BUY-only veto gate
  ├── risk_manager.py  → validates orders against hard limits
  ├── executor.py      → places bracket orders via Alpaca
  └── portfolio.py     → tracks positions & account state
```

Each module is independently importable and testable. The main loop in `main.py` orchestrates the cycle:

1. Check market hours → 2. Fetch data → 3. Compute indicators → 4. Evaluate signals → 5. Risk check → 6. Execute or block

---

## Error Handling

- **API failures**: Every Alpaca call uses exponential backoff (max 3 attempts)
- **Data gaps**: If data fetch fails, that cycle is skipped — the bot does NOT crash
- **Order rejection**: Full rejection reason is logged, bot continues operating
- **Unhandled exceptions**: The main loop catches everything, logs the stack trace, and **restarts** — the process never dies
- **Shutdown**: SIGINT/SIGTERM triggers graceful shutdown with a final portfolio snapshot

---

## Legal & Disclaimer

**This software is provided for educational and research purposes only.**

- Trading stocks, ETFs, and forex involves substantial risk of loss
- Past performance of any strategy is not indicative of future results
- You are solely responsible for any financial losses incurred
- Always test thoroughly in paper trading mode before considering live trading
- Consult a qualified financial advisor before making investment decisions

**No warranty, express or implied, is provided. Use entirely at your own risk.**
