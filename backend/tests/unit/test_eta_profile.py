from src import eta_profile as eta


def _clear(monkeypatch):
    for name in (
        "TRANSCRIPTION_PROVIDER",
        "WHISPER_ENGINE",
        "WHISPER_MODEL",
        "WHISPER_MODEL_SIZE",
        "LLM",
        "ETA_TRANSCRIBE_SECONDS_PER_MINUTE",
        "ETA_FIND_SECONDS_PER_MINUTE",
        "ETA_RENDER_SECONDS_PER_CLIP",
    ):
        monkeypatch.delenv(name, raising=False)


def test_the_hosted_setup_keeps_the_frontend_rates(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("TRANSCRIPTION_PROVIDER", "assemblyai")
    monkeypatch.setenv("LLM", "google-gla:gemini-3.5-flash")
    assert eta.eta_profile() == {
        "transcribe_seconds_per_minute": None,
        "find_seconds_per_minute": None,
        "render_seconds_per_clip": None,
    }


def test_local_whisper_and_ollama_report_cpu_rates(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("TRANSCRIPTION_PROVIDER", "whisper")
    monkeypatch.setenv("LLM", "ollama:gemma4:12b")
    profile = eta.eta_profile()
    assert profile["transcribe_seconds_per_minute"] == eta.FASTER_WHISPER_SECONDS_PER_MINUTE
    assert profile["find_seconds_per_minute"] == eta.LOCAL_LLM_FIND_SECONDS_PER_MINUTE

    monkeypatch.setenv("WHISPER_ENGINE", "openai")
    assert eta.eta_profile()["transcribe_seconds_per_minute"] == eta.OPENAI_WHISPER_SECONDS_PER_MINUTE


def test_eta_variables_override_and_bad_values_are_ignored(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("TRANSCRIPTION_PROVIDER", "whisper")
    monkeypatch.setenv("ETA_TRANSCRIBE_SECONDS_PER_MINUTE", "3")
    monkeypatch.setenv("ETA_RENDER_SECONDS_PER_CLIP", "not-a-number")
    monkeypatch.setenv("ETA_FIND_SECONDS_PER_MINUTE", "-1")
    profile = eta.eta_profile()
    assert profile["transcribe_seconds_per_minute"] == 3.0
    assert profile["render_seconds_per_clip"] is None
    assert profile["find_seconds_per_minute"] is None


def test_faster_models_report_faster_transcription(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("TRANSCRIPTION_PROVIDER", "whisper")
    monkeypatch.setenv("WHISPER_MODEL_SIZE", "large-v3-turbo")
    assert eta.eta_profile()["transcribe_seconds_per_minute"] == 9.8
    monkeypatch.setenv("WHISPER_MODEL_SIZE", "medium")
    assert eta.eta_profile()["transcribe_seconds_per_minute"] == eta.FASTER_WHISPER_SECONDS_PER_MINUTE
