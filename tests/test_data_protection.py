"""Protection of the frozen data versions (docs/architecture.md section 13): manifests, verify, lock / unlock, the
pipeline's refusal to write a frozen version, the account-wide Tiingo budget and the mega-cap studies' own ledgers.
No data, no network: every file is in tmp_path."""
import gzip
import json
import os
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from quant.data import protect
from quant.data import version as dv
from quant.data.sources import http
from quant.data.sources import megacap_oos2 as md
from quant.data.sources import tiingo as tq

ROOT = Path(__file__).resolve().parents[1]
MONTH = datetime.now(timezone.utc).strftime("%Y-%m")


def make_version(tmp_path):
    cache = tmp_path / "research_cache" / "rev"
    (cache / "prices").mkdir(parents=True)
    (cache / "prices" / "A.csv").write_text("date,close\n2020-01-02,1\n")
    (cache / "raw_index.csv.gz").write_bytes(gzip.compress(b"x\n", mtime=0))
    (cache / ".DS_Store").write_text("finder")                    # skipped, as in the backups
    checkout = tmp_path / "checkout"
    inputs = checkout / dv.INPUTS_PARENT / dv.INPUTS_NAME["v1"]
    inputs.mkdir(parents=True)
    (inputs / "security_master.csv").write_text("security_id\n1\n")
    (inputs / "special_distributions.csv").write_text("sid\n")   # optional (untracked)
    m = protect.build_manifest("v1", inputs_root=checkout, cache=cache, tracked=["security_master.csv"])
    path = tmp_path / "manifests" / "v1.json.gz"
    protect.write_manifest(m, path)
    return cache, checkout, inputs, path


def verify(tmp_path, cache, checkout, quick=False):
    return protect.verify_version("v1", tmp_path / "manifests", checkout, cache=cache, quick=quick)


def test_manifest_has_paths_sizes_and_hashes_only(tmp_path):
    cache, checkout, inputs, path = make_version(tmp_path)
    m = protect.read_manifest(path)
    assert [r[0] for r in m["cache"]["files"]] == ["prices/A.csv", "raw_index.csv.gz"]
    assert m["inputs"]["files"][0][:2] == ["security_master.csv", len("security_id\n1\n")]
    assert m["optional_inputs"]["files"][0][0] == "special_distributions.csv"
    assert all(len(r) == 3 and len(r[2]) == 64 for r in m["cache"]["files"])
    assert gzip.decompress(path.read_bytes())[:1] == b"{" and path.read_bytes()[4:8] == b"\0\0\0\0"   # gzip mtime 0


def test_verify_passes_then_reports_changed_missing_and_unexpected_files(tmp_path):
    cache, checkout, inputs, path = make_version(tmp_path)
    r = verify(tmp_path, cache, checkout)
    assert r["ok"] and r["cache"]["checked"] == 2 and r["inputs"]["checked"] == 1
    (cache / "prices" / "A.csv").write_text("date,close\n2020-01-02,2\n")      # same size, other bytes
    assert not verify(tmp_path, cache, checkout, quick=True)["cache"]["changed"]  # --quick: sizes only
    r = verify(tmp_path, cache, checkout)
    assert not r["ok"] and r["cache"]["changed"] == ["prices/A.csv"]
    (cache / "raw_index.csv.gz").unlink()
    (cache / "prices" / "B.csv").write_text("new")
    (inputs / "extra.csv").write_text("x")
    r = verify(tmp_path, cache, checkout)
    assert r["cache"]["missing"] == ["raw_index.csv.gz"] and r["cache"]["unexpected"] == ["prices/B.csv"]
    assert r["inputs"]["unexpected"] == ["extra.csv"]


def test_an_absent_optional_input_is_reported_but_not_a_failure(tmp_path):
    cache, checkout, inputs, path = make_version(tmp_path)
    (inputs / "special_distributions.csv").unlink()
    r = verify(tmp_path, cache, checkout)
    assert r["ok"] and r["optional_inputs"]["absent_optional"] == ["special_distributions.csv"]
    (inputs / "special_distributions.csv").write_text("changed\n")
    assert verify(tmp_path, cache, checkout)["optional_inputs"]["changed"] == ["special_distributions.csv"]


