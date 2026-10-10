import json
from pathlib import Path

from scripts.research_v8_shadow_activate import activate


def test_activation_enables_only_local_shadow(tmp_path: Path):
    runtime = tmp_path / "runtime.py"
    runtime.write_text("pass\n")
    import hashlib
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "policy_status": "FROZEN_FORWARD_ONLY",
        "release_status": "BLOCKED",
        "forward_evidence_start": "2026-09-01",
        "bindings": {"runtime_code": {
            str(runtime): hashlib.sha256(runtime.read_bytes()).hexdigest(),
        }},
    }))
    state = tmp_path / "state.json"
    result = activate(manifest, state)
    assert result["enabled"] is True
    assert result["mode"] == "MANUAL_LOCAL_SHADOW"
    assert result["broker_action_authorized"] is False
    assert result["github_workflow_enabled"] is False
