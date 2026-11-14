from types import SimpleNamespace

from src import ai


def _config(fallback):
    return SimpleNamespace(openrouter_fallback_model=fallback, openrouter_api_key="sk-or-test")


def test_a_free_model_never_falls_back_to_a_paid_one(monkeypatch):
    monkeypatch.delenv("OPENROUTER_DATA_COLLECTION", raising=False)
    model = ai._build_single_model(_config("openai/gpt-6-luna"), "openrouter:nvidia/nemotron-3.5-super-vl:free")
    assert model.settings["openrouter_models"] == ["openrouter/free"]
    assert model.settings["openrouter_provider"]["data_collection"] == "deny"


def test_a_paid_model_keeps_its_configured_backup():
    model = ai._build_single_model(_config("openai/gpt-6-luna"), "openrouter:anthropic/claude-sonnet-5.5")
    assert model.settings["openrouter_models"] == ["openai/gpt-6-luna"]


def test_data_collection_can_be_allowed_for_free_endpoints(monkeypatch):
    monkeypatch.setenv("OPENROUTER_DATA_COLLECTION", "allow")
    model = ai._build_single_model(_config("openrouter/free"), "openrouter:nvidia/nemotron-3.5-super-vl:free")
    assert model.settings["openrouter_provider"]["data_collection"] == "allow"
    monkeypatch.setenv("OPENROUTER_DATA_COLLECTION", "maybe")
    model = ai._build_single_model(_config("openrouter/free"), "openrouter:nvidia/nemotron-3.5-super-vl:free")
    assert model.settings["openrouter_provider"]["data_collection"] == "deny"
