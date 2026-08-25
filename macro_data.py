"""Small FRED/ALFRED adapter for structured macroeconomic context."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from logger import get_logger

log = get_logger()


DEFAULT_SERIES_LABELS = {
    "CPIAUCSL": "consumer price index",
    "UNRATE": "unemployment rate",
    "FEDFUNDS": "effective federal funds rate",
    "GDPC1": "real GDP",
    "DGS10": "10-year Treasury yield",
    "T10Y2Y": "10-year minus 2-year Treasury spread",
    "PCEPI": "personal consumption expenditures price index",
}


@dataclass(frozen=True)
class MacroObservation:
    """Latest usable observation and its previous published value."""

    series_id: str
    label: str
    value: float
    observation_date: str
    previous_value: Optional[float] = None

    @property
    def change(self) -> Optional[float]:
        if self.previous_value is None:
            return None
        return self.value - self.previous_value

    def to_dict(self) -> dict[str, Any]:
        return {
            "series_id": self.series_id,
            "label": self.label,
            "value": self.value,
            "observation_date": self.observation_date,
            "previous_value": self.previous_value,
            "change": self.change,
        }


@dataclass(frozen=True)
class MacroSnapshot:
    """Point-in-time collection of structured macro observations."""

    observations: Dict[str, MacroObservation]
    fetched_at: datetime
    available: bool = True

    def fingerprint(self) -> str:
        payload = [
            self.observations[key].to_dict()
            for key in sorted(self.observations)
        ]
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest()

    def to_context(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "fetched_at": self.fetched_at.isoformat(),
            "observations": [
                self.observations[key].to_dict()
                for key in sorted(self.observations)
            ],
        }


def parse_fred_response(
    series_id: str,
    payload: dict[str, Any],
    label: Optional[str] = None,
) -> Optional[MacroObservation]:
    """Parse a FRED JSON response, ignoring missing-value markers."""
    usable = []
    for item in payload.get("observations", []) or []:
        raw_value = str(item.get("value", "")).strip()
        if not raw_value or raw_value == ".":
            continue
        try:
            usable.append((str(item.get("date", "")), float(raw_value)))
        except (TypeError, ValueError):
            continue
    if not usable:
        return None
    usable.sort(key=lambda item: item[0], reverse=True)
    date, value = usable[0]
    previous = usable[1][1] if len(usable) > 1 else None
    return MacroObservation(
        series_id=series_id,
        label=label or DEFAULT_SERIES_LABELS.get(series_id, series_id),
        value=value,
        observation_date=date,
        previous_value=previous,
    )


class FredMacroData:
    """Fetch configured FRED observations with per-series failure isolation."""

    BASE_URL = "https://api.stlouisfed.org/fred/series/observations"

    def __init__(
        self,
        api_key: str = "",
        timeout_seconds: float = 10.0,
        urlopen_fn=urlopen,
    ) -> None:
        self._api_key = api_key.strip()
        self._timeout_seconds = timeout_seconds
        self._urlopen = urlopen_fn

    def fetch_snapshot(self, series_ids: Iterable[str]) -> MacroSnapshot:
        fetched_at = datetime.now(timezone.utc)
        if not self._api_key:
            return MacroSnapshot({}, fetched_at, available=False)

        observations: Dict[str, MacroObservation] = {}
        for raw_id in series_ids:
            series_id = str(raw_id).strip().upper()
            if not series_id:
                continue
            try:
                params = urlencode(
                    {
                        "api_key": self._api_key,
                        "file_type": "json",
                        "series_id": series_id,
                        "sort_order": "desc",
                        "limit": 5,
                    }
                )
                request = Request(
                    f"{self.BASE_URL}?{params}",
                    headers={"User-Agent": "trading-bot/1.0 (+macro-data-reader)"},
                )
                payload = None
                for attempt in range(1, 4):
                    try:
                        with self._urlopen(
                            request, timeout=self._timeout_seconds
                        ) as response:
                            payload = json.loads(response.read().decode("utf-8"))
                        break
                    except Exception:
                        if attempt == 3:
                            raise
                        time.sleep(min(5.0, 2.0 ** (attempt - 1)))
                if payload is None:
                    continue
                observation = parse_fred_response(series_id, payload)
                if observation is not None:
                    observations[series_id] = observation
            except Exception as exc:
                log.warning(f"FRED series {series_id} failed: {exc}")

        return MacroSnapshot(observations, fetched_at, available=bool(observations))
