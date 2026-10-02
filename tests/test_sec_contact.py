from src.io.sec_contact import PLACEHOLDER, sec_user_agent


def test_env_variable_wins(tmp_path, monkeypatch):
    (tmp_path / ".env.sec").write_text("SEC_USER_AGENT=from file a@b.org\n", encoding="utf-8")
    monkeypatch.setenv("SEC_USER_AGENT", "from env c@d.org")
    assert sec_user_agent(tmp_path / "pkg" / "mod.py") == "from env c@d.org"


def test_nearest_env_file_above_is_read(tmp_path, monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    (tmp_path / ".env.sec").write_text('OTHER=x\nSEC_USER_AGENT="research a@b.org"\n', encoding="utf-8")
    nested = tmp_path / ".claude" / "worktrees" / "w" / "src" / "io" / "mod.py"
    assert sec_user_agent(nested) == "research a@b.org"


def test_placeholder_without_a_contact(tmp_path, monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    (tmp_path / ".env.sec").write_text("SEC_USER_AGENT=\n", encoding="utf-8")
    assert sec_user_agent(tmp_path / "a" / "mod.py") == PLACEHOLDER
