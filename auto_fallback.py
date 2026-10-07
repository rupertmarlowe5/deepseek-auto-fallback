"""Model fallback for DeepSeek V4 chat calls.

Send an OpenAI-style Chat Completions request to the first model in a chain,
retry the failures that are worth retrying, and move down the chain when a
model cannot answer. Standard library only (Python 3.9+).

    export OPENAI_API_KEY=...            # key for the default base URL
    python auto_fallback.py "Explain optimistic locking in two sentences"

The default base URL is ArgoLink's (https://argolink.io/v1), where one key
reaches DeepSeek and GLM models. Any OpenAI-compatible endpoint works per route.
"""
from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import http.client
import json
import os
import random
import socket
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Iterator

DEFAULT_BASE_URL = "https://argolink.io/v1"
DEFAULT_CHAIN = ("deepseek-v4-pro", "deepseek-v4.1-flash", "glm-5.3")
# Some hosts, the default base URL included, answer 403 to Python's default
# "Python-urllib/3.x" agent, so every request names this client instead.
USER_AGENT = "deepseek-auto-fallback/1.0"

# USD per 1M tokens as (input, cached input, output) on the default base URL,
# read from the model pages on argolink.io on 2026-10-07. Override per route
# with Route(prices=...) for any other provider.
PRICES = {
    "deepseek-v4-pro": (0.528, 0.0176, 1.584),
    "deepseek-v4.1-flash": (0.12, 0.0024, 0.48),
    "glm-5.3": (1.12, 0.208, 3.52),
    "glm-5.2": (1.12, 0.208, 3.52),
    "glm-5.3-flash": (0.12, 0.024, 0.40),
    "glm-5.3-flashx": (0.296, 0.06, 1.0),
}

# What each model accepts, from the same pages: max output tokens, whether it
# reads images, and whether thinking can be switched off.
MODELS = {
    "deepseek-v4-pro": {"max_output": 393_216, "images": False, "always_thinks": True},
    "deepseek-v4.1-flash": {"max_output": 393_216, "images": True, "always_thinks": False},
    "deepseek-flash": {"max_output": 393_216, "images": True, "always_thinks": False},
    "glm-5.3": {"max_output": 128_000, "images": False, "always_thinks": True},
    "glm-5.2": {"max_output": 128_000, "images": False, "always_thinks": False},
    "glm-5.3-flash": {"max_output": 128_000, "images": True, "always_thinks": True},
    "glm-5.3-flashx": {"max_output": 128_000, "images": True, "always_thinks": True},
}

# DeepSeek bills its models at twice the listed rate in two weekday windows:
# 01:00-04:00 and 06:00-10:00 UTC (09:00-12:00 and 14:00-18:00 Beijing time).
PEAK_WINDOWS_UTC = ((1, 4), (6, 10))
PEAK_BILLED_PREFIX = "deepseek-"

RETRY_STATUSES = {408, 429, 500, 502, 503, 504}   # transient: retry, then fall back
SKIP_STATUSES = {401, 402, 403, 404}              # this route cannot serve it: next route


class FallbackError(RuntimeError):
    """Base class; `attempts` lists every try that was made."""

    def __init__(self, message: str, attempts: list | None = None):
        super().__init__(message)
        self.attempts = attempts or []


class RequestRejected(FallbackError):
    """A 4xx that says the request itself is wrong (400, 413, 422...). Not retried."""

    def __init__(self, message: str, status: int, body: Any, attempts: list):
        super().__init__(message, attempts)
        self.status = status
        self.body = body


class AllRoutesFailed(FallbackError):
    """Every route in the chain failed or was skipped."""


class StreamInterrupted(FallbackError):
    """A stream broke after text had been delivered; it is not replayed on another model."""


@dataclass
class Route:
    model: str
    base_url: str | None = None          # default: $OPENAI_BASE_URL or DEFAULT_BASE_URL
    api_key_env: str = "OPENAI_API_KEY"
    retries: int = 1                     # extra tries on a transient error before moving on
    timeout: float = 300                 # seconds per socket read; see README "Timeouts"
    params: dict = field(default_factory=dict)   # merged over the request body for this route
    drop: tuple = ()                     # body keys to remove for this route
    prices: tuple | None = None          # (input, cached, output) USD per 1M tokens

    def url(self) -> str:
        base = self.base_url or os.environ.get("OPENAI_BASE_URL") or DEFAULT_BASE_URL
        return base.rstrip("/") + "/chat/completions"

    def label(self) -> str:
        return self.model if not self.base_url else f"{self.model}@{self.base_url}"


