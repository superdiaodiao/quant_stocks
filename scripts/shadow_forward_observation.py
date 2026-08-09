#!/usr/bin/env python3
"""Record a research-only shadow observation from a public price endpoint.

This deliberately does not modify the formal price cache, recommendation
ledger, validation artifacts, or release gate.  It is useful when the local
Nasdaq endpoint is temporarily behind the first post-signal close: the last
locally verified close is used as the overlap anchor, and the next close is
accepted only after the same Yahoo series matches that anchor within tolerance.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd

from src.conf import CLEANED_PRICE_DATA_DIR, NASDAQ_INDEX_FILE, PROJECT_PATH
from src.io.nasdaq_update import fetch_history
from src.research.shadow_evaluation import monthly_execution_session


HEADERS = {"User-Agent": "quant-stocks-shadow-research", "Accept": "application/json"}
DEFAULT_MODEL_DIR = (
    Path(PROJECT_PATH) / "output/daily/can-slim-top3-v1"
)


def yahoo_chart(ticker: str, start: pd.Timestamp, end: pd.Timestamp) -> tuple[bytes, pd.DataFrame]:
    params = urlencode({
        "period1": int(start.tz_localize("UTC").timestamp()),
        "period2": int((end + pd.Timedelta(days=1)).tz_localize("UTC").timestamp()),
        "interval": "1d",
        "events": "div,splits",
        "includeAdjustedClose": "true",
    })
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?{params}"
    with urlopen(Request(url, headers=HEADERS), timeout=30) as response:
        payload = response.read()
    chart = json.loads(payload.decode("utf-8"))["chart"]
    if chart.get("error") or not chart.get("result"):
        raise ValueError(f"Yahoo returned no result for {ticker}: {chart.get('error')}")
    result = chart["result"][0]
    timestamps = result.get("timestamp") or []
    quotes = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    frame = pd.DataFrame({
        "date": pd.to_datetime(timestamps, unit="s", utc=True).tz_convert(None).normalize(),
        "close": quotes.get("close", []),
    }).dropna(subset=["date", "close"]).drop_duplicates("date")
    if frame.empty or frame["close"].le(0).any():
        raise ValueError(f"Yahoo returned no positive closes for {ticker}")
    return payload, frame.sort_values("date").reset_index(drop=True)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _local_close(ticker: str, date: pd.Timestamp) -> float:
    frame = pd.read_csv(
        Path(CLEANED_PRICE_DATA_DIR) / f"{ticker.lower()}.csv",
        parse_dates=["date"],
    )
    rows = frame.loc[frame["date"].dt.normalize().eq(date), "close"]
    if rows.empty:
        raise ValueError(f"local close missing for {ticker} on {date.date()}")
    return float(rows.iloc[-1])


def _fetch_observation_source(
    ticker: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> tuple[bytes, pd.DataFrame, str]:
    """Prefer the repository's Nasdaq API, falling back only when it lags."""
    asset_class = "index" if ticker == "^IXIC" else "stocks"
    try:
        frame = fetch_history(
            ticker,
            start.date(),
            end.date(),
            asset_class=asset_class,
            retries=1,
        )
        frame = frame[["date", "close"]].copy()
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.normalize()
        if frame["date"].eq(end).any():
            payload = frame.sort_values("date").to_json(orient="records", date_format="iso").encode()
            return payload, frame.dropna(subset=["date", "close"]), "Nasdaq public historical API"
    except Exception:
        pass
    payload, frame = yahoo_chart(ticker, start, end)
    return payload, frame, "Yahoo Chart API fallback"


def _write_observation_index(out: Path, report: dict) -> None:
    index_path = out.parent / "index.json"
    index = []
    if index_path.exists():
        try:
            index = list(json.loads(index_path.read_text(encoding="utf-8")).get("observations", []))
        except (OSError, json.JSONDecodeError):
            index = []
    index = [item for item in index if item.get("observation_date") != report["observation_date"]]
    index.append({
        "observation_date": report["observation_date"],
        "signal_date": report["signal_date"],
        "status": report.get("status", "UNANCHORED_FORWARD_OBSERVATION"),
        "forward_sessions": report.get("forward_sessions", 0),
        "strategy_return": report.get("strategy_return"),
        "benchmark_return": report["benchmark_return"],
        "excess_return": report["excess_return"],
        "file": out.name,
    })
    index_path.write_text(
        json.dumps({"schema_version": 1, "research_only": True, "observations": sorted(index, key=lambda item: item["observation_date"])}, indent=2) + "\n",
        encoding="utf-8",
    )


