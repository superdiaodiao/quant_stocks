"""One-command daily data refresh and shadow recommendation pipeline."""

from __future__ import annotations

import argparse
from datetime import date, timedelta

from src.io.financial_update import update_financials, update_sec_fallback
from src.io.fundamentals_update import update_fundamentals
from src.io.nasdaq_update import update_all
from src.research.data_audit import require_project_data
from src.research.can_slim_daily_recommendations import (
    generate_can_slim_shadow_recommendations,
    save_can_slim_shadow_recommendations,
)
from src.research.shadow_evaluation import evaluate_history


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-update", action="store_true", help="Use all existing local data")
    parser.add_argument("--skip-market-update", action="store_true")
    parser.add_argument("--skip-financial-update", action="store_true")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--output-dir", default="output/daily")
    args = parser.parse_args()
    as_of = date.today() - timedelta(days=1)
    if not args.skip_update and not args.skip_market_update:
        update_all(end=as_of, workers=args.workers)
    if not args.skip_update and not args.skip_financial_update:
        update_financials(as_of=as_of, workers=min(args.workers, 8))
        update_sec_fallback(as_of=as_of, workers=min(args.workers, 4))
        update_fundamentals(as_of=as_of, workers=min(args.workers, 4))
    require_project_data(as_of)
    recommendations, metadata = generate_can_slim_shadow_recommendations()
    output = save_can_slim_shadow_recommendations(
        recommendations, metadata, args.output_dir
    )
    print(recommendations.to_string(
        index=False, float_format=lambda value: f"{value:.4f}"
    ))
    print(metadata)
    print(output)
    model_dir = f"{args.output_dir}/{metadata['model_version']}"
    evaluation = evaluate_history(
        f"{model_dir}/recommendation_history.csv",
        f"{model_dir}/shadow_evaluation.json",
    )
    print(evaluation)


if __name__ == "__main__":
    main()
