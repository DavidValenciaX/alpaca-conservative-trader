# Conservative Forex/Stock Trading Bot

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
| `ALPACA_BASE_URL` | Paper: `https://paper-api.alpaca.markets` (default) |
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
3. Start the signal-check loop (every 15 min by default)
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

## Trading Strategy: Conservative Mean Reversion

### Core idea

When an asset's price deviates significantly from its recent average, it tends to revert. The bot buys when prices are "too low" and sells when they recover.

### Indicators

- **20-period SMA** — short-term trend
- **50-period SMA** — medium-term trend (macro filter)
- **RSI(14)** — momentum / overbought-oversold
- **Bollinger Bands (20, 2.0)** — volatility envelope

### BUY signal (ALL must be true)

1. Price is **below the lower Bollinger Band** (oversold on a volatility basis)
2. **RSI < 35** (oversold on a momentum basis)
3. Price is **above the 50-period SMA** (macro uptrend intact — we don't catch falling knives)
4. **No open position** in this asset

### SELL signal (ANY is sufficient)

1. Price **crosses above the middle Bollinger Band** (mean reversion complete)
2. **RSI > 65** (overbought — take profits early)
3. **Stop-loss hit** (automatically handled by bracket order at -1.5%)
4. **Take-profit hit** (automatically handled by bracket order at +2.5%)

### Execution timing

- Signal check runs every **15 minutes** during market hours
- **No trading** in the first 15 minutes after market open (high volatility)
- **No trading** in the last 15 minutes before market close (position risk overnight)

---

## Risk Rules (never bypassed)

The risk manager sits between every signal and every order. If any rule is violated, the order is **blocked** and the reason is logged.

| Rule | Limit | What happens |
|---|---|---|
| **Max position size** | 5% of portfolio per asset | Prevents over-concentration in a single position |
| **Max total exposure** | 20% of portfolio in all positions | Ensures 80% remains in cash — no margin needed |
| **Stop-loss** | 1.5% below entry (bracket) | Caps downside per trade. Placed automatically with every order |
| **Take-profit** | 2.5% above entry (bracket) | Locks in gains automatically |
| **Max daily loss** | 3% portfolio drop in one day | **Halts all trading** for the rest of the day to prevent tilt |
| **Max consecutive losses** | 3 losing trades in a row | **2-hour cooldown** enforced. Logs a warning |
| **No margin/leverage** | Cash account only | Bot validates buying power before every order |
| **Bracket orders** | Always | Every entry has attached stop-loss and take-profit — no naked positions |

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

### Strategy

| Variable | Default | Description |
|---|---|---|
| `ASSETS` | `SPY,QQQ,GLD,IWM` | Comma-separated symbols to trade |
| `SMA_SHORT` | `20` | Short SMA period |
| `SMA_LONG` | `50` | Long SMA period (macro trend filter) |
| `RSI_PERIOD` | `14` | RSI calculation period |
| `RSI_OVERSOLD` | `35` | RSI threshold for BUY signal |
| `RSI_OVERBOUGHT` | `65` | RSI threshold for SELL signal |
| `BB_PERIOD` | `20` | Bollinger Bands period |
| `BB_STD_DEV` | `2.0` | Bollinger Bands standard deviations |
| `BAR_TIMEFRAME` | `15Min` | Bar size for indicators |
| `CHECK_INTERVAL_MINUTES` | `15` | How often to evaluate signals |
| `USE_LIMIT_ENTRY` | `true` | `true` = limit entry (caps slippage), `false` = market entry |
| `ENTRY_LIMIT_BUFFER_PCT` | `0.1` | Buffer above signal price for BUY limit entries (%) |

### Risk

| Variable | Default | Description |
|---|---|---|
| `MAX_POSITION_SIZE_PCT` | `5.0` | Max % of portfolio per position |
| `MAX_TOTAL_EXPOSURE_PCT` | `20.0` | Max % in all positions combined |
| `STOP_LOSS_PCT` | `1.5` | Stop-loss % below entry |
| `TAKE_PROFIT_PCT` | `2.5` | Take-profit % above entry |
| `MAX_DAILY_LOSS_PCT` | `3.0` | Daily loss limit before halt |
| `MAX_CONSECUTIVE_LOSSES` | `3` | Losses before cooldown |
| `CONSECUTIVE_LOSS_COOLDOWN_MINUTES` | `120` | Cooldown duration |

---

## Module Architecture

```
main.py
  ├── config.py        → loads & validates .env
  ├── logger.py        → sets up loguru sinks
  ├── data_feed.py     → fetches bars from Alpaca
  ├── strategy.py      → computes indicators & signals
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
