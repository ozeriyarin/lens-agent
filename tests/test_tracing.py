"""Tracing is a no-op without a token (007 FR-5) and never raises."""

import app.tracing as tracing


def test_tracing_is_a_noop_without_token(monkeypatch):
    monkeypatch.delenv("LOGFIRE_TOKEN", raising=False)
    assert tracing.configure() is False


def test_tracing_warns_but_does_not_raise_when_logfire_is_missing(monkeypatch, caplog):
    import builtins

    real_import = builtins.__import__

    def no_logfire(name, *a, **k):
        if name == "logfire":
            raise ImportError("no logfire")
        return real_import(name, *a, **k)

    monkeypatch.setenv("LOGFIRE_TOKEN", "x")
    monkeypatch.setattr(builtins, "__import__", no_logfire)
    assert tracing.configure() is False
    assert any("not installed" in r.getMessage() for r in caplog.records)