def test_the_verify_command_exits_non_zero_on_a_problem(tmp_path, monkeypatch):
    cache, checkout, inputs, path = make_version(tmp_path)
    monkeypatch.setattr(dv, "cache_dir", lambda v: cache)
    args = ["verify", "--version", "v1", "--manifest-dir", str(tmp_path / "manifests"), "--inputs-root", str(checkout)]
    assert protect.main(args) == 0
    (inputs / "security_master.csv").write_text("security_id\n2\n")
    assert protect.main(args) == 1
    assert protect.main(["verify", "--version", "v2", "--manifest-dir", str(tmp_path / "manifests")]) == 1   # none


def test_manifest_command_never_overwrites_without_force(tmp_path, monkeypatch, capsys):
    cache, checkout, inputs, path = make_version(tmp_path)
    before = path.read_bytes()
    assert protect.main(["manifest", "--version", "v1", "--manifest-dir", str(tmp_path / "manifests")]) == 1
    assert path.read_bytes() == before and "--force" in capsys.readouterr().out


def test_lock_makes_the_cache_read_only_and_unlock_restores_it(tmp_path, monkeypatch):
    cache, checkout, inputs, path = make_version(tmp_path)
    monkeypatch.setattr(dv, "cache_dir", lambda v: cache)
    assert protect.main(["lock", "--version", "v1"]) == 0
    try:
        if os.geteuid() != 0:   # root ignores permission bits
            with pytest.raises(PermissionError):
                (cache / "prices" / "A.csv").write_text("overwritten")
            with pytest.raises(PermissionError):
                (cache / "prices" / "new.csv").write_text("new file")
        assert not stat.S_IMODE((cache / "prices").stat().st_mode) & protect.WRITE_BITS
        r = verify(tmp_path, cache, checkout)
        assert r["ok"] and r["locked"]
    finally:
        assert protect.main(["unlock", "--version", "v1"]) == 0
    (cache / "prices" / "new.csv").write_text("writable again")
    assert stat.S_IMODE((cache / "prices" / "A.csv").stat().st_mode) == 0o444 | stat.S_IWUSR
    assert not verify(tmp_path, cache, checkout)["locked"]


def test_the_committed_manifests_cover_every_frozen_version():
    for v in dv.frozen_versions():
        path = protect.manifest_path(v)
        assert path.is_file(), path
        m = protect.read_manifest(path)
        assert m["version"] == v and m["frozen_utc"] == dv.FROZEN_UTC[v]
        assert m["cache"]["root"] == f"research_cache/{dv.CACHE_NAME[v]}" and m["cache"]["file_count"] > 1000
        names = {r[0] for r in m["inputs"]["files"]}
        tracked = protect.tracked_files(ROOT, dv.INPUTS_PARENT / dv.INPUTS_NAME[v])
        assert names == set(tracked)
        inputs = dv.inputs_dir(v)
        for name, size, digest in m["inputs"]["files"]:   # the committed inputs are what the manifest says
            assert (inputs / name).stat().st_size == size and protect.sha256(inputs / name) == digest
        assert set(m) == {"schema", "version", "frozen_utc", "created_utc", "cache", "inputs", "optional_inputs"}


# ------------------------------------------------------------------ the pipeline refuses to write a frozen version

def test_every_version_is_frozen_today():
    assert dv.frozen_versions() == ["v1", "v2", "v2.1"] and all(dv.is_frozen(v) for v in dv.VERSIONS)


