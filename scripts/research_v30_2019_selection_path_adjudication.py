#!/usr/bin/env python3
"""Adjudicate the remaining 2019 selection path for the v29 winner.

The full exchange membership of four 2019 signals is not recoverable from the
archived snapshots.  This audit proves the narrower fact the strategy needs:
the eventual Top-5 is unchanged across the bounding snapshots, their
intersection/union, and an unrestricted price-panel competitor set.  It also
repairs the source-locked FB-to-META ticker identity used by the price and
fundamental panels.  No old artifact or formal input file is modified.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path

import pandas as pd

from scripts import research_v26_large_liquid_stock_momentum as v26
from scripts import research_v29_recovered_2019_stock_momentum as v29
from src.io.security_identity import SECURITY_IDENTITY_FILE
from src.research.universe_history import universe_as_of
from src.strategy.common import market_regime_is_on


OUTPUT_DIR = Path(
    "output/research_only/v30/selection_path_adjudication_20260830"
)
PROTOCOL_PATH = OUTPUT_DIR / "frozen_protocol.json"
RESULT_OUTPUT_DIR = OUTPUT_DIR / "results"
V29_MANIFEST = v29.DEVELOPMENT_OUTPUT_DIR / "manifest.json"
SELECTED_CANDIDATE = "mom63_skip0_liquid25_top5_profitable_monthly"
META_IDENTITY_EVIDENCE = {
    "provider_ticker": "META",
    "historical_ticker": "FB",
    "last_historical_date": "2022-06-08",
    "current_ticker_first_date": "2022-06-09",
    "identity_type": "issuer_rename",
    "source_url": (
        "https://www.sec.gov/Archives/edgar/data/1326801/"
        "000132680122000070/fb-20220531.htm"
    ),
    "source_accession": "0001326801-22-000070",
    "source_fact": (
        "The 2022-05-31 Form 8-K states that Class A common stock would begin "
        "trading on Nasdaq under META before market open on 2022-06-09."
    ),
}
RISK_ON_GAP_SIGNALS = tuple(pd.to_datetime([
    "2019-04-30",
    "2019-08-30",
    "2019-09-30",
]))
RISK_OFF_GAP_SIGNALS = (pd.Timestamp("2019-05-31"),)


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _file_binding(path: Path) -> dict:
    path = Path(path)
    return {"path": str(path), "sha256": _sha256(path)}


def selected_specification() -> dict:
    manifest = json.loads(V29_MANIFEST.read_text(encoding="utf-8"))
    if manifest["selected_candidate"] != SELECTED_CANDIDATE:
        raise RuntimeError("v29 selected candidate changed")
    return manifest["selected_specification"]


def normalize_meta_identity(
    snapshots: dict[pd.Timestamp, set[str]],
) -> dict[pd.Timestamp, set[str]]:
    """Match historical FB snapshot membership to provider ticker META."""
    last_historical = pd.Timestamp(
        META_IDENTITY_EVIDENCE["last_historical_date"]
    )
    normalized = {}
    for stamp, symbols in snapshots.items():
        members = set(symbols)
        if stamp <= last_historical and "FB" in members:
            members.remove("FB")
            members.add("META")
        normalized[stamp] = members
    return normalized


def _ranking_for_universe(
    signal_date: pd.Timestamp,
    universe: set[str],
    spec: dict,
    inputs: dict,
) -> list[str]:
    inputs["universe"] = lambda _date: universe
    inputs["technical_cache"] = {}
    inputs["quality_cache"] = {}
    inputs["large_liquid_cache"] = {}
    ranking = v26._large_liquid_ranking(signal_date, spec, inputs)
    return ranking.index.astype(str).tolist()


def audit_gap_selection_path(
    inputs: dict,
    snapshots: dict[pd.Timestamp, set[str]],
    spec: dict,
) -> tuple[dict, dict[pd.Timestamp, set[str]]]:
    index_close = inputs["nasdaq"].reindex(inputs["close"].index).ffill()
    records = []
    adjudicated = {}
    all_price_tickers = set(inputs["close"].columns)
    for signal_date in map(pd.Timestamp, v29.EXPECTED_MISSING_2019_SIGNALS):
        prior_date = max(stamp for stamp in snapshots if stamp <= signal_date)
        next_date = min(stamp for stamp in snapshots if stamp > signal_date)
        regime_on = market_regime_is_on(
            signal_date, index_close, v26.v24.MARKET_MA_DAYS
        )
        record = {
            "signal_date": signal_date.strftime("%Y-%m-%d"),
            "market_regime_on": bool(regime_on),
            "prior_snapshot_date": prior_date.strftime("%Y-%m-%d"),
            "next_snapshot_date": next_date.strftime("%Y-%m-%d"),
        }
        if not regime_on:
            if signal_date not in RISK_OFF_GAP_SIGNALS:
                raise RuntimeError(f"unexpected risk-off gap signal: {signal_date}")
            record.update({
                "status": "PASS_RISK_OFF_NO_UNIVERSE_REQUIRED",
                "selection": [],
            })
            records.append(record)
            continue
        if signal_date not in RISK_ON_GAP_SIGNALS:
            raise RuntimeError(f"unexpected risk-on gap signal: {signal_date}")
        prior = snapshots[prior_date]
        following = snapshots[next_date]
        scenarios = {
            "prior": prior,
            "next": following,
            "intersection": prior & following,
            "union": prior | following,
            "unrestricted_price_panel": all_price_tickers,
        }
        rankings = {
            label: _ranking_for_universe(signal_date, universe, spec, inputs)
            for label, universe in scenarios.items()
        }
        top_n = int(spec["top_n"])
        selections = {
            label: ranking[:top_n] for label, ranking in rankings.items()
        }
        stable = len({tuple(value) for value in selections.values()}) == 1
        if not stable:
            raise RuntimeError(
                f"gap signal Top-{top_n} is membership-sensitive: {signal_date}"
            )
        adjudicated[signal_date] = scenarios["intersection"]
        record.update({
            "status": "PASS_TOP5_STABLE_ACROSS_BOUNDING_AND_UNRESTRICTED",
            "selection": next(iter(selections.values())),
            "scenario_selections": selections,
            "scenario_liquid_pool": rankings,
            "adjudicated_universe_policy": "BOUNDING_SNAPSHOT_INTERSECTION",
        })
        records.append(record)
    report = {
        "schema_version": 1,
        "status": "PASS" if len(records) == 4 else "BLOCKED",
        "full_exchange_membership_recovered": False,
        "strategy_selection_path_complete": bool(
            len(records) == 4
            and all(record["status"].startswith("PASS_") for record in records)
        ),
        "identity_evidence": META_IDENTITY_EVIDENCE,
        "records": records,
    }
    return report, adjudicated


def freeze_protocol(path: Path = PROTOCOL_PATH) -> dict:
    path = Path(path)
    if path.exists():
        raise RuntimeError(f"v30 protocol will not be overwritten: {path}")
    runner = Path(__file__).resolve().relative_to(Path.cwd().resolve())
    protocol = {
        "schema_version": 1,
        "research_only": True,
        "stage": "V30_2019_SELECTION_PATH_ADJUDICATION_PRECOMMITMENT",
        "status": "FROZEN_NOT_RUN",
        "frozen_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "objective": (
            "Prove the selected stock path for four unresolved 2019 signals "
            "without claiming full exchange-membership recovery."
        ),
        "selected_candidate": SELECTED_CANDIDATE,
        "selected_specification": selected_specification(),
        "identity_evidence": META_IDENTITY_EVIDENCE,
        "risk_on_gap_signals": [
            stamp.strftime("%Y-%m-%d") for stamp in RISK_ON_GAP_SIGNALS
        ],
        "risk_off_gap_signals": [
            stamp.strftime("%Y-%m-%d") for stamp in RISK_OFF_GAP_SIGNALS
        ],
        "input_bindings": {
            "runner": _file_binding(runner),
            "v26_candidate_helpers": _file_binding(
                Path("scripts/research_v26_large_liquid_stock_momentum.py")
            ),
            "v29_recovered_universe_helpers": _file_binding(
                Path("scripts/research_v29_recovered_2019_stock_momentum.py")
            ),
            "v29_manifest": _file_binding(V29_MANIFEST),
            "formal_security_identity": _file_binding(SECURITY_IDENTITY_FILE),
        },
        "2026_used_for_development_or_selection": False,
        "brokerage_or_trading_authorized": False,
        "release_status": "BLOCKED",
        "promotion_eligible": False,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(protocol, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {**protocol, "protocol": _file_binding(path)}


def _validated_protocol(path: Path) -> tuple[dict, str]:
    path = Path(path)
    protocol_sha = _sha256(path)
    protocol = json.loads(path.read_text(encoding="utf-8"))
    if protocol["status"] != "FROZEN_NOT_RUN":
        raise RuntimeError("v30 protocol status changed")
    if protocol["selected_specification"] != selected_specification():
        raise RuntimeError("v30 selected specification changed")
    if protocol["identity_evidence"] != META_IDENTITY_EVIDENCE:
        raise RuntimeError("v30 identity evidence changed")
    for name, binding in protocol["input_bindings"].items():
        if _sha256(Path(binding["path"])) != binding["sha256"]:
            raise RuntimeError(f"v30 file binding changed for {name}")
    return protocol, protocol_sha


def run(
    protocol_path: Path = PROTOCOL_PATH,
    output_dir: Path = RESULT_OUTPUT_DIR,
) -> dict:
    protocol, protocol_sha = _validated_protocol(protocol_path)
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise RuntimeError(f"v30 output will not be overwritten: {output_dir}")
    inputs = v29._load_inputs()
    snapshots = normalize_meta_identity(v29.load_repaired_universe_snapshots())
    audit, adjudicated = audit_gap_selection_path(
        inputs, snapshots, protocol["selected_specification"]
    )
    if not audit["strategy_selection_path_complete"]:
        raise RuntimeError("v30 selection path remains incomplete")

    universe_cache = {}
    def universe(signal_date):
        stamp = pd.Timestamp(signal_date).normalize()
        if stamp in adjudicated:
            return adjudicated[stamp] - v29.FORBIDDEN_ETFS
        if stamp not in universe_cache:
            symbols = universe_as_of(
                snapshots,
                stamp,
                maximum_age_days=v29.MAXIMUM_SNAPSHOT_AGE_DAYS,
            )
            universe_cache[stamp] = (
                None if symbols is None else set(symbols) - v29.FORBIDDEN_ETFS
            )
        return universe_cache[stamp]

    inputs["universe"] = universe
    inputs["technical_cache"] = {}
    inputs["quality_cache"] = {}
    inputs["large_liquid_cache"] = {}
    results, targets = v26._generate_candidate(
        protocol["selected_specification"], inputs
    )
    summary = v26.v23._summary(results)

    output_dir.mkdir(parents=True, exist_ok=True)
    audit_path = output_dir / "gap_selection_path_audit.json"
    audit_path.write_text(
        json.dumps(audit, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    targets_path = output_dir / "selected_targets.csv"
    targets.to_csv(targets_path, index=False)
    outputs = {
        "gap_selection_path_audit": _file_binding(audit_path),
        "selected_targets": _file_binding(targets_path),
    }
    for cost in v29.COSTS:
        result_path = output_dir / f"selected_daily_{cost}bps.csv"
        results[cost].to_csv(result_path, index_label="date")
        outputs[f"selected_daily_{cost}bps"] = _file_binding(result_path)
    report = {
        "schema_version": 1,
        "research_only": True,
        "stage": "V30_2019_SELECTION_PATH_ADJUDICATION_RESULT",
        "protocol": {"path": str(protocol_path), "sha256": protocol_sha},
        "audit": audit,
        "selected_candidate": SELECTED_CANDIDATE,
        "selected_summary": summary,
        "outputs": outputs,
        "2026_used_for_development_or_selection": False,
        "brokerage_or_trading_authorized": False,
        "release_status": "BLOCKED",
        "promotion_eligible": False,
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {**report, "manifest": _file_binding(manifest_path)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    freeze_parser = subparsers.add_parser("freeze")
    freeze_parser.add_argument("--protocol", type=Path, default=PROTOCOL_PATH)
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--protocol", type=Path, default=PROTOCOL_PATH)
    run_parser.add_argument("--output-dir", type=Path, default=RESULT_OUTPUT_DIR)
    args = parser.parse_args()
    report = (
        freeze_protocol(args.protocol)
        if args.command == "freeze"
        else run(args.protocol, args.output_dir)
    )
    fields = (
        ("status", "selected_candidate", "protocol")
        if args.command == "freeze"
        else ("selected_candidate", "audit", "release_status", "manifest")
    )
    print(json.dumps({field: report[field] for field in fields}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
