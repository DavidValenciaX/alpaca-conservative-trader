"""
main.py — Entry point for the autonomous conservative trading bot.

Starts the event loop and scheduler. Runs signal checks on a configurable
interval and manages the full trade lifecycle: data → signal → risk check →
execution → monitoring.

Designed to run as a long-lived process (compatible with PM2 or systemd).
Never exits on error — restarts the loop on unhandled exceptions.
"""

from __future__ import annotations

import signal
import sys
import time
from datetime import datetime, time as dtime

import pytz
import schedule

from config import load_config
from data_feed import DataFeed
from executor import OrderExecutor
from logger import get_logger, setup_logger
from portfolio import PortfolioTracker
from risk_manager import RiskBlock, RiskManager
from strategy import MeanReversionStrategy, Signal

log = get_logger()

# ── Constants ───────────────────────────────────────────────────────────

ET = pytz.timezone("US/Eastern")
MARKET_OPEN = dtime(9, 30)  # Eastern
MARKET_CLOSE = dtime(16, 0)  # Eastern
NO_TRADE_OPEN_BUFFER = 15  # minutes after open
NO_TRADE_CLOSE_BUFFER = 15  # minutes before close

# ── Global state ────────────────────────────────────────────────────────

_shutdown_requested = False


def _handle_shutdown(signum: int, _frame) -> None:
    """Capture SIGINT/SIGTERM and gracefully shut down."""
    global _shutdown_requested
    _shutdown_requested = True
    log.warning(f"Received signal {signum}. Shutting down gracefully...")


def _is_market_open() -> bool:
    """Check if we are within regular market hours (no extended hours)."""
    now_et = datetime.now(ET)
    current_time = now_et.time()

    # Weekend check — no trading on Saturday or Sunday
    if now_et.weekday() >= 5:
        return False

    # Before open or after close
    if current_time < MARKET_OPEN or current_time >= MARKET_CLOSE:
        return False

    return True


def _is_in_no_trade_window() -> bool:
    """Check if we are in the first or last 15 minutes of the trading day."""
    now_et = datetime.now(ET)
    current_time = now_et.time()

    open_seconds = (
        current_time.hour * 3600 + current_time.minute * 60 + current_time.second
    )
    open_start = MARKET_OPEN.hour * 3600 + MARKET_OPEN.minute * 60
    close_start = MARKET_CLOSE.hour * 3600 + MARKET_CLOSE.minute * 60

    # First 15 minutes after open
    no_trade_end = open_start + NO_TRADE_OPEN_BUFFER * 60
    if open_start <= open_seconds < no_trade_end:
        return True

    # Last 15 minutes before close
    no_trade_start = close_start - NO_TRADE_CLOSE_BUFFER * 60
    if no_trade_start <= open_seconds < close_start:
        return True

    return False


def _log_portfolio_snapshot(portfolio: PortfolioTracker) -> None:
    """Fetch and log a snapshot of the current portfolio."""
    try:
        snapshot = portfolio.get_snapshot()
        log.info(f"HOURLY SNAPSHOT: {snapshot.summary()}")
    except Exception as e:
        log.error(f"Failed to log portfolio snapshot: {e}")


# ── Main trading cycle ─────────────────────────────────────────────────