def record_observation(
    *,
    observation_date: str,
    model_dir: str | Path = DEFAULT_MODEL_DIR,
    overlap_tolerance: float = 0.01,
    refresh_existing: bool = False,
) -> dict:
    observation = pd.Timestamp(observation_date).normalize()
    observation_dir = Path(model_dir) / "shadow_observations"
    out = observation_dir / f"{observation:%Y-%m-%d}.json"
    if out.exists() and not refresh_existing:
        existing = json.loads(out.read_text(encoding="utf-8"))
        _write_observation_index(out, existing)
        existing["status"] = "ALREADY_RECORDED"
        existing["output"] = str(out)
        return existing
    history_path = Path(model_dir) / "recommendation_history.csv"
    history = pd.read_csv(history_path)
    candidates = history.loc[
        (history["as_of"] == history["as_of"].max())
        & history["action"].eq("BUY_NEXT_CLOSE")
    ].copy()
    if candidates.empty:
        raise ValueError("no BUY_NEXT_CLOSE shadow candidates in recommendation history")
    signal_date = pd.Timestamp(candidates["as_of"].iloc[0]).normalize()
    execution_date = monthly_execution_session(signal_date)
    anchor_date = signal_date
    if observation < execution_date:
        raise ValueError("observation date must be on or after the execution session")
    source_payloads: dict[str, str] = {}
    rows = []
    for ticker in [*candidates["ticker"].astype(str).tolist(), "^IXIC"]:
        payload, source_frame, source_name = _fetch_observation_source(
            ticker, anchor_date, observation
        )
        source_payloads[ticker] = _sha256(payload)
        anchor = source_frame.loc[source_frame["date"].eq(anchor_date), "close"]
        execution = source_frame.loc[source_frame["date"].eq(execution_date), "close"]
        observed = source_frame.loc[source_frame["date"].eq(observation), "close"]
        if anchor.empty or execution.empty or observed.empty:
            raise ValueError(f"source overlap/execution/observation missing for {ticker}")
        reference = (
            _local_close(ticker, anchor_date)
            if ticker != "^IXIC"
            else float(pd.read_csv(NASDAQ_INDEX_FILE, parse_dates=["date"])
                       .loc[lambda x: x["date"].dt.normalize().eq(anchor_date), "close"]
                       .iloc[-1])
        )
        ratio = float(anchor.iloc[-1]) / reference
        if abs(ratio - 1.0) > overlap_tolerance:
            raise ValueError(f"{ticker}: Yahoo/local overlap ratio {ratio:.6f} exceeds tolerance")
        rows.append({
            "ticker": ticker,
            "anchor_date": anchor_date.strftime("%Y-%m-%d"),
            "anchor_local_close": reference,
            "anchor_yahoo_close": float(anchor.iloc[-1]),
            "execution_date": execution_date.strftime("%Y-%m-%d"),
            "execution_close": float(execution.iloc[-1]),
            "observation_date": observation.strftime("%Y-%m-%d"),
            "observation_close": float(observed.iloc[-1]),
            "return_from_execution": (
                float(observed.iloc[-1] / execution.iloc[-1] - 1.0)
                if observation > execution_date else None
            ),
            "source_payload_sha256": source_payloads[ticker],
            "source_provider": source_name,
        })
    stock = pd.DataFrame(rows).loc[lambda x: x["ticker"] != "^IXIC"]
    benchmark = pd.DataFrame(rows).loc[lambda x: x["ticker"] == "^IXIC"].iloc[0]
    forward_sessions = int(observation > execution_date)
    weights = candidates.set_index(
        candidates["ticker"].astype(str).str.upper()
    )["target_weight"].astype(float)
    exposure = float(weights.sum())
    cost_bps = 10.0
    post_trade_nav = 1.0 / (1.0 + exposure * cost_bps / 10_000.0)
    strategy_return = None
    if forward_sessions:
        growth = stock.set_index("ticker")["observation_close"].div(
            stock.set_index("ticker")["execution_close"]
        )
        ending_nav = post_trade_nav * (1.0 - exposure)
        ending_nav += float(
            (weights.reindex(growth.index) * post_trade_nav * growth).sum()
        )
        strategy_return = float(ending_nav - 1.0)
    benchmark_return = (
        float(benchmark["observation_close"] / benchmark["execution_close"] - 1.0)
        if forward_sessions else None
    )
    report = {
        "schema_version": 1,
        "research_only": True,
        "status": (
            "UNANCHORED_FORWARD_OBSERVATION"
            if forward_sessions else "EXECUTION_ANCHOR_ONLY"
        ),
        "model_version": str(candidates["model_version"].iloc[0]),
        "signal_date": anchor_date.strftime("%Y-%m-%d"),
        "execution_date": execution_date.strftime("%Y-%m-%d"),
        "observation_date": observation.strftime("%Y-%m-%d"),
        "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
        "tickers": rows,
        "strategy_return": strategy_return,
        "accounting_method": "standalone_fixed_positions",
        "transaction_cost_bps": cost_bps,
        "benchmark_return": benchmark_return,
        "excess_return": (
            strategy_return - benchmark_return
            if forward_sessions else None
        ),
        "forward_sessions": forward_sessions,
        "overlap_tolerance": overlap_tolerance,
        "source": "Nasdaq public historical API preferred; Yahoo fallback; overlap-checked against local Nasdaq closes",
        "formal_price_cache_modified": False,
        "formal_financial_files_modified": False,
        "release_gate_modified": False,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    report["output"] = str(out)
    _write_observation_index(out, report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--observation-date",
        default="latest",
        help="YYYY-MM-DD, or latest to probe recent completed sessions",
    )
    parser.add_argument("--model-dir", default=str(DEFAULT_MODEL_DIR))
    parser.add_argument("--refresh-existing", action="store_true")
    args = parser.parse_args()
    if args.observation_date != "latest":
        result = record_observation(
            observation_date=args.observation_date,
            model_dir=args.model_dir,
            refresh_existing=args.refresh_existing,
        )
    else:
        history = pd.read_csv(Path(args.model_dir) / "recommendation_history.csv")
        signal_date = pd.Timestamp(history["as_of"].max()).normalize()
        result = None
        for offset in range(1, 8):
            candidate = pd.Timestamp.today().normalize() - pd.Timedelta(days=offset)
            if candidate <= signal_date:
                continue
            try:
                result = record_observation(
                    observation_date=candidate.strftime("%Y-%m-%d"),
                    model_dir=args.model_dir,
                    refresh_existing=args.refresh_existing,
                )
                break
            except Exception:
                continue
        if result is None:
            raise RuntimeError("no completed public close found in the last seven days")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
