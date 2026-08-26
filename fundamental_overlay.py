"""Background fundamental service and conservative technical overlay.

The service owns ingestion and LLM work in a daemon thread.  The trading loop
only reads a short-lived, validated assessment through :meth:`check_buy`; it
never waits for a provider and never gets an order-writing capability.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional, Sequence
from zoneinfo import ZoneInfo

from config import AppConfig
from fundamental_agent import (
    FundamentalAgent,
    FundamentalAssessment,
    OpenAICompatibleClient,
)
from fundamental_types import FundamentalAgentError
from logger import get_logger
from macro_data import FredMacroData, MacroSnapshot
from news_feed import (
    AlpacaNewsSource,
    NewsItem,
    NewsSource,
    RssNewsSource,
    deduplicate_news,
)

log = get_logger()


@dataclass(frozen=True)
class FundamentalDecision:
    """Result of checking whether a fundamental layer may veto a BUY."""

    allowed: bool
    reason: str
    status: str
    stance: str = ""
    confidence: float = 0.0
    event_risk: str = "none"


def _utc(value: Optional[datetime]) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except (TypeError, ValueError):
        return None


class FundamentalService:
    """Ingest, infer and cache fundamental context without blocking trading."""

    def __init__(
        self,
        config: AppConfig,
        sources: Optional[Sequence[NewsSource]] = None,
        macro_source: Optional[object] = None,
        agent: Optional[FundamentalAgent] = None,
        now_fn: Optional[Callable[[], datetime]] = None,
        sleep_fn: Optional[Callable[[float], None]] = None,
    ) -> None:
        self._config = config
        self._fundamental = config.fundamental
        self._assets = tuple(
            str(symbol).strip().upper()
            for symbol in config.strategy.assets
            if str(symbol).strip()
        )
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self._sleep_fn = sleep_fn or time.sleep
        self._state_path = Path(self._fundamental.state_file)
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._state_lock = threading.RLock()
        self._inference_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

        self._assessment: Optional[FundamentalAssessment] = None
        self._last_fetch_at: Optional[datetime] = None
        self._last_inference_at: Optional[datetime] = None
        self._last_macro_fingerprint = ""
        self._seen_ids: set[str] = set()
        self._recent_news: Dict[str, NewsItem] = {}
        self._pending_news: Dict[str, NewsItem] = {}
        self._pending_macro_change = False
        self._technical_context: Dict[str, Any] = {}
        self._source_failures: Dict[str, int] = {}
        self._source_circuit_until: Dict[str, datetime] = {}
        self._inference_circuit_until: Optional[datetime] = None

        self._sources = list(sources) if sources is not None else self._build_sources()
        self._macro_source = macro_source or FredMacroData(
            api_key=getattr(self._fundamental, "fred_api_key", ""),
            timeout_seconds=getattr(self._fundamental, "llm_timeout_seconds", 45),
        )
        self._agent = agent or self._build_agent()
        self._load_state()

    def _build_sources(self) -> list[NewsSource]:
        result: list[NewsSource] = []
        try:
            result.append(AlpacaNewsSource(self._config))
        except Exception as exc:
            log.warning(f"Alpaca news source unavailable: {exc}")
        try:
            result.append(
                RssNewsSource(
                    getattr(self._fundamental, "rss_urls", ()),
                    timeout_seconds=getattr(
                        self._fundamental, "llm_timeout_seconds", 45
                    ),
                )
            )
        except Exception as exc:
            log.warning(f"RSS news source unavailable: {exc}")
        return result

    def _build_agent(self) -> Optional[FundamentalAgent]:
        try:
            client = OpenAICompatibleClient(
                api_key=getattr(self._fundamental, "llm_api_key", ""),
                base_url=getattr(self._fundamental, "llm_base_url", ""),
                model=getattr(self._fundamental, "llm_model", ""),
                timeout_seconds=getattr(
                    self._fundamental, "llm_timeout_seconds", 45
                ),
                max_tokens=getattr(self._fundamental, "llm_max_tokens", 2048),
                thinking_enabled=getattr(
                    self._fundamental, "llm_thinking_enabled", False
                ),
            )
            return FundamentalAgent(
                client,
                self._assets,
                getattr(self._fundamental, "llm_model", ""),
                max_news=getattr(self._fundamental, "max_news", 10),
                news_max_chars=getattr(
                    self._fundamental, "news_max_chars", 700
                ),
            )
        except Exception as exc:
            log.warning(f"Fundamental LLM agent unavailable: {exc}")
            return None

    def _load_state(self) -> None:
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        try:
            assessment_raw = raw.get("assessment")
            if assessment_raw:
                generated_at = _parse_datetime(assessment_raw.get("generated_at"))
                self._assessment = FundamentalAssessment.from_dict(
                    assessment_raw, self._assets, generated_at=generated_at
                )
        except Exception as exc:
            log.warning(f"Ignoring invalid fundamental state assessment: {exc}")
        self._last_fetch_at = _parse_datetime(raw.get("last_fetch_at"))
        self._last_inference_at = _parse_datetime(raw.get("last_inference_at"))
        self._last_macro_fingerprint = str(raw.get("last_macro_fingerprint", ""))
        seen = raw.get("seen_ids", [])
        if isinstance(seen, list):
            self._seen_ids = {str(item) for item in seen[-1000:]}

    def _save_state(self) -> None:
        with self._state_lock:
            payload = {
                "assessment": self._assessment.to_dict() if self._assessment else None,
                "last_fetch_at": self._last_fetch_at.isoformat()
                if self._last_fetch_at
                else None,
                "last_inference_at": self._last_inference_at.isoformat()
                if self._last_inference_at
                else None,
                "last_macro_fingerprint": self._last_macro_fingerprint,
                "seen_ids": sorted(self._seen_ids)[-1000:],
            }
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._state_path.with_name(self._state_path.name + ".tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            temporary.replace(self._state_path)
        except OSError as exc:
            log.warning(f"Could not persist fundamental state: {exc}")

    def start(self) -> None:
        """Start the single background worker, if it is not running."""
        with self._state_lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._worker_loop,
                name="fundamental-worker",
                daemon=True,
            )
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the worker without interrupting the technical bot."""
        self._stop_event.set()
        self._wake_event.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=timeout)

    def request_refresh(self) -> None:
        """Wake the worker for an early refresh after an external event."""
        self._wake_event.set()

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.refresh_once()
            except Exception as exc:
                log.exception(f"Fundamental refresh failed: {exc}")
            self._wake_event.wait(timeout=self._poll_seconds())
            self._wake_event.clear()

    def _poll_seconds(self) -> float:
        now_et = _utc(self._now_fn()).astimezone(ZoneInfo("America/New_York"))
        active = (
            now_et.weekday() < 5
            and (now_et.hour, now_et.minute) >= (7, 30)
            and (now_et.hour, now_et.minute) < (18, 0)
        )
        minutes = (
            getattr(self._fundamental, "poll_interval_minutes", 15)
            if active
            else 60
        )
        return max(1.0, float(minutes) * 60.0)

    def refresh_once(self, force: bool = False) -> Optional[FundamentalAssessment]:
        """Perform one isolated provider poll and, when due, one inference."""
        now = _utc(self._now_fn())
        poll_minutes = max(
            1, int(getattr(self._fundamental, "poll_interval_minutes", 15))
        )
        with self._state_lock:
            since = self._last_fetch_at or now - timedelta(
                minutes=max(
                    poll_minutes,
                    int(getattr(self._fundamental, "state_ttl_minutes", 120)),
                )
            )

        collected: list[NewsItem] = []
        for source in self._sources:
            collected.extend(self._fetch_news_with_backoff(source, since, now))
        news = deduplicate_news(collected, limit=100)

        with self._state_lock:
            for item in news:
                key = item.dedupe_key
                self._recent_news[key] = item
                if key not in self._seen_ids:
                    self._pending_news[key] = item
                    self._seen_ids.add(key)
            if len(self._recent_news) > 200:
                keep = sorted(
                    self._recent_news.values(),
                    key=lambda item: item.updated_at,
                    reverse=True,
                )[:200]
                self._recent_news = {item.dedupe_key: item for item in keep}

        macro = MacroSnapshot({}, now, available=False)
        try:
            macro = self._macro_source.fetch_snapshot(
                getattr(self._fundamental, "fred_series", ())
            )
        except Exception as exc:
            log.warning(f"Fundamental macro source failed: {exc}")
        macro_fingerprint = macro.fingerprint()
        with self._state_lock:
            if macro_fingerprint != self._last_macro_fingerprint:
                self._pending_macro_change = True
                self._last_macro_fingerprint = macro_fingerprint
            self._last_fetch_at = now
            pending_news = bool(self._pending_news)
            pending_macro = self._pending_macro_change
            stale = self._is_stale_locked(now)
            last_inference = self._last_inference_at
            due_gap = last_inference is None or (
                now - last_inference
            ).total_seconds() >= 60 * max(
                0, int(getattr(self._fundamental, "min_inference_gap_minutes", 30))
            )
            should_infer = force or due_gap and (pending_news or pending_macro or stale)
            agent = self._agent
            technical_context = dict(self._technical_context)
            context_news = sorted(
                self._recent_news.values(),
                key=lambda item: item.updated_at,
                reverse=True,
            )[: max(1, int(getattr(self._fundamental, "max_news", 10)))]

        if should_infer and agent is not None:
            if not self._inference_lock.acquire(blocking=False):
                log.debug("Fundamental inference already running; skipping overlap")
                self._save_state()
                return self._assessment
            try:
                assessment = self._run_inference_with_backoff(
                    agent, context_news, macro, technical_context, now
                )
                if assessment is None:
                    self._save_state()
                    return self._assessment
                with self._state_lock:
                    self._assessment = assessment
                    self._last_inference_at = now
                    self._pending_news.clear()
                    self._pending_macro_change = False
                self._save_state()
                log.info(
                    "Fundamental assessment refreshed: "
                    f"regime={assessment.regime}, model={assessment.model or 'configured'}"
                )
            except Exception as exc:
                log.warning(f"Fundamental inference failed; keeping prior state: {exc}")
            finally:
                self._inference_lock.release()
        elif should_infer and agent is None:
            log.debug("Fundamental inference skipped: no valid LLM client")

        self._save_state()
        return self._assessment

    def _run_inference_with_backoff(
        self,
        agent: FundamentalAgent,
        news: Sequence[NewsItem],
        macro: MacroSnapshot,
        technical_context: Dict[str, Any],
        now: datetime,
    ) -> Optional[FundamentalAssessment]:
        """Retry transient LLM failures and open a short circuit when needed."""
        if self._inference_circuit_until and now < self._inference_circuit_until:
            log.debug("Skipping fundamental inference while LLM circuit is open")
            return None
        max_attempts = max(
            1, int(getattr(self._fundamental, "llm_max_attempts", 2))
        )
        last_error: Optional[Exception] = None
        for attempt in range(1, max_attempts + 1):
            try:
                result = agent.analyze(news, macro, technical_context)
                self._inference_circuit_until = None
                return result
            except Exception as exc:
                last_error = exc
                retryable = self._is_retryable_inference_error(exc)
                log.warning(
                    f"Fundamental inference failed (attempt {attempt}/"
                    f"{max_attempts}, retryable={retryable}): {exc}"
                )
                if attempt >= max_attempts or not retryable:
                    break
                self._sleep_fn(min(10.0, 5.0 * (2.0 ** (attempt - 1))))
        circuit_minutes = max(
            1, int(getattr(self._fundamental, "llm_circuit_minutes", 30))
        )
        self._inference_circuit_until = now + timedelta(minutes=circuit_minutes)
        log.warning(
            "Fundamental LLM circuit open for "
            f"{circuit_minutes} minutes after error: {last_error}"
        )
        return None

    @staticmethod
    def _is_retryable_inference_error(exc: Exception) -> bool:
        """Retry provider/transient failures, not deterministic contract errors."""
        if isinstance(exc, FundamentalAgentError):
            return "empty response" in str(exc).lower()

        status_code = getattr(exc, "status_code", None)
        if status_code in {408, 409, 429, 500, 502, 503, 504}:
            return True
        error_name = type(exc).__name__.lower()
        return any(
            marker in error_name
            for marker in ("timeout", "connection", "connecterror")
        )

    def _fetch_news_with_backoff(
        self, source: NewsSource, since: datetime, until: datetime
    ) -> list[NewsItem]:
        """Retry transient provider errors and temporarily open a circuit."""
        key = f"{type(source).__name__}:{id(source)}"
        now = _utc(self._now_fn())
        circuit_until = self._source_circuit_until.get(key)
        if circuit_until and now < circuit_until:
            log.debug(f"Skipping open fundamental source circuit for {type(source).__name__}")
            return []

        for attempt in range(1, 4):
            try:
                result = source.fetch_since(self._assets, since, until)
                self._source_failures.pop(key, None)
                self._source_circuit_until.pop(key, None)
                return result or []
            except Exception as exc:
                log.warning(
                    f"Fundamental news source {type(source).__name__} failed "
                    f"(attempt {attempt}/3): {exc}"
                )
                if attempt < 3:
                    self._sleep_fn(min(5.0, 2.0 ** (attempt - 1)))

        failures = self._source_failures.get(key, 0) + 1
        self._source_failures[key] = failures
        if failures >= 1:
            self._source_circuit_until[key] = now + timedelta(minutes=5)
            log.warning(
                f"Fundamental source circuit open for 5 minutes: "
                f"{type(source).__name__}"
            )
        return []

    def _is_stale_locked(self, now: datetime) -> bool:
        if self._assessment is None:
            return True
        ttl = max(0, int(getattr(self._fundamental, "state_ttl_minutes", 120)))
        return (now - _utc(self._assessment.generated_at)).total_seconds() > ttl * 60

    def update_technical_context(self, result: Any, has_position: bool = False) -> None:
        """Cache the latest technical evidence for the next grouped inference."""
        symbol = str(getattr(result, "symbol", "")).strip().upper()
        if not symbol:
            return
        indicators = getattr(result, "indicators", {}) or {}
        with self._state_lock:
            self._technical_context[symbol] = {
                "signal": str(getattr(getattr(result, "signal", None), "value", "")),
                "price": float(getattr(result, "price", 0.0) or 0.0),
                "reason": str(getattr(result, "reason", ""))[:500],
                "indicators": {
                    str(key): self._context_value(value)
                    for key, value in indicators.items()
                }
                if isinstance(indicators, dict)
                else {},
                "has_position": bool(has_position),
            }

    @staticmethod
    def _context_value(value: Any) -> Any:
        """Convert common pandas/numpy scalar values to JSON-safe values."""
        if value is None or isinstance(value, (str, bool, int, float)):
            return value
        try:
            return float(value)
        except (TypeError, ValueError):
            return str(value)

    def check_buy(self, symbol: str) -> FundamentalDecision:
        """Apply the overlay veto rules to a new BUY only."""
        normalized = str(symbol).strip().upper()
        if not getattr(self._fundamental, "enabled", False):
            return FundamentalDecision(True, "fundamental layer disabled", "disabled")
        if getattr(self._fundamental, "mode", "shadow") == "shadow":
            return FundamentalDecision(True, "shadow mode; no order impact", "shadow")

        now = _utc(self._now_fn())
        with self._state_lock:
            assessment = self._assessment
            stale = self._is_stale_locked(now)
        if assessment is None or stale:
            fail_open = bool(getattr(self._fundamental, "fail_open", True))
            return FundamentalDecision(
                fail_open,
                "no fresh validated assessment",
                "technical_only" if fail_open else "fundamental_unavailable",
            )

        view = assessment.asset_views.get(normalized)
        if view is None:
            return FundamentalDecision(True, "asset has no fundamental view", "technical_only")
        if view.event_risk == "high":
            return FundamentalDecision(
                False,
                "high fundamental event risk",
                "veto",
                view.stance,
                view.confidence,
                view.event_risk,
            )
        threshold = float(getattr(self._fundamental, "veto_confidence", 0.75))
        if view.stance == "negative" and view.confidence >= threshold:
            return FundamentalDecision(
                False,
                "negative fundamental view above veto confidence",
                "veto",
                view.stance,
                view.confidence,
                view.event_risk,
            )
        return FundamentalDecision(
            True,
            "fundamental view does not veto BUY",
            "fundamental_pass",
            view.stance,
            view.confidence,
            view.event_risk,
        )

    def status(self) -> dict[str, Any]:
        """Return non-sensitive state for logs and health checks."""
        now = _utc(self._now_fn())
        with self._state_lock:
            return {
                "enabled": bool(getattr(self._fundamental, "enabled", False)),
                "mode": getattr(self._fundamental, "mode", "shadow"),
                "assessment_available": self._assessment is not None,
                "stale": self._is_stale_locked(now),
                "last_fetch_at": self._last_fetch_at.isoformat()
                if self._last_fetch_at
                else None,
                "last_inference_at": self._last_inference_at.isoformat()
                if self._last_inference_at
                else None,
                "inference_circuit_until": self._inference_circuit_until.isoformat()
                if self._inference_circuit_until
                else None,
                "pending_news": len(self._pending_news),
                "pending_macro_change": self._pending_macro_change,
            }
