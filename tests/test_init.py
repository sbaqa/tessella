from tessella.config import OllamaLocal, Settings
from tessella.integrations import ollama_runtime
from tessella.setup.wizard import _render_yaml
import yaml


def test_is_up_false_on_unbound_port():
    assert ollama_runtime.is_up(1) is False  # port 1 is never a usable Ollama server


def test_rendered_yaml_round_trips_through_settings():
    answers = {
        "reports": ["playwright-report/"],
        "traces": ["test-results/**/trace.zip"],
        "rerun_command": "pytest {test_id} -q",
        "rerun_attempts": 2,
        "reader": {"provider": "ollama_chat", "name": "qwen3.5:4b", "base_url": "http://127.0.0.1:11435"},
        "vision": None,
        "ollama_enabled": True,
        "ollama_port": 11435,
        "ollama_models_dir": ".tessella/models",
        "board": "github",
        "jira": None,
        "github": {"repo": "me/repo", "token_env": "GITHUB_TOKEN", "labels": ["bug"]},
        "network_allow": ["localhost:11435", "api.github.com"],
    }
    text = _render_yaml(answers)
    settings = Settings.model_validate(yaml.safe_load(text))

    assert settings.board == "github"
    assert settings.github.repo == "me/repo"
    assert settings.github.labels == ["bug"]
    assert settings.ollama == OllamaLocal(enabled=True, port=11435, models_dir=".tessella/models")
    assert settings.models.reader.base_url == "http://127.0.0.1:11435"
    assert settings.models.vision is None
    assert settings.network.allow == ["localhost:11435", "api.github.com"]


def test_rendered_yaml_omits_board_when_none():
    answers = {
        "reports": ["playwright-report/"],
        "traces": ["test-results/**/trace.zip"],
        "rerun_command": "pytest {test_id} -q",
        "rerun_attempts": 2,
        "reader": {"provider": "ollama_chat", "name": "qwen3.5:4b"},
        "vision": None,
        "ollama_enabled": False,
        "ollama_port": 11435,
        "ollama_models_dir": ".tessella/models",
        "board": None,
        "jira": None,
        "github": None,
        "network_allow": ["localhost:11434"],
    }
    settings = Settings.model_validate(yaml.safe_load(_render_yaml(answers)))
    assert settings.board is None
    assert settings.ollama.enabled is False
