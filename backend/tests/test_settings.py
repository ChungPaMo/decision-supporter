from pathlib import Path

from app.config import Settings


def test_env_path_is_repository_root_even_when_started_in_backend(monkeypatch):
    backend = Path(__file__).resolve().parents[1]
    monkeypatch.chdir(backend)
    assert Path(Settings.model_config["env_file"]) == backend.parent / ".env"
