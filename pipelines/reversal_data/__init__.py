"""The reversal 2012-2026 data pipeline (docs/reversal_2012_2026_data_plan.md): one module per step.

Version switch and paths: ``quant.data.version`` (v1 / v2 / v2.1). Fetchers and parsers of each vendor:
``quant.data.sources``. The fixed-point runner of a version: ``build`` (``python -m pipelines.reversal_data.build
--version v2.1``). Every old command ``scripts/reversal_data_<step>.py`` still works (a forwarding wrapper).

Kept free of imports: a step runs as ``python -m pipelines.reversal_data.<step>``.
"""