@dataclass
class Attempt:
    route: str
    status: int | None      # HTTP status, None for a network error or a skip
    error: str
    seconds: float


@dataclass
class Result:
    response: dict
    route: Route
    attempts: list

    @property
    def text(self) -> str:
        return self.response["choices"][0]["message"].get("content") or ""

    def cost(self, when: dt.datetime | None = None) -> float | None:
        return estimate_cost(self.route, self.response.get("usage") or {}, when)


def is_deepseek_peak(when: dt.datetime | None = None) -> bool:
    """True inside DeepSeek's weekday peak windows (Chinese public holidays are not modelled)."""
    when = when or dt.datetime.now(dt.timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    when = when.astimezone(dt.timezone.utc)
    if when.weekday() >= 5:
        return False
    return any(start <= when.hour < end for start, end in PEAK_WINDOWS_UTC)


def estimate_cost(route: Route, usage: dict, when: dt.datetime | None = None) -> float | None:
    """USD for one response from its usage block; None when the route has no price."""
    prices = route.prices or PRICES.get(route.model)
    if not prices or not usage:
        return None
    price_in, price_cached, price_out = prices
    prompt = usage.get("prompt_tokens") or 0
    cached = usage.get("prompt_cache_hit_tokens")
    if cached is None:
        cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
    output = usage.get("completion_tokens") or 0  # reasoning tokens are counted here
    cost = ((prompt - cached) * price_in + cached * price_cached + output * price_out) / 1_000_000
    if route.model.startswith(PEAK_BILLED_PREFIX) and is_deepseek_peak(when):
        cost *= 2
    return round(cost, 6)


def _has_images(messages: list) -> bool:
    for message in messages:
        content = message.get("content")
        if isinstance(content, list) and any(part.get("type") == "image_url" for part in content):
            return True
    return False


def adapt_body(route: Route, body: dict) -> dict | None:
    """Fit a request body to one route; None means the model cannot take it (skip the route)."""
    spec = MODELS.get(route.model, {})
    out = {k: v for k, v in body.items() if k not in route.drop}
    out.update(route.params)
    out["model"] = route.model
    if spec.get("images") is False and _has_images(out.get("messages", [])):
        return None
    if spec.get("always_thinks") and out.get("reasoning_effort") == "none":
        del out["reasoning_effort"]  # ignored by deepseek-v4-pro, refused by glm-5.3
    limit = spec.get("max_output")
    for key in ("max_completion_tokens", "max_tokens"):
        if limit and isinstance(out.get(key), int) and out[key] > limit:
            out[key] = limit
    return out


def _retry_after(headers) -> float | None:
    value = headers.get("Retry-After") if headers else None
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            moment = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        return max(0.0, (moment - dt.datetime.now(dt.timezone.utc)).total_seconds())


class FallbackChat:
    def __init__(self, routes=None, max_retry_after: float = 5, backoff: float = 1.0):
        chain = routes or os.environ.get("FALLBACK_CHAIN", "").split(",")
        chain = [r for r in chain if r] or list(DEFAULT_CHAIN)
        self.routes = [r if isinstance(r, Route) else Route(r.strip()) for r in chain]
        self.max_retry_after = max_retry_after  # longer Retry-After: move on instead of waiting
        self.backoff = backoff

    @classmethod
    def from_file(cls, path: str, **kwargs) -> "FallbackChat":
        with open(path, encoding="utf-8") as handle:
            config = json.load(handle)
        routes = [Route(**{**item, "drop": tuple(item.get("drop", ())),
                           "prices": tuple(item["prices"]) if item.get("prices") else None})
                  for item in config["routes"]]
        return cls(routes, **kwargs)

    # -- transport -------------------------------------------------------
    def _open(self, route: Route, body: dict):
        key = os.environ.get(route.api_key_env)
        if not key:
            raise FallbackError(f"set {route.api_key_env} for route {route.label()}")
        request = urllib.request.Request(route.url(), data=json.dumps(body).encode(), method="POST")
        request.add_header("Authorization", f"Bearer {key}")
        request.add_header("Content-Type", "application/json")
        request.add_header("User-Agent", USER_AGENT)
        return urllib.request.urlopen(request, timeout=route.timeout)

    def _try_route(self, route: Route, body: dict, attempts: list, consume):
        """Run one route with its retries. Returns consume()'s value, or None to fall back."""
        for attempt in range(route.retries + 1):
            started = time.monotonic()
            wait = self.backoff * (2 ** attempt) * (0.5 + random.random())
            try:
                response = self._open(route, body)
                return consume(response, route, attempts, started)
            except urllib.error.HTTPError as error:
                raw = error.read().decode(errors="replace")
                try:
                    detail = json.loads(raw)
                except ValueError:
                    detail = raw
                attempts.append(Attempt(route.label(), error.code, raw[:200], time.monotonic() - started))
                if error.code in SKIP_STATUSES:
                    return None
                if error.code not in RETRY_STATUSES:
                    raise RequestRejected(f"{route.label()} rejected the request with {error.code}: {raw[:200]}",
                                          error.code, detail, attempts) from None
                retry_after = _retry_after(error.headers)
                if retry_after is not None:
                    if retry_after > self.max_retry_after:
                        return None
                    wait = retry_after
            except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError,
                    http.client.HTTPException) as error:
                reason = getattr(error, "reason", error)
                attempts.append(Attempt(route.label(), None, f"network: {reason}", time.monotonic() - started))
            if attempt < route.retries:
                time.sleep(wait)
        return None

    def _run(self, body: dict, consume):
        attempts: list = []
        for route in self.routes:
            routed = adapt_body(route, body)
            if routed is None:
                attempts.append(Attempt(route.label(), None, "skipped: model reads no images", 0.0))
                continue
            outcome = self._try_route(route, routed, attempts, consume)
            if outcome is not None:
                return outcome
        tried = "; ".join(f"{a.route}: {a.status or a.error}" for a in attempts)
        raise AllRoutesFailed(f"no route answered ({tried})", attempts)

    # -- public API ------------------------------------------------------
    def create(self, messages: list, **params) -> Result:
        """Non-streaming call. params: any Chat Completions field (reasoning_effort, tools...)."""
        body = {**params, "messages": messages, "stream": False}

        def consume(response, route, attempts, started):
            with response:
                data = json.loads(response.read())
            attempts.append(Attempt(route.label(), 200, "", time.monotonic() - started))
            return Result(data, route, attempts)

        return self._run(body, consume)

    def stream(self, messages: list, **params) -> Iterator[dict]:
        """Yield chunk dicts. Fallback is possible only until the first chunk arrives."""
        body = {**params, "messages": messages, "stream": True}

        def consume(response, route, attempts, started):
            events = _sse_events(response)
            try:
                first = next(events, None)  # anything that goes wrong here still falls back
            except BaseException:
                response.close()
                raise
            if first is None or "error" in first:
                response.close()
                reason = first["error"] if first else "stream ended before the first chunk"
                raise urllib.error.URLError(f"stream: {reason}")
            attempts.append(Attempt(route.label(), 200, "", time.monotonic() - started))
            return route, first, events, response

        route, first, rest, response = self._run(body, consume)
        yield {**first, "_route": route.label()}
        try:
            for chunk in rest:
                if "error" in chunk:
                    raise ValueError(chunk["error"])
                yield chunk
        except (OSError, ValueError, http.client.HTTPException) as error:
            raise StreamInterrupted(f"{route.label()} stream broke after output started: {error}") from None
        finally:
            response.close()


