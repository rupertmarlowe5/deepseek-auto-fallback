"""Offline tests: urlopen is replaced by a scripted fake, so no key or network is needed."""
import datetime as dt
import io
import json
import os
import socket
import sys
import urllib.error

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import auto_fallback as af  # noqa: E402

OK = {"choices": [{"message": {"role": "assistant", "content": "hi"}}],
      "usage": {"prompt_tokens": 1000, "completion_tokens": 200, "prompt_cache_hit_tokens": 400}}


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def http_error(url, code, body=b'{"error":{"message":"x"}}', headers=None):
    return urllib.error.HTTPError(url, code, "error", headers or {}, io.BytesIO(body))


@pytest.fixture
def api(monkeypatch):
    """Script responses per model: a list of dicts (200 JSON), ints (HTTP status) or exceptions."""
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("FALLBACK_CHAIN", raising=False)
    monkeypatch.setattr(af.time, "sleep", lambda s: sleeps.append(s))
    script, calls, sleeps = {}, [], []

    def urlopen(request, timeout=None):
        body = json.loads(request.data)
        assert request.get_header("User-agent", "").startswith("deepseek-auto-fallback/")
        calls.append((request.full_url, body))
        step = script[body["model"]].pop(0)
        if isinstance(step, Exception):
            raise step
        if isinstance(step, tuple):
            raise http_error(request.full_url, step[0], headers=step[1])
        if isinstance(step, int):
            raise http_error(request.full_url, step)
        if isinstance(step, bytes):
            return FakeResponse(step)
        return FakeResponse(json.dumps(step).encode())

    monkeypatch.setattr(af.urllib.request, "urlopen", urlopen)
    return script, calls, sleeps


def test_first_model_answers(api):
    script, calls, _ = api
    script["deepseek-v4-pro"] = [OK]
    result = af.FallbackChat().create([{"role": "user", "content": "hi"}], reasoning_effort="high")
    assert result.text == "hi" and result.route.model == "deepseek-v4-pro"
    url, body = calls[0]
    assert url == "https://argolink.io/v1/chat/completions"
    assert body["reasoning_effort"] == "high" and body["stream"] is False


def test_503_is_retried_then_falls_back(api):
    script, calls, sleeps = api
    script["deepseek-v4-pro"] = [503, 503]
    script["deepseek-v4.1-flash"] = [OK]
    result = af.FallbackChat().create([{"role": "user", "content": "hi"}])
    assert [b["model"] for _, b in calls] == ["deepseek-v4-pro", "deepseek-v4-pro", "deepseek-v4.1-flash"]
    assert result.route.model == "deepseek-v4.1-flash"
    assert [a.status for a in result.attempts] == [503, 503, 200]
    assert len(sleeps) == 1


def test_400_fails_fast(api):
    script, calls, _ = api
    script["deepseek-v4-pro"] = [400]
    with pytest.raises(af.RequestRejected) as raised:
        af.FallbackChat().create([{"role": "user", "content": "hi"}])
    assert raised.value.status == 400 and len(calls) == 1


def test_404_skips_to_next_route_without_retry(api):
    script, calls, sleeps = api
    script["deepseek-v4-pro"] = [404]
    script["deepseek-v4.1-flash"] = [OK]
    af.FallbackChat().create([{"role": "user", "content": "hi"}])
    assert len(calls) == 2 and sleeps == []


def test_429_honours_short_retry_after_and_skips_long_one(api):
    script, calls, sleeps = api
    script["deepseek-v4-pro"] = [(429, {"Retry-After": "2"}), OK]
    af.FallbackChat().create([{"role": "user", "content": "hi"}])
    assert sleeps == [2.0]

    script["deepseek-v4-pro"] = [(429, {"Retry-After": "60"})]
    script["deepseek-v4.1-flash"] = [OK]
    result = af.FallbackChat().create([{"role": "user", "content": "hi"}])
    assert result.route.model == "deepseek-v4.1-flash" and sleeps == [2.0]


def test_timeout_counts_as_transient(api):
    script, _, _ = api
    script["deepseek-v4-pro"] = [socket.timeout("timed out"), urllib.error.URLError("reset")]
    script["deepseek-v4.1-flash"] = [OK]
    result = af.FallbackChat().create([{"role": "user", "content": "hi"}])
    assert [a.status for a in result.attempts] == [None, None, 200]


def test_all_routes_failing_reports_every_attempt(api):
    script, _, _ = api
    script["a"] = [500, 500]
    script["b"] = [401]
    with pytest.raises(af.AllRoutesFailed) as raised:
        af.FallbackChat(["a", "b"]).create([{"role": "user", "content": "hi"}])
    assert [a.status for a in raised.value.attempts] == [500, 500, 401]


