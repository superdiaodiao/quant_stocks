"""Forward observation of the frozen candidates (docs/forward_observation_checklist.md section B).

``runner`` (entry point: scripts/forward_observation.py, run monthly by .github/workflows/forward_observation.yml)
builds the log from the frozen lines declared in quant.strategies.registry; ``prices`` fetches / stores the B1 / B2
daily data (Tiingo, Yahoo fallback; returns-only history state); ``smisp`` builds the monthly S-MISP signal (B3).
Moved unchanged from scripts/forward_observation.py, forward_prices.py and forward_smisp.py (phase 2).

Keep this file free of imports: the runner must set REVERSAL_DATA_VERSION=v2 before quant.data.version loads.
"""