def run_trading_cycle(
    data_feed: DataFeed,
    strategy: MeanReversionStrategy,
    risk_manager: RiskManager,
    executor: OrderExecutor,
    portfolio: PortfolioTracker,
    assets: list[str],
) -> None:
    """
    One complete trading cycle:
      1. Check market status
      2. Fetch data
      3. Compute indicators
      4. Evaluate signals for each asset
      5. Risk-check and execute BUY/SELL
    """
    global _shutdown_requested

    if _shutdown_requested:
        return

    # ── Market gate ──────────────────────────────────────────────────
    if not _is_market_open():
        log.debug("Market closed — skipping cycle")
        return

    if _is_in_no_trade_window():
        log.info("Inside no-trade window (first/last 15 min) — skipping")
        return

    # ── Check risk manager global gate ────────────────────────────────
    if not risk_manager.can_trade:
        log.info("Risk manager blocked trading — skipping cycle")
        return

    # ── Fetch data ───────────────────────────────────────────────────
    log.info("=== Starting trading cycle ===")
    df = data_feed.get_historical_bars(symbols=assets, days_back=10)
    if df.empty:
        log.warning("No data returned — skipping cycle")
        return

    # ── Compute indicators per symbol ────────────────────────────────
    symbols_with_indicators = {}
    for symbol in assets:
        try:
            symbol_df = df.xs(symbol, level="symbol").copy()
            if symbol_df.empty:
                continue
            ind_df = strategy.compute_indicators(symbol_df)
            if ind_df.empty:
                continue
            symbols_with_indicators[symbol] = ind_df
        except KeyError:
            log.warning(f"No data available for {symbol}")
            continue

    if not symbols_with_indicators:
        log.warning("No indicator data computed for any asset — skipping")
        return

    # ── Evaluate signals ─────────────────────────────────────────────
    positions_snapshot = portfolio.get_snapshot()
    portfolio_value = positions_snapshot.portfolio_value
    buying_power = positions_snapshot.buying_power
    existing_positions = {
        p.symbol: {
            "qty": p.qty,
            "avg_entry": p.avg_entry_price,
            "market_value": p.market_value,
        }
        for p in positions_snapshot.positions
    }

    for symbol, ind_df in symbols_with_indicators.items():
        if _shutdown_requested:
            break

        latest_row = ind_df.iloc[-1]
        has_position = symbol in existing_positions

        # Evaluate
        result = strategy.evaluate(symbol, latest_row, has_position)
        log.info(result.to_log())

        if result.signal == Signal.BUY:
            _handle_buy_signal(
                symbol=symbol,
                price=result.price,
                portfolio_value=portfolio_value,
                buying_power=buying_power,
                existing_positions=existing_positions,
                risk_manager=risk_manager,
                executor=executor,
                portfolio=portfolio,
            )
        elif result.signal == Signal.SELL and has_position:
            _handle_sell_signal(
                symbol=symbol,
                executor=executor,
                portfolio=portfolio,
                risk_manager=risk_manager,
            )

    log.info("=== Trading cycle complete ===")


def _handle_buy_signal(
    symbol: str,
    price: float,
    portfolio_value: float,
    buying_power: float,
    existing_positions: dict,
    risk_manager: RiskManager,
    executor: OrderExecutor,
    portfolio: PortfolioTracker,
) -> None:
    """Process a BUY signal: size position, validate risk, place bracket order."""
    # Determine position size: 5% of portfolio / price
    position_size_pct = risk_manager._cfg.max_position_size_pct / 100.0
    order_value = portfolio_value * position_size_pct
    quantity = round(order_value / price, 4)

    if quantity <= 0:
        log.warning(f"Calculated quantity <= 0 for {symbol} — skipping")
        return

    # Round to integer shares for stocks (Alpaca requires whole shares for most)
    quantity = max(1, int(quantity))

    try:
        risk_manager.validate_order(
            symbol=symbol,
            quantity=quantity,
            estimated_price=price,
            portfolio_value=portfolio_value,
            existing_positions=existing_positions,
            buying_power=buying_power,
        )
    except RiskBlock as rb:
        log.warning(f"RISK BLOCK: {rb}")
        return

    # Get bracket prices
    stop_price, take_profit_price = risk_manager.get_bracket_prices(
        entry_price=price, side="BUY"
    )

    # Place order
    order = executor.place_bracket_order(
        symbol=symbol,
        quantity=quantity,
        side="BUY",
        entry_price=price,
        stop_loss_price=stop_price,
        take_profit_price=take_profit_price,
    )

    if order:
        log.info(f"Position opened: {quantity} {symbol} @ ${price:.4f}")
    else:
        log.warning(f"Failed to open position for {symbol}")


