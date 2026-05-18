"""Fetch market data from the local BB-Terminal OpenBB API."""
from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import dataclass
from typing import Iterable

import httpx

API_BASE = "http://127.0.0.1:6900/api/v1"
TIMEOUT = httpx.Timeout(8.0, connect=2.0)


@dataclass(frozen=True)
class Asset:
    sym: str
    name: str
    kind: str  # "equity" | "etf" | "index" | "crypto"


@dataclass
class Quote:
    sym: str
    name: str
    kind: str
    last: float | None
    prev: float | None
    series: list[float]  # close series for sparkline

    @property
    def chg(self) -> float | None:
        if self.last is None or self.prev is None:
            return None
        return self.last - self.prev

    @property
    def chg_pct(self) -> float | None:
        if self.last is None or self.prev is None or self.prev == 0:
            return None
        return (self.last - self.prev) / self.prev * 100


@dataclass
class Mover:
    sym: str
    name: str
    price: float | None
    chg_pct: float | None  # percent (already scaled)


WATCHLIST: list[Asset] = [
    Asset("BTC-USD", "Bitcoin",           "crypto"),
    Asset("AAPL",    "Apple",             "equity"),
    Asset("NDAQ",    "Nasdaq Inc",        "equity"),
    Asset("ARM",     "ARM Holdings",      "equity"),
    Asset("WFC",     "Wells Fargo",       "equity"),
    Asset("^DJI",    "Dow Jones",         "index"),
    Asset("TSLA",    "Tesla",             "equity"),
    Asset("NVDA",    "Nvidia",            "equity"),
    Asset("LCID",    "Lucid Motors",      "equity"),
    Asset("AMZN",    "Amazon",            "equity"),
    Asset("RIVN",    "Rivian",            "equity"),
    Asset("MSFT",    "Microsoft",         "equity"),
    Asset("GOOGL",   "Alphabet",          "equity"),
    Asset("F",       "Ford",              "equity"),
    Asset("AMD",     "AMD",               "equity"),
    Asset("META",    "Meta Platforms",    "equity"),
    Asset("AMC",     "AMC Entertainment", "equity"),
    Asset("DIS",     "Disney",            "equity"),
    Asset("WMT",     "Walmart",           "equity"),
    Asset("NFLX",    "Netflix",           "equity"),
    Asset("KO",      "Coca-Cola",         "equity"),
    Asset("QQQ",     "Invesco QQQ",       "etf"),
]

CC_INDICES: list[Asset] = [
    Asset("^GSPC", "S&P 500",   "index"),
    Asset("^DJI",  "Dow",       "index"),
    Asset("^IXIC", "Nasdaq",    "index"),
    Asset("^RUT",  "Russell",   "index"),
    Asset("^VIX",  "VIX",       "index"),
]


async def _fetch_history(client: httpx.AsyncClient, sym: str, days: int = 30) -> list[float]:
    """Close-series for any symbol. Routes through /equity/ — works for all symbol
    types on this install because openbb-index and openbb-crypto aren't present."""
    start = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    r = await client.get(
        f"{API_BASE}/equity/price/historical",
        params={
            "provider": "yfinance",
            "symbol": sym,
            "interval": "1d",
            "start_date": start,
        },
    )
    r.raise_for_status()
    rows = r.json().get("results", [])
    return [float(row["close"]) for row in rows if row.get("close") is not None]


async def _fetch_movers(client: httpx.AsyncClient, kind: str) -> list[Mover]:
    """kind: 'gainers' or 'losers'."""
    r = await client.get(f"{API_BASE}/equity/discovery/{kind}", params={"provider": "yfinance"})
    r.raise_for_status()
    out: list[Mover] = []
    for row in r.json().get("results", []):
        out.append(
            Mover(
                sym=row.get("symbol", ""),
                name=row.get("name", ""),
                price=row.get("price"),
                # API returns percent_change as a decimal (0.05 = 5%)
                chg_pct=(row.get("percent_change") or 0) * 100 if row.get("percent_change") is not None else None,
            )
        )
    return out


async def _quote_from_history(client: httpx.AsyncClient, asset: Asset, days: int) -> Quote:
    try:
        series = await _fetch_history(client, asset.sym, days)
    except Exception:
        series = []
    last = series[-1] if series else None
    prev = series[-2] if len(series) >= 2 else None
    return Quote(asset.sym, asset.name, asset.kind, last, prev, series)


async def fetch_quotes(assets: Iterable[Asset], days: int = 30) -> list[Quote]:
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        return await asyncio.gather(*(_quote_from_history(client, a, days) for a in assets))


async def fetch_movers(limit: int = 5) -> tuple[list[Mover], list[Mover]]:
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        g, l = await asyncio.gather(_fetch_movers(client, "gainers"), _fetch_movers(client, "losers"))
    return g[:limit], l[:limit]


def synchronous_quotes(assets: Iterable[Asset], days: int = 30) -> list[Quote]:
    return asyncio.run(fetch_quotes(assets, days))


def synchronous_movers(limit: int = 5) -> tuple[list[Mover], list[Mover]]:
    return asyncio.run(fetch_movers(limit))
