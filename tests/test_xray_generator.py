"""test_xray_generator.py — unit tests for xray_generator module."""

import time

import pytest

import xray_generator
from xray_generator import model_chain, _complete


# ── fixture: clean failure state between tests ───────────────────────────────

@pytest.fixture(autouse=True)
def clean_failures():
    """Clear the circuit-breaker state before and after every test."""
    xray_generator._model_failures.clear()
    yield
    xray_generator._model_failures.clear()


# ═══════════════════════════════════════════════════════════════════════════════
# model_chain
# ═══════════════════════════════════════════════════════════════════════════════

class TestModelChain:

    def test_openai_primary_starts_chain(self, monkeypatch):
        # Ensure an OpenAI key is present so chain builder can add fallbacks
        monkeypatch.setenv("MARGINALIA_OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv("MARGINALIA_MODEL_CHAIN", "")  # override via env cleared
        monkeypatch.setattr(xray_generator, "MODEL_CHAIN_ENV", "")
        chain = model_chain("openai:gpt-4o")
        assert chain[0] == "openai:gpt-4o"

    def test_anthropic_primary_starts_chain(self, monkeypatch):
        monkeypatch.setenv("MARGINALIA_ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setattr(xray_generator, "MODEL_CHAIN_ENV", "")
        chain = model_chain("anthropic:claude-opus-4-5")
        assert chain[0] == "anthropic:claude-opus-4-5"

    def test_bedrock_primary_includes_fallback(self, monkeypatch):
        monkeypatch.setattr(xray_generator, "MODEL_CHAIN_ENV", "")
        chain = model_chain("us.anthropic.claude-sonnet-4-6")
        assert "us.anthropic.claude-sonnet-4-6" in chain
        # Chain should have at least one entry
        assert len(chain) >= 1

    def test_env_var_override_returns_exact_models(self, monkeypatch):
        monkeypatch.setattr(xray_generator, "MODEL_CHAIN_ENV",
                            "modelA,modelB,modelC")
        chain = model_chain()
        assert chain == ["modelA", "modelB", "modelC"]

    def test_env_var_stripped_of_whitespace(self, monkeypatch):
        monkeypatch.setattr(xray_generator, "MODEL_CHAIN_ENV",
                            " modelA , modelB ")
        chain = model_chain()
        assert chain == ["modelA", "modelB"]

    def test_chain_is_list(self, monkeypatch):
        monkeypatch.setattr(xray_generator, "MODEL_CHAIN_ENV", "")
        chain = model_chain("openai:gpt-4o-mini")
        assert isinstance(chain, list)


# ═══════════════════════════════════════════════════════════════════════════════
# _complete (with mocked _invoke_one)
# ═══════════════════════════════════════════════════════════════════════════════

class TestComplete:

    def test_returns_first_successful_output(self, monkeypatch):
        monkeypatch.setattr(xray_generator, "MODEL_CHAIN_ENV", "model-a,model-b")
        monkeypatch.setattr(
            xray_generator, "_invoke_one",
            lambda prompt, model_id, **kw: "hello from model-a",
        )
        result = _complete("test prompt")
        assert result == "hello from model-a"

    def test_falls_through_to_next_on_failure(self, monkeypatch):
        monkeypatch.setattr(xray_generator, "MODEL_CHAIN_ENV", "model-a,model-b")
        call_log = []

        def fake_invoke(prompt, model_id, **kw):
            call_log.append(model_id)
            if model_id == "model-a":
                raise RuntimeError("model-a down")
            return "response from model-b"

        monkeypatch.setattr(xray_generator, "_invoke_one", fake_invoke)
        result = _complete("test prompt")
        assert result == "response from model-b"
        assert "model-a" in call_log
        assert "model-b" in call_log

    def test_raises_runtime_error_when_all_fail(self, monkeypatch):
        monkeypatch.setattr(xray_generator, "MODEL_CHAIN_ENV", "model-a,model-b")

        def always_fail(prompt, model_id, **kw):
            raise RuntimeError(f"{model_id} failed")

        monkeypatch.setattr(xray_generator, "_invoke_one", always_fail)
        with pytest.raises(RuntimeError):
            _complete("test prompt")

    def test_records_failure_in_model_failures(self, monkeypatch):
        monkeypatch.setattr(xray_generator, "MODEL_CHAIN_ENV", "bad-model,good-model")

        def fake_invoke(prompt, model_id, **kw):
            if model_id == "bad-model":
                raise RuntimeError("down")
            return "ok"

        monkeypatch.setattr(xray_generator, "_invoke_one", fake_invoke)
        _complete("test")
        assert "bad-model" in xray_generator._model_failures

    def test_clears_failure_on_success(self, monkeypatch):
        monkeypatch.setattr(xray_generator, "MODEL_CHAIN_ENV", "model-a")
        # Pre-populate a stale failure
        xray_generator._model_failures["model-a"] = time.time() - 99999

        monkeypatch.setattr(
            xray_generator, "_invoke_one",
            lambda prompt, model_id, **kw: "ok",
        )
        _complete("test")
        assert "model-a" not in xray_generator._model_failures


# ═══════════════════════════════════════════════════════════════════════════════
# circuit breaker
# ═══════════════════════════════════════════════════════════════════════════════

class TestCircuitBreaker:

    def test_recently_failed_model_skipped(self, monkeypatch):
        monkeypatch.setattr(xray_generator, "MODEL_CHAIN_ENV", "bad-model,good-model")
        monkeypatch.setattr(xray_generator, "MODEL_COOLDOWN_S", 99999.0)
        # Mark bad-model as having just failed
        xray_generator._model_failures["bad-model"] = time.time()

        called_models = []

        def fake_invoke(prompt, model_id, **kw):
            called_models.append(model_id)
            return "ok"

        monkeypatch.setattr(xray_generator, "_invoke_one", fake_invoke)
        result = _complete("test")
        assert result == "ok"
        assert "bad-model" not in called_models
        assert "good-model" in called_models

    def test_cooled_down_model_retried(self, monkeypatch):
        monkeypatch.setattr(xray_generator, "MODEL_CHAIN_ENV", "model-a")
        monkeypatch.setattr(xray_generator, "MODEL_COOLDOWN_S", 1.0)
        # Mark model-a as having failed more than cooldown seconds ago
        xray_generator._model_failures["model-a"] = time.time() - 10.0

        called_models = []

        def fake_invoke(prompt, model_id, **kw):
            called_models.append(model_id)
            return "recovered"

        monkeypatch.setattr(xray_generator, "_invoke_one", fake_invoke)
        result = _complete("test")
        assert result == "recovered"
        assert "model-a" in called_models

    def test_all_cooling_down_tries_full_chain(self, monkeypatch):
        """If every model is cooling down, the full chain is used anyway."""
        monkeypatch.setattr(xray_generator, "MODEL_CHAIN_ENV", "model-a,model-b")
        monkeypatch.setattr(xray_generator, "MODEL_COOLDOWN_S", 99999.0)
        xray_generator._model_failures["model-a"] = time.time()
        xray_generator._model_failures["model-b"] = time.time()

        called_models = []

        def fake_invoke(prompt, model_id, **kw):
            called_models.append(model_id)
            if model_id == "model-a":
                raise RuntimeError("still down")
            return "model-b ok"

        monkeypatch.setattr(xray_generator, "_invoke_one", fake_invoke)
        result = _complete("test")
        assert result == "model-b ok"
        # Both models were attempted (full chain used as fallback)
        assert "model-a" in called_models
        assert "model-b" in called_models