def _handle_sell_signal(
    symbol: str,
    executor: OrderExecutor,
    portfolio: PortfolioTracker,
    risk_manager: RiskManager,
) -> None:
    """Process a SELL signal: close the position."""
    position = portfolio.get_position(symbol)
    if not position:
        log.warning(f"SELL signal for {symbol} but no position found")
        return

    entry_price = position.avg_entry_price
    current_price = position.current_price
    pl_pct = ((current_price - entry_price) / entry_price) * 100

    log.info(
        f"Closing {symbol}: entry=${entry_price:.4f}, "
        f"current=${current_price:.4f}, P&L={pl_pct:.2f}%"
    )

    success = executor.close_position(symbol)
    if success:
        if pl_pct < 0:
            risk_manager.record_losing_trade()
        else:
            risk_manager.record_winning_trade()
    else:
        log.warning(f"Failed to close position for {symbol}")


# ── Main ────────────────────────────────────────────────────────────────


def main() -> None:
    """Initialize and run the trading bot."""
    # ── Load config and setup logging ────────────────────────────────
    try:
        config = load_config()
    except (RuntimeError, ValueError) as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        sys.exit(1)

    setup_logger(config.log_level)
    log.info("=" * 60)
    log.info("TRADING BOT STARTING")
    log.info(f"Paper mode: {config.paper_mode}")
    log.info(f"Assets: {config.strategy.assets}")
    log.info(f"Check interval: {config.strategy.check_interval_minutes} min")
    log.info("=" * 60)

    # ── Initialize components ─────────────────────────────────────────
    data_feed = DataFeed(config)
    strategy = MeanReversionStrategy(config)
    risk_manager = RiskManager(config)
    executor = OrderExecutor(config)
    portfolio_tracker = PortfolioTracker(config)

    assets = config.strategy.assets

    # ── Register signal handlers for graceful shutdown ────────────────
    signal.signal(signal.SIGINT, _handle_shutdown)
    signal.signal(signal.SIGTERM, _handle_shutdown)

    # ── Schedule jobs ────────────────────────────────────────────────
    interval_minutes = config.strategy.check_interval_minutes

    # Schedule the trading cycle to run at the configured interval
    schedule.every(interval_minutes).minutes.do(
        run_trading_cycle,
        data_feed=data_feed,
        strategy=strategy,
        risk_manager=risk_manager,
        executor=executor,
        portfolio=portfolio_tracker,
        assets=assets,
    )

    # Schedule hourly portfolio snapshot
    schedule.every(60).minutes.do(
        _log_portfolio_snapshot, portfolio=portfolio_tracker
    )

    # Run one cycle immediately on startup
    log.info("Running initial trading cycle...")
    try:
        run_trading_cycle(
            data_feed=data_feed,
            strategy=strategy,
            risk_manager=risk_manager,
            executor=executor,
            portfolio=portfolio_tracker,
            assets=assets,
        )
    except Exception as e:
        log.error(f"Initial cycle failed: {e}")

    # ── Main loop ────────────────────────────────────────────────────
    log.info(
        f"Entering main loop. Checking every {interval_minutes} minute(s)."
    )

    while not _shutdown_requested:
        try:
            schedule.run_pending()
            time.sleep(1)
        except Exception as e:
            log.error(f"Unhandled exception in main loop: {e}")
            log.exception("Stack trace:")
            log.info("Restarting main loop...")
            time.sleep(5)

    # ── Graceful shutdown ────────────────────────────────────────────
    log.info("Shutting down...")
    try:
        snapshot = portfolio_tracker.get_snapshot()
        log.info(f"FINAL SNAPSHOT: {snapshot.summary()}")
    except Exception:
        pass
    log.info("Trading bot stopped.")
    sys.exit(0)


if __name__ == "__main__":
    main()
