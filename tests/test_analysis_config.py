import json

from app.core.analysis_config import AnalysisSettings


def test_blank_key_is_not_a_configured_analysis_model() -> None:
    settings = AnalysisSettings(
        provider="hive-ai", model="zai-org/glm-5.3-flash", api_key="", _env_file=None
    )
    assert settings.model_config_values() is None
    assert settings.model_selection() is None


def test_model_selection_never_persists_credentials_or_server_url() -> None:
    settings = AnalysisSettings(
        provider="hive-ai",
        model="glm",
        api_key="private-api-key",
        server_password="password",
        server_url="http://localhost:1234",
        _env_file=None,
    )
    snapshot = settings.model_selection()
    assert snapshot is not None and snapshot["model"] == "glm"
    assert "private-api-key" not in json.dumps(snapshot)
    assert "password" not in json.dumps(snapshot)
    assert "localhost" not in json.dumps(snapshot)
    assert "private-api-key" not in repr(settings)
