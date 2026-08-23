from pathlib import Path
import plistlib


PLIST = Path("ops/com.quant-stocks.v6-shadow.plist")


def test_v6_launchd_template_is_prepared_but_not_self_installing() -> None:
    payload = plistlib.loads(PLIST.read_bytes())

    assert payload["Label"] == "com.quant-stocks.v6-shadow"
    assert payload["ProgramArguments"][-1].endswith(
        "/scripts/research_v6_scheduled_run.py"
    )
    assert len(payload["StartCalendarInterval"]) == 5
    assert {item["Weekday"] for item in payload["StartCalendarInterval"]} == {
        2, 3, 4, 5, 6
    }
    assert all(item["Hour"] == 9 for item in payload["StartCalendarInterval"])
    assert "launchctl" not in PLIST.read_text()