def test_body_is_adapted_per_model():
    body = {"messages": [{"role": "user", "content": "x"}], "reasoning_effort": "none",
            "max_completion_tokens": 300_000}
    glm = af.adapt_body(af.Route("glm-5.3"), body)
    assert "reasoning_effort" not in glm and glm["max_completion_tokens"] == 128_000
    flash = af.adapt_body(af.Route("deepseek-v4.1-flash"), body)
    assert flash["reasoning_effort"] == "none" and flash["max_completion_tokens"] == 300_000
    pinned = af.adapt_body(af.Route("glm-5.3", params={"reasoning_effort": "high"}, drop=("max_completion_tokens",)), body)
    assert pinned["reasoning_effort"] == "high" and "max_completion_tokens" not in pinned


def test_image_requests_skip_text_only_models(api):
    script, calls, _ = api
    script["deepseek-v4.1-flash"] = [OK]
    image = [{"role": "user", "content": [{"type": "text", "text": "what is this?"},
                                          {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}}]}]
    result = af.FallbackChat().create(image)
    assert [b["model"] for _, b in calls] == ["deepseek-v4.1-flash"]
    assert result.attempts[0].error.startswith("skipped")


def test_peak_windows_and_cost():
    monday = dt.datetime(2026, 10, 5, tzinfo=dt.timezone.utc)
    assert af.is_deepseek_peak(monday.replace(hour=2))
    assert af.is_deepseek_peak(monday.replace(hour=9, minute=59))
    assert not af.is_deepseek_peak(monday.replace(hour=5))
    assert not af.is_deepseek_peak(monday.replace(day=10, hour=2))  # Saturday
    route = af.Route("deepseek-v4-pro")
    usage = OK["usage"]  # 600 uncached + 400 cached input, 200 output
    off_peak = (600 * 0.528 + 400 * 0.0176 + 200 * 1.584) / 1e6
    assert af.estimate_cost(route, usage, monday.replace(hour=12)) == round(off_peak, 6)
    assert af.estimate_cost(route, usage, monday.replace(hour=2)) == round(off_peak * 2, 6)
    assert af.estimate_cost(af.Route("glm-5.3"), usage, monday.replace(hour=2)) == round(
        (600 * 1.12 + 400 * 0.208 + 200 * 3.52) / 1e6, 6)


def sse(*events):
    return b"".join(b"data: " + json.dumps(e).encode() + b"\n\n" for e in events) + b"data: [DONE]\n\n"


def test_stream_falls_back_only_before_first_chunk(api):
    script, _, _ = api
    chunk = {"choices": [{"delta": {"content": "he"}}]}
    script["deepseek-v4-pro"] = [503, sse({"error": {"message": "overloaded"}})]
    script["deepseek-v4.1-flash"] = [sse(chunk, {"choices": [{"delta": {"content": "llo"}}]})]
    chunks = list(af.FallbackChat().stream([{"role": "user", "content": "hi"}]))
    assert chunks[0]["_route"] == "deepseek-v4.1-flash"
    assert "".join(c["choices"][0]["delta"]["content"] for c in chunks) == "hello"


def test_stream_error_after_output_is_not_replayed(api):
    script, calls, _ = api
    script["deepseek-v4-pro"] = [sse({"choices": [{"delta": {"content": "he"}}]}, {"error": {"message": "boom"}})]
    stream = af.FallbackChat().stream([{"role": "user", "content": "hi"}])
    assert next(stream)["_route"] == "deepseek-v4-pro"
    with pytest.raises(af.StreamInterrupted):
        next(stream)
    assert len(calls) == 1


def test_routes_load_from_json(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "x")
    path = tmp_path / "routes.json"
    path.write_text(json.dumps({"routes": [
        {"model": "deepseek-v4-pro"},
        {"model": "deepseek-v4-pro", "base_url": "https://api.deepseek.com", "api_key_env": "DEEPSEEK_API_KEY",
         "prices": [0.66, 0.022, 1.98]},
    ]}))
    chat = af.FallbackChat.from_file(str(path))
    assert chat.routes[1].url() == "https://api.deepseek.com/chat/completions"
    assert chat.routes[1].prices == (0.66, 0.022, 1.98)


def test_chain_from_environment(monkeypatch):
    monkeypatch.setenv("FALLBACK_CHAIN", "glm-5.3-flash, deepseek-v4.1-flash")
    assert [r.model for r in af.FallbackChat().routes] == ["glm-5.3-flash", "deepseek-v4.1-flash"]
