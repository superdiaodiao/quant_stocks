from scripts.research_v8_weekly_mark import record_weekly_mark


def test_weekly_mark_does_not_write_before_week_end():
    result = record_weekly_mark(as_of="2026-08-10")
    assert result["status"] == "WAITING_FOR_WEEK_END"
    assert result["written"] is False
    assert result["release_status"] == "BLOCKED"