def test_refuse_frozen_stops_a_write_into_a_frozen_cache_unless_allowed(tmp_path):
    from pipelines.reversal_data import common
    argv, allow = common.frozen_flag(["--offline", "--allow-frozen-rebuild"])
    assert argv == ["--offline"] and allow
    with pytest.raises(SystemExit, match="--allow-frozen-rebuild"):
        common.refuse_frozen("step", False)                       # default targets: the version's cache and inputs
    with pytest.raises(SystemExit):
        common.refuse_frozen("step", False, [dv.cache_dir("v2.1") / "prices" / "x.csv"])
    common.refuse_frozen("step", True)
    common.refuse_frozen("step", False, [tmp_path / "scratch"])   # elsewhere: nothing to refuse


@pytest.mark.parametrize("step,argv", [("prefilter", ["--offline"]), ("yahoo", []), ("reconcile", ["--rebuild"]),
                                       ("terminal", ["--offline"]), ("earnings", ["--offline"]), ("universe", []),
                                       ("validate", []), ("security_master", ["--offline"]), ("listings", []),
                                       ("form25", ["--offline"]), ("tiingo", ["--offline"]), ("review", [])])
def test_each_writing_step_refuses_a_frozen_version_before_doing_anything(step, argv):
    import importlib
    module = importlib.import_module(f"pipelines.reversal_data.{step}")
    with pytest.raises(SystemExit) as stop:
        module.main(argv)
    assert "--allow-frozen-rebuild" in str(stop.value.code)


def test_the_runner_refuses_every_frozen_version_without_the_flag():
    for version in dv.VERSIONS:
        out = subprocess.run([sys.executable, "-m", "pipelines.reversal_data.build", "--version", version],
                             cwd=ROOT, capture_output=True, text=True,
                             env={**{k: v for k, v in os.environ.items() if k != "REVERSAL_DATA_VERSION"},
                                  "PYTHONPATH": str(ROOT)})
        assert out.returncode != 0 and "--allow-frozen-rebuild" in out.stderr, out.stderr


def test_the_runner_passes_the_flag_to_each_step(monkeypatch, tmp_path):
    from pipelines.reversal_data import build
    seen = []
    monkeypatch.setattr(build.subprocess, "run",
                        lambda cmd, **kw: seen.append(cmd) or subprocess.CompletedProcess(cmd, 0))
    build.run_step("v1", "prefilter", ["prefilter", "--offline"], tmp_path, allow_frozen=True)
    build.run_step("v1", "yahoo", ["yahoo"], tmp_path)
    assert seen[0][-2:] == ["--offline", "--allow-frozen-rebuild"] and "--allow-frozen-rebuild" not in seen[1]


# ------------------------------------------------------------------ the Tiingo account's monthly budget

def ledger(path, rows, month=MONTH):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("fetched_utc,source,month,symbol,status\n"
                    + "".join(f"{month}-02T00:00:00+00:00,{src},{month},{sym},200\n" for src, sym in rows))
    return path


def test_month_symbols_are_unique_across_all_ledgers(tmp_path, monkeypatch):
    a = ledger(tmp_path / "rev" / "quota_ledger.csv", [("tiingo", "AAA"), ("tiingo", "bbb"), ("yahoo", "CCC")])
    b = ledger(tmp_path / "megacap" / "quota_ledger.csv", [("tiingo", "BBB"), ("tiingo", "DDD")])
    c = ledger(tmp_path / "old" / "quota_ledger.csv", [("tiingo", "EEE")], month="2001-01")
    monkeypatch.setattr(tq, "account_ledgers", lambda: [a, b, c, a])
    assert tq.month_symbols() == {"AAA", "BBB", "DDD"}
    assert tq.month_symbols("2001-01") == {"EEE"}
    assert tq.check_budget(["AAA", "XXX", "YYY"], stop=4) == (["AAA", "XXX"], ["YYY"])
    assert tq.check_budget(["XXX"], stop=4, already_used=1) == ([], ["XXX"])
    with pytest.raises(ValueError):
        tq.check_budget(["XXX"], stop=501)