def _sse_events(response) -> Iterator[dict]:
    for raw in response:
        line = raw.decode("utf-8", errors="replace").strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            return
        yield json.loads(payload)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Chat with a fallback chain of models.")
    parser.add_argument("prompt")
    parser.add_argument("--chain", help="comma-separated model IDs (default: $FALLBACK_CHAIN or %s)"
                        % ",".join(DEFAULT_CHAIN))
    parser.add_argument("--config", help="JSON file with a routes list (see examples/routes.json)")
    parser.add_argument("--effort", help="reasoning_effort, e.g. low, high, max")
    parser.add_argument("--stream", action="store_true")
    args = parser.parse_args(argv)

    if args.config:
        chat = FallbackChat.from_file(args.config)
    else:
        chat = FallbackChat(args.chain.split(",") if args.chain else None)
    params = {"reasoning_effort": args.effort} if args.effort else {}
    messages = [{"role": "user", "content": args.prompt}]
    if args.stream:
        for chunk in chat.stream(messages, **params):
            if "_route" in chunk:
                print(f"[{chunk['_route']}]", file=sys.stderr)
            for choice in chunk.get("choices", []):
                print(choice.get("delta", {}).get("content") or "", end="", flush=True)
        print()
        return 0
    result = chat.create(messages, **params)
    print(result.text)
    tries = ", ".join(f"{a.route}={a.status or a.error}" for a in result.attempts)
    cost = result.cost()
    print(f"\nserved by {result.route.label()} | attempts: {tries}"
          + (f" | ~${cost:.6f}" if cost is not None else ""), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
