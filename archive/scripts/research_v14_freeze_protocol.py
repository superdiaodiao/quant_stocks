#!/usr/bin/env python3
"""Freeze the v14 research replay protocol without executing final-data results."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path

import pandas as pd

from src.research.can_slim_walk_forward import candidate_configs


OUTPUT_PATH = Path("output/research_only/v14/frozen_protocol_20260829.json")
FROZEN_AT = "2026-08-29"

FORMAL_BINDINGS = {
    "annual_fundamentals": {
        "path": Path(
            "cleaned_stocks_data/financial/fundamentals_point_in_time.csv"
        ),
        "sha256": (
            "62f6c624b2fac85118ea6d49646870f1a56fa053687cb617dc468d856e19c34d"
        ),
    },
    "quarterly_fundamentals": {
        "path": Path(
            "cleaned_stocks_data/financial/quarterly_fundamentals_point_in_time.csv"
        ),
        "sha256": (
            "1be16a6342217d6771eca7d2ca49156e726b424ebfbfe7b90dfa6c232ea8bf69"
        ),
    },
    "annual_coverage": {
        "path": Path("cleaned_stocks_data/financial/fundamentals_coverage.json"),
        "sha256": (
            "275664a817d430767de47af430c7a6265670c1d541c6170ee4d120ff9f3e0e21"
        ),
    },
    "quarterly_coverage": {
        "path": Path(
            "cleaned_stocks_data/financial/quarterly_fundamentals_coverage.json"
        ),
        "sha256": (
            "40c33be0f82c4ee4a55ba7f9650773620e4f8d284c17e9c419336013ce640bf2"
        ),
    },
}

CANDIDATE_BINDINGS = {
    "annual": {
        "path": Path(
            "output/research_only/v14/"
            "candidate_fundamentals_v14_checkpoint_20260829_"
            "sohu_restated_quarters/annual.csv"
        ),
        "sha256": (
            "f49fe5777baf2a64331a0cbe54bee794e8c2819d5dd732fb7dca017b2d2a95ce"
        ),
    },
    "quarterly": {
        "path": Path(
            "output/research_only/v14/"
            "candidate_fundamentals_v14_checkpoint_20260829_"
            "sohu_restated_quarters/quarterly.csv"
        ),
        "sha256": (
            "a9df5975908c7416b7e0902a8adeef02cc32b25c83367999687d9992b45d5caf"
        ),
    },
    "manifest": {
        "path": Path(
            "output/research_only/v14/"
            "candidate_fundamentals_v14_checkpoint_20260829_"
            "sohu_restated_quarters/manifest.json"
        ),
        "sha256": (
            "f2489fa0ed95b4f0f9a39d8c54e7b9244290bec199f2767af0c634b5b7eb50a0"
        ),
    },
}

AUDIT_BINDINGS = {
    "summary": {
        "path": Path(
            "output/research_only/v14/"
            "checkpoint_20260829_sohu_restated_quarters_recovered.json"
        ),
        "sha256": (
            "70ae7458cdcf785d69942acc46782b99bca3fd70c4b67b0348c5f68e178295a1"
        ),
    },
    "financial_priorities": {
        "path": Path(
            "output/research_only/v14/"
            "checkpoint_20260829_sohu_restated_quarters_recovered_"
            "financial_priorities.csv"
        ),
        "sha256": (
            "da109905c70d36898fe8f2689275fc272e69d69a0451f9866de59e6eb8beedca"
        ),
    },
    "price_priorities": {
        "path": Path(
            "output/research_only/v14/"
            "checkpoint_20260829_sohu_restated_quarters_recovered_"
            "price_priorities.csv"
        ),
        "sha256": (
            "79aca5f47fad5d5fc9dd9057787cbea3bf05640cc473b81b0aae2102bd0d7959"
        ),
    },
}

UNIVERSE_NEGATIVE_BINDING = {
    "path": Path(
        "output/research_only/v14/"
        "universe_2019_stale_signal_negative_evidence.json"
    ),
    "sha256": (
        "1292eed89c98af4394255095f5de6ae848a662fa36e6fa6d22e94e24e3b1e734"
    ),
}

CODE_BINDINGS = {
    "can_slim": {
        "path": Path("src/research/can_slim.py"),
        "sha256": (
            "5d1ba65c3f4e7896bbf1a000a55bc7fd6cbb7ce989c0bbf087b9617613855c41"
        ),
    },
    "walk_forward": {
        "path": Path("src/research/can_slim_walk_forward.py"),
        "sha256": (
            "b16fa32237d20e0fb9e715e6af924ebb68690275ef53f23e17156a90d48a5a25"
        ),
    },
    "adaptive_runner": {
        "path": Path("scripts/research_v14_adaptive_pretrain.py"),
        "sha256": (
            "c2cedf7ab38da76b8aa80cff2e826ff120a27aba8fa6c243856796d650db8cd9"
        ),
    },
    "universe_exclusion": {
        "path": Path(
            "scripts/research_v14_universe_gap_negative_evidence.py"
        ),
        "sha256": (
            "b07cd214503a88e38ff2dde114a8a75edf97a41ad3527a47ef1374e3ed9a682b"
        ),
    },
    "frozen_replay": {
        "path": Path("scripts/research_v14_frozen_replay.py"),
        "sha256": (
            "71a0f86b5f584924f22eca27b071f40e3403bf838c6d3b2af5e87050c761e72c"
        ),
    },
}

EVIDENCE_BINDINGS = {
    "ADUS": ({
        "path": Path(
            "output/research_only/v14/adus_pit_unrecoverable/manifest.json"
        ),
        "sha256": (
            "8603b3fd6b64abcde18eaa2a9413f69d036ca0b95f8f6499b6ad901c88e2c994"
        ),
    },),
    "ARGX": ({
        "path": Path(
            "output/research_only/v14/"
            "argx_sec_quarterly_reports_2019_2021/manifest.json"
        ),
        "sha256": (
            "f5bbe77d87e39c3af9ba8656cb1fb6627b726f1027654c1794669b3798a13a53"
        ),
    },),
    "GGAL": ({
        "path": Path(
            "output/research_only/v14/"
            "ggal_ias29_quarters_2019q3_2021q2/manifest.json"
        ),
        "sha256": (
            "7432407d801483a598e579d6473c9a46dfedcdd122aa89e31f6c0401838aad58"
        ),
    },),
    "HCM": ({
        "path": Path(
            "output/research_only/v14/hcm_direct_ttm_loss/manifest.json"
        ),
        "sha256": (
            "204b76224f03e54a2d33f40b39f1aeed9929029a1e69ac8149881f99149e5b90"
        ),
    },),
    "ITOS": ({
        "path": Path(
            "output/research_only/v14/itos_zero_revenue_growth/manifest.json"
        ),
        "sha256": (
            "e51a378175f715e553a0f78af379239012dc88bd3aeb49532f0911164b23b0dd"
        ),
    },),
    "MOMO": ({
        "path": Path(
            "output/research_only/v14/momo_pit_unrecoverable/manifest.json"
        ),
        "sha256": (
            "4bebcb68bebfa53db55e134bc7cda1eb85b555a47c53b7173958cb7e30f53c10"
        ),
    },),
    "OZK": (
        {
            "path": Path(
                "output/research_only/v14/"
                "ozk_ir_quarterly_reports_2018_2021/manifest.json"
            ),
            "sha256": (
                "61645548293f55a6fe1965373b0f6ac998e03b30c7aec14b5eaa5e82511b6951"
            ),
        },
        {
            "path": Path(
                "output/research_only/v14/ozk_2020q1_residual/manifest.json"
            ),
            "sha256": (
                "88ed73e9398411d2b0a7a73978a39872f9c8b5c08b080a0cc26266579e694a26"
            ),
        },
    ),
    "SMPL": ({
        "path": Path(
            "output/research_only/v14/smpl_acquisition_basis_gap/manifest.json"
        ),
        "sha256": (
            "e54d2eacbbb9e4604318444cec109af46fa1cdcfa3e50a41cdd3c0436743a563"
        ),
    },),
}

EXPECTED_GAP_SYMBOLS = (
    "ADUS",
    "ARGX",
    "GGAL",
    "HCM",
    "ITOS",
    "MOMO",
    "OZK",
    "SMPL",
)
EXCLUDED_SIGNAL_DATES = (
    "2019-03-29",
    "2019-04-30",
    "2019-05-31",
    "2019-08-30",
    "2019-09-30",
)
SNAPSHOT_DIR = Path("output/research_only/v14/universe_snapshots")
SNAPSHOT_FILE_COUNT = 422
SNAPSHOT_CONTENT_MANIFEST_SHA256 = (
    "b68c87a12027eb68420d61410bbdad5667fe9e2541b0be962975b98f434f39f4"
)
PRICE_FILE_COUNT = 4_403
PRICE_CONTENT_MANIFEST_SHA256 = (
    "a8b944e74470afb377c22a7b533ddf1207397d3a285bd8ab0ca548235c13c715"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _verify_bindings(bindings: dict[str, dict]) -> dict[str, dict]:
    verified = {}
    for name, binding in bindings.items():
        path = Path(binding["path"])
        actual = _sha256(path)
        if actual != binding["sha256"]:
            raise RuntimeError(f"{name} binding changed: {actual}")
        verified[name] = {"path": str(path), "sha256": actual}
    return verified


def _directory_binding(
    path: Path,
    pattern: str,
    expected_count: int,
    expected_sha256: str,
) -> dict:
    digest = hashlib.sha256()
    files = sorted(Path(path).glob(pattern))
    for item in files:
        digest.update(item.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_sha256(item).encode("ascii"))
        digest.update(b"\n")
    actual = digest.hexdigest()
    if len(files) != expected_count or actual != expected_sha256:
        raise RuntimeError(
            f"directory binding changed for {path}: {len(files)} {actual}"
        )
    return {
        "path": str(path),
        "pattern": pattern,
        "file_count": len(files),
        "content_manifest_sha256": actual,
    }


def _validate_candidate_and_audit() -> dict:
    candidate = _verify_bindings(CANDIDATE_BINDINGS)
    audit_bindings = _verify_bindings(AUDIT_BINDINGS)
    manifest = json.loads(
        CANDIDATE_BINDINGS["manifest"]["path"].read_text(encoding="utf-8")
    )
    if manifest["outputs"]["quarterly_rows"] != 406_413:
        raise RuntimeError("candidate quarterly row count changed")
    if manifest["formal_financials_modified"]:
        raise RuntimeError("candidate claims formal financial mutation")

    audit = json.loads(
        AUDIT_BINDINGS["summary"]["path"].read_text(encoding="utf-8")
    )
    priorities = pd.read_csv(AUDIT_BINDINGS["financial_priorities"]["path"])
    symbols = tuple(sorted(priorities["ticker"].unique()))
    if symbols != EXPECTED_GAP_SYMBOLS:
        raise RuntimeError(f"financial gap symbol union changed: {symbols}")
    if int(priorities["missing_signal_count"].sum()) != 40:
        raise RuntimeError("financial missing-observation count changed")
    if audit["missing_financial_symbol_union"] != list(EXPECTED_GAP_SYMBOLS):
        raise RuntimeError("audit summary financial gap union changed")
    if not audit["gates"]["research_pretraining_allowed"]:
        raise RuntimeError("audit no longer allows research pretraining")
    if audit["release_status"] != "BLOCKED":
        raise RuntimeError("audit release boundary changed")

    price_binding = audit["input_bindings"]["price_directory"]
    price_directory = _directory_binding(
        Path(price_binding["path"]),
        "*.csv",
        PRICE_FILE_COUNT,
        PRICE_CONTENT_MANIFEST_SHA256,
    )
    if price_directory["content_manifest_sha256"] != price_binding[
        "content_manifest_sha256"
    ]:
        raise RuntimeError("audit price-directory binding changed")
    snapshots = _directory_binding(
        SNAPSHOT_DIR,
        "nasdaq_listed_*.csv",
        SNAPSHOT_FILE_COUNT,
        SNAPSHOT_CONTENT_MANIFEST_SHA256,
    )
    return {
        "candidate": candidate,
        "audit": audit_bindings,
        "price_directory": price_directory,
        "universe_snapshots": snapshots,
        "missing_financial_symbols": list(symbols),
        "missing_financial_observation_count": 40,
    }


def _validate_universe_negative_evidence() -> dict:
    verified = _verify_bindings({
        "universe_negative_evidence": UNIVERSE_NEGATIVE_BINDING
    })["universe_negative_evidence"]
    report = json.loads(
        UNIVERSE_NEGATIVE_BINDING["path"].read_text(encoding="utf-8")
    )
    if report["classification"] != "SOURCE_EXHAUSTED_EXCLUDE_SIGNAL_DATES":
        raise RuntimeError("universe negative-evidence classification changed")
    policy = report["execution_policy"]
    if tuple(policy["excluded_signal_dates"]) != EXCLUDED_SIGNAL_DATES:
        raise RuntimeError("excluded universe signal dates changed")
    if policy != {
        "excluded_signal_dates": list(EXCLUDED_SIGNAL_DATES),
        "carry_prior_holdings_forward": True,
        "backdate_later_snapshot": False,
        "raise_maximum_snapshot_age": False,
        "fabricate_membership": False,
    }:
        raise RuntimeError("universe exclusion policy changed")
    return {**verified, "classification": report["classification"]}


def _validate_financial_gap_evidence() -> dict[str, dict]:
    manifests: dict[str, list[dict]] = {}
    for ticker, bindings in EVIDENCE_BINDINGS.items():
        rows = []
        for index, binding in enumerate(bindings):
            verified = _verify_bindings({f"{ticker}_{index}": binding})[
                f"{ticker}_{index}"
            ]
            report = json.loads(binding["path"].read_text(encoding="utf-8"))
            if report.get("ticker") != ticker:
                raise RuntimeError(f"{ticker} evidence ticker changed")
            if report.get("release_status") != "BLOCKED":
                raise RuntimeError(f"{ticker} evidence release boundary changed")
            if report.get("promotion_eligible") is not False:
                raise RuntimeError(f"{ticker} evidence promotion boundary changed")
            rows.append({**verified, "schema_version": report.get("schema_version")})
        manifests[ticker] = rows

    primary = {
        ticker: json.loads(bindings[0]["path"].read_text(encoding="utf-8"))
        for ticker, bindings in EVIDENCE_BINDINGS.items()
    }
    classifications = {
        "ADUS": primary["ADUS"]["recovery_classification"],
        "ARGX": primary["ARGX"]["blocked_recovery_classification"],
        "GGAL": primary["GGAL"]["blocked_recovery_classification"],
        "HCM": "UNRECOVERABLE_SIX_MONTH_REPORTING_CADENCE",
        "ITOS": primary["ITOS"]["recovery_classification"],
        "MOMO": "UNRECOVERABLE_CURRENCY_BASIS_BREAK",
        "OZK": "SOURCE_LOCKED_PIT_HISTORY_LIMIT",
        "SMPL": primary["SMPL"]["recovery_classification"],
    }
    if primary["HCM"]["unrecoverable_audit_observation_count"] != 2:
        raise RuntimeError("HCM unrecoverable observation count changed")
    if primary["MOMO"]["unrecoverable_audit_observation_count"] != 6:
        raise RuntimeError("MOMO unrecoverable observation count changed")

    ozk = primary["OZK"]
    pre_signal = [
        row for row in ozk["recovered_quarters"]
        if row["available_date"] <= "2019-12-31"
    ]
    if len(pre_signal) != 7:
        raise RuntimeError("OZK pre-signal quarter count changed")
    if pre_signal[0]["fiscal_end"] != "2018-03-31":
        raise RuntimeError("OZK earliest source-locked quarter changed")
    if pre_signal[-1]["fiscal_end"] != "2019-09-30":
        raise RuntimeError("OZK latest pre-signal quarter changed")
    q4_2019 = [
        row for row in ozk["recovered_quarters"]
        if row["fiscal_end"] == "2019-12-31"
    ]
    if len(q4_2019) != 1 or q4_2019[0]["available_date"] != "2020-01-16":
        raise RuntimeError("OZK post-signal Q4 availability changed")

    return {
        ticker: {
            "classification": classifications[ticker],
            "manifests": manifests[ticker],
        }
        for ticker in EXPECTED_GAP_SYMBOLS
    }


def _frozen_grid() -> list[dict]:
    configs = candidate_configs(
        signal_frequency="monthly",
        use_quarterly_fundamentals=True,
        adaptive_channel=False,
        end="2026-07-17",
        maximum_financial_age_days=(150, 365, 550),
    )
    if len(configs) != 18:
        raise RuntimeError("frozen candidate grid size changed")
    if {config.minimum_eps_growth for config in configs} != {0.25}:
        raise RuntimeError("frozen profit-growth gate changed")
    if {config.minimum_revenue_growth for config in configs} != {0.10}:
        raise RuntimeError("frozen revenue-growth gate changed")
    return [
        {"config_id": config_id, **asdict(config)}
        for config_id, config in enumerate(configs)
    ]


def build(
    output_path: Path = OUTPUT_PATH,
    *,
    allow_historical_code_drift: bool = False,
) -> dict:
    """Build the frozen payload; drift bypass is for non-executing tests only.

    The default continues to reject any runtime-code mismatch.  The explicit
    bypass preserves the already-frozen code hashes in a regenerated payload
    so its structure can remain testable after later research code evolves.
    Execution code must never enable it.
    """
    inputs = _validate_candidate_and_audit()
    inputs["formal"] = _verify_bindings(FORMAL_BINDINGS)
    inputs["code"] = (
        {
            name: {
                "path": str(binding["path"]),
                "sha256": binding["sha256"],
            }
            for name, binding in CODE_BINDINGS.items()
        }
        if allow_historical_code_drift
        else _verify_bindings(CODE_BINDINGS)
    )
    inputs["universe_negative_evidence"] = (
        _validate_universe_negative_evidence()
    )
    inputs["financial_gap_evidence"] = _validate_financial_gap_evidence()
    grid = _frozen_grid()

    protocol = {
        "schema_version": 1,
        "research_only": True,
        "frozen_at": FROZEN_AT,
        "protocol_status": "FROZEN_RESEARCH_PROTOCOL",
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "parameters_frozen": True,
        "formal_financials_modified": False,
        "formal_universe_modified": False,
        "final_data_replay_executed": False,
        "results_inspected": False,
        "input_bindings": inputs,
        "model_grid": {
            "candidate_count": len(grid),
            "signal_frequency": "monthly",
            "uses_quarterly_fundamentals": True,
            "adaptive_channel": False,
            "top_n": [3, 5, 10],
            "minimum_median_dollar_volume": [2_000_000.0, 10_000_000.0],
            "maximum_financial_age_days": [150, 365, 550],
            "minimum_profit_growth": 0.25,
            "minimum_revenue_growth": 0.10,
            "configs": grid,
        },
        "selector": {
            "expanding_training_start": "2019-01-01",
            "rolling_window_months": 36,
            "parameter_update_frequency": "annual",
            "first_effective_date": "2022-01-01",
            "last_effective_end": "2026-07-17",
            "ensemble_size": 3,
            "candidate_group": "top_n plus liquidity; age variants compete within group",
            "rank_weights": [3.0, 2.0, 1.0],
            "no_evidence_fallback": False,
            "tie_break_order": [
                "combined_rank ascending",
                "rolling_worst_annual_excess descending",
                "rolling_quality descending",
                "config_id ascending",
            ],
        },
        "execution": {
            "transaction_cost_bps": [10, 30, 50],
            "excluded_signal_dates": list(EXCLUDED_SIGNAL_DATES),
            "excluded_signal_behavior": "carry prior holdings forward",
            "run_count": 1,
            "retune_after_result": False,
            "failure_action": "remain BLOCKED; do not change grid or gates",
        },
        "data_split": {
            "fit_history_start": "2019-01-01",
            "development_validation": {
                "start": "2022-01-01",
                "end": "2024-12-31",
            },
            "final_data_historical_confirmation": {
                "start": "2025-01-01",
                "end": "2026-07-17",
                "exposure_status": "HUMAN_EXPOSURE_CONTAMINATED",
                "statistically_untouched": False,
                "interpretation": (
                    "One-shot frozen-data confirmation only; prior diagnostics "
                    "already exposed these years, so this is not a clean holdout."
                ),
            },
            "genuine_untouched_phase": {
                "kind": "future forward observation",
                "minimum_months": 3,
                "target_months": 6,
                "started": False,
            },
        },
        "predeclared_gates": {
            "annual_excess_win_count": {
                "10_bps": {"required": 4, "total_years": 5},
                "30_bps": {"required": 3, "total_years": 5},
                "50_bps": {"required": 3, "total_years": 5},
            },
            "compounded_excess": {
                "cost_bps": [10, 30, 50],
                "operator": ">",
                "threshold": 0.0,
            },
            "drawdown": {
                "cost_bps": 10,
                "maximum_loss_fraction": 0.40,
                "maximum_underperformance_vs_nasdaq_percentage_points": 5.0,
            },
            "leave_one_out": {
                "remove": "largest single-name contribution",
                "selection_metric": (
                    "largest net arithmetic daily return attribution"
                ),
                "removed_weight_behavior": "leave as cash; do not renormalize",
                "compounded_excess_operator": ">",
                "compounded_excess_threshold": 0.0,
            },
            "gate_failure": "release_status remains BLOCKED",
        },
        "interpretation_guardrail": (
            "This freeze authorizes exactly one research-only historical replay. "
            "It does not authorize promotion, trading, formal-data replacement, "
            "or describing the exposed 2025-2026 interval as untouched."
        ),
    }
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(protocol, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        **protocol,
        "output": {"path": str(output_path), "sha256": _sha256(output_path)},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    args = parser.parse_args()
    report = build(args.output)
    print(json.dumps({
        "protocol_status": report["protocol_status"],
        "candidate_count": report["model_grid"]["candidate_count"],
        "financial_gap_evidence_count": len(
            report["input_bindings"]["financial_gap_evidence"]
        ),
        "excluded_signal_dates": report["execution"]["excluded_signal_dates"],
        "final_data_replay_executed": report["final_data_replay_executed"],
        "release_status": report["release_status"],
        "output": report["output"],
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