def test_account_ledgers_find_every_cache_ledger_and_the_forward_one(tmp_path, monkeypatch):
    root = tmp_path / "research_cache"
    a = ledger(root / "reversal_2012_2026" / "quota_ledger.csv", [("tiingo", "A")])
    b = ledger(root / "megacap_oos2" / "quota_ledger.csv", [("tiingo", "B")])
    f = ledger(tmp_path / ".cache" / "quota_ledger.csv", [("tiingo", "F")])
    monkeypatch.setattr(dv, "CACHE_ROOT", root)
    monkeypatch.setenv("FORWARD_OBS_CACHE", str(tmp_path / ".cache"))
    assert tq.account_ledgers() == [b, a, f] or set(tq.account_ledgers()) == {a, b, f}
    assert tq.month_symbols() == {"A", "B", "F"}


def test_the_megacap_studies_log_to_their_own_ledgers(monkeypatch, tmp_path):
    assert md.LEDGER.raw_index.parent == md.CACHE and md.LEDGER.quota.parent == md.CACHE
    assert not str(md.LEDGER.quota).startswith(str(dv.cache_dir("v1")))
    seen = {}
    monkeypatch.setattr(http, "cached_get", lambda url, path, ledger, **kw: seen.setdefault("ledger", ledger) and b"")
    md.cached_get("https://example.com", tmp_path / "x")
    assert seen["ledger"] == md.LEDGER
    assert md.study_ledger(tmp_path) == http.Ledger(tmp_path / "raw_index.csv.gz", tmp_path / "quota_ledger.csv")


def test_megacap_lookback_reads_a_cached_tiingo_answer_whatever_the_month(monkeypatch, tmp_path):
    from quant.data import megacap_lookback as ml
    body = json.dumps([{"date": "1998-01-02T00:00:00.000Z", "close": 10.0, "adjClose": 10.0, "divCash": 0.0,
                        "splitFactor": 1.0, "open": 10, "high": 10, "low": 10, "volume": 1}]).encode()
    cache = tmp_path / "raw/tiingo" / "AAA__1997-09-01_1998-07-31.json.gz"
    cache.parent.mkdir(parents=True)
    cache.write_bytes(gzip.compress(body, mtime=0))
    monkeypatch.setattr(md, "tiingo_month_symbols", lambda: set())       # not asked this month
    out = ml.tiingo_pre("AAA", tmp_path, "1997-09-01", "1998-07-31")
    assert out is not None and len(out) == 1
    assert ml.tiingo_pre("BBB", tmp_path, "1997-09-01", "1998-07-31") is None   # not cached, not counted: not asked


def test_the_forward_fetch_asks_tiingo_only_within_the_budget(monkeypatch, tmp_path):
    from scripts import forward_observation as fo
    full = ledger(tmp_path / "other" / "quota_ledger.csv", [("tiingo", f"S{k}") for k in range(500)])
    monkeypatch.setattr(tq, "account_ledgers", lambda: [full])
    asked = []
    out = fo.fetch(("QQQ",), raw_dir=tmp_path / "raw", log=tmp_path / "log.csv", history=None, source="tiingo",
                   tiingo_getter=lambda url, headers: asked.append(url), getter=lambda u: b"{}",
                   sleep=lambda s: None)
    assert asked == [] and out == {"QQQ": "not daily / empty"}      # went to Yahoo (here: an empty answer)
    monkeypatch.setattr(tq, "account_ledgers", lambda: [])

    def refuse(url, headers):
        asked.append(url)
        raise RuntimeError("no rows")
    fo.fetch(("QQQ",), raw_dir=tmp_path / "raw", log=tmp_path / "log.csv", history=None, source="tiingo",
             tiingo_getter=refuse, getter=lambda u: b"{}", sleep=lambda s: None)
    assert len(asked) == 1
    rows = (tmp_path / "quota_ledger.csv").read_text().splitlines()
    assert rows[0] == "fetched_utc,source,month,symbol,status" and rows[1].split(",")[1:] == ["tiingo", MONTH,
                                                                                               "QQQ", "RuntimeError"]
    assert tq.month_symbols(extra=[tmp_path / "quota_ledger.csv"]) == {"QQQ"}
