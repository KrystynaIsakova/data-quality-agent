import pytest

from dq_agent import config


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    monkeypatch.setattr(config, "ENV_PATH", path)
    for name in config.ALL_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    return path


def test_edits_of_env_are_picked_up_without_restart(env_file):
    env_file.write_text("GEMINI_API_KEY=k\nDATABASE_URL=postgresql://u:p@old-host/db\n")
    assert "old-host" in config.load_settings().database_url
    env_file.write_text("GEMINI_API_KEY=k\nDATABASE_URL=postgresql://u:p@new-host/db\n")
    assert "new-host" in config.load_settings().database_url


def test_env_file_is_not_written_into_process_environment(env_file):
    import os
    env_file.write_text("GEMINI_API_KEY=k\nDATABASE_URL=postgresql://u:p@h/db\n")
    config.load_settings()
    assert "DATABASE_URL" not in os.environ


def test_shell_variables_take_precedence(env_file, monkeypatch):
    env_file.write_text("GEMINI_API_KEY=k\nDATABASE_URL=postgresql://u:p@file-host/db\n")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@shell-host/db")
    assert "shell-host" in config.load_settings().database_url


def test_missing_values_are_named_not_shown(env_file):
    env_file.write_text("GEMINI_API_KEY=secret-key\nDATABASE_URL=\n")
    with pytest.raises(config.ConfigError) as info:
        config.load_settings()
    assert "DATABASE_URL" in str(info.value) and "secret-key" not in str(info.value)
