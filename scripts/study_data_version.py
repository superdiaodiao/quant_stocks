"""Moved to quant/data/version.py (phase 1 extracted it; phase 3 folded the two copies into one and added v2.1).
Importing this file returns that module itself, so ``scripts.study_data_version`` and ``quant.data.version`` are the
same object (a monkeypatch of one is a monkeypatch of the other)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from quant.data import version as _version  # noqa: E402

sys.modules[__name__] = _version
