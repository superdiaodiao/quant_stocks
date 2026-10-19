"""Daily forward-observation run with health checks: quant/observation/daily.py (workflow
.github/workflows/forward_observation_daily.yml; docs/forward_observation_checklist.md B0.8)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from quant.observation import daily  # noqa: E402

if __name__ == "__main__":
    sys.exit(daily.main())
