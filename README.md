# DeepSeek Auto Fallback: retries and model failover for the DeepSeek V4 API

A one-file Python client (standard library only) that sends a Chat Completions request to `deepseek-v4-pro`, retries the errors worth retrying, and falls back down a chain you choose: `deepseek-v4.1-flash`, `glm-5.3`, or the same model on a second provider. Each model gets the parameters it actually accepts.

[Quick start](#quick-start) · [Default chain](#the-default-chain) · [What triggers a fallback](#what-triggers-a-fallback) · [Streaming](#streaming) · [Cost per tier](#what-each-tier-costs) · [FAQ](#faq)

<sub>Last checked: 2026-10-07, against DeepSeek's API docs, Z.ai's pricing page and the [DeepSeek V4 Pro model page](https://argolink.io/gh-deepseek-auto-fallback) the defaults point at.</sub>

## Why DeepSeek calls need a fallback

- **429 at the concurrency cap.** DeepSeek limits each account to 500 concurrent `deepseek-v4-pro` requests and 2,500 on Flash, counted until the response completes. Its own [error-code page](https://api-docs.deepseek.com/quick_start/error_codes) suggests temporarily switching to another provider when you hit 429.
- **503 "Server Overloaded" and 500** under heavy traffic, documented on the same page as "retry after a brief wait".
- **Long, always-thinking answers.** V4 Pro reasons before it answers and can write up to 393,216 output tokens, so a slow or stuck request can hold a worker for a long time unless a timeout cuts it off.
- **Peak pricing.** DeepSeek bills twice its off-peak rate on weekdays 01:00–04:00 and 06:00–10:00 UTC ([pricing](https://api-docs.deepseek.com/quick_start/pricing)). A fallback chain is also a place to route around that.

## Quick start

```bash
git clone https://github.com/rupertmarlowe5/deepseek-auto-fallback
cd deepseek-auto-fallback
export OPENAI_API_KEY=your-key        # one key for every model in the default chain
python auto_fallback.py "Explain optimistic locking in two sentences" --effort high
```

The default base URL is `https://argolink.io/v1`, where one key reaches the DeepSeek and GLM models in the chain ([get a key](https://argolink.io/gh-deepseek-auto-fallback)). Point `OPENAI_BASE_URL` at any other OpenAI-compatible endpoint to change it. `pip install -e .` adds a `deepseek-fallback` command; there are no dependencies.

The answer goes to stdout and a summary to stderr. After two 503s from V4 Pro, with a 20,000-token prompt and a 2,000-token answer, the summary reads:

```text
served by deepseek-v4.1-flash | attempts: deepseek-v4-pro=503, deepseek-v4-pro=503, deepseek-v4.1-flash=200 | ~$0.003360
```

In code:

```python
from auto_fallback import FallbackChat, Route, RequestRejected, AllRoutesFailed

chat = FallbackChat([
    Route("deepseek-v4-pro", retries=1),
    Route("deepseek-v4.1-flash", retries=1),
    Route("glm-5.3", params={"reasoning_effort": "high"}),
])

try:
    result = chat.create([{"role": "user", "content": "Write a SQL query for a 7-day rolling average"}],
                         reasoning_effort="max", max_completion_tokens=8000)
    print(result.text)
    print(result.route.model, result.cost())          # which model answered, USD estimate
    for a in result.attempts:                          # every try, including skipped routes
        print(a.route, a.status, a.error, round(a.seconds, 2))
except RequestRejected as e:                           # 400/413/422: fix the request, don't retry
    print(e.status, e.body)
except AllRoutesFailed as e:
    print(e)
```

## The default chain

| # | Model ID | Why it is in the chain | Context | Max output | Images | Thinking |
|---|---|---|---:|---:|---|---|
| 1 | `deepseek-v4-pro` | DeepSeek's strongest model | 1,048,576 | 393,216 | no | always on |
| 2 | `deepseek-v4.1-flash` | same vendor, separate capacity (DeepSeek sets its concurrency limits per model) | 1,048,576 | 393,216 | yes | on; `reasoning_effort: "none"` turns it off |
| 3 | `glm-5.3` | a different vendor (Z.ai), so it survives a DeepSeek-wide outage | 1,000,000 | 128,000 | no | always on |

Context, output and thinking rules are the ones listed on each model's page for the default base URL. Other chains that make sense:

| Goal | Chain |
|---|---|
| Quality first | `deepseek-v4-pro` → `glm-5.3` → `deepseek-v4.1-flash` |
| Cheapest that works | `deepseek-v4.1-flash` → `glm-5.3-flash` → `deepseek-v4-pro` |
| Same model, two providers | `deepseek-v4-pro` (gateway) → `deepseek-v4-pro` on `api.deepseek.com` → `glm-5.3` ([examples/routes.json](examples/routes.json)) |
| Images in the prompt | `deepseek-v4.1-flash` → `glm-5.3-flash` (text-only models are skipped automatically) |

Set a chain without code: `FALLBACK_CHAIN=glm-5.3-flash,deepseek-v4.1-flash`, `--chain a,b,c`, or `--config routes.json`.

## What triggers a fallback

| Response | What the client does | Why |
|---|---|---|
| 200 | returns it | |
| 408, 500, 502, 503, 504 | retries the same route (`retries`, exponential backoff with jitter), then moves on | server-side and transient; DeepSeek documents 500 and 503 as "retry after a brief wait" |
| 429 with `Retry-After` ≤ 5 s | waits that long and retries the same route, if `retries` allows | a short queue is cheaper to wait out |
| 429 with a longer `Retry-After`, or still 429 after retries | moves to the next route | you are at a concurrency cap; another model has its own |
| Timeout, connection reset, DNS error | retries, then moves on | see [Timeouts](#timeouts) for the double-billing caveat |
| 401, 402, 403, 404 | moves on at once, no retry | this route cannot serve you (bad key, no balance, model not allowed or not found); the next one might |
| 400, 413, 422 and other 4xx | raises `RequestRejected` at once | the request is wrong and will fail everywhere; fix it instead of paying for retries |
| Image parts sent to a text-only model | skips the route and records `skipped` | `deepseek-v4-pro` and `glm-5.3` read no images |

`max_retry_after` (default 5 s) and `backoff` (default 1 s) are arguments of `FallbackChat`.

## Parameters that don't carry over

A request written for one model is not always valid for the next. Before each route, `adapt_body()` makes these changes; add your own with `Route(params={...}, drop=(...))`.

| Field | `deepseek-v4-pro` | `deepseek-v4.1-flash` | `glm-5.3` | What the adapter does |
|---|---|---|---|---|
| `reasoning_effort` | `low` · `high` · `max` (default `high`) | `none` · `low` · `high` · `max` | `low` · `high` · `max` (default `max`) | drops `"none"` for models that always think: V4 Pro ignores it, GLM-5.3 refuses it |
| `max_completion_tokens` / `max_tokens` | up to 393,216 | up to 393,216 | up to 128,000 | clamps to the model's limit |
| image parts | not read | read | not read | skips text-only routes |

One rule the adapter can't fix: DeepSeek's [thinking-mode guide](https://api-docs.deepseek.com/guides/thinking_mode) requires every earlier `reasoning_content` to be sent back when a request carries `tools`, or it returns 400. Moving a tool-calling conversation between vendors halfway through can trip this; start such conversations on the model that will finish them.

## Streaming

`chat.stream(messages, **params)` yields Chat Completions chunks. Fallback is only possible **before the first chunk**: a 5xx, a timeout, or an `error` event as the first event moves on as usual. Once text has reached your code, a broken stream raises `StreamInterrupted` instead of replaying the prompt on another model, which would print a second, different answer after the first half of the original one. The first chunk carries a `_route` key naming the model that is answering.

```python
for chunk in chat.stream([{"role": "user", "content": "Draft a changelog entry"}]):
    for choice in chunk.get("choices", []):
        print(choice["delta"].get("content") or "", end="", flush=True)
```

## Timeouts

`Route.timeout` (default 300 s) applies to every socket read, not to the whole request:

- **Non-streaming**, the server sends nothing until the answer is complete, so the timeout has to cover the longest answer you allow (`max_completion_tokens`, up to 393,216 on V4 Pro).
- **Streaming**, it becomes the longest silence allowed between chunks, so a much shorter value (say 60 s) detects a stalled request early.

A timeout does not prove the provider stopped working on the request. A retried non-streaming request can be generated, and billed, twice. If that matters more than latency, set `retries=0` on expensive routes and let the chain move to the next model instead.

## What each tier costs

USD per 1M tokens, checked 2026-10-07.

| Model | Gateway input | Cached input | Output | Official input / output | Official source |
|---|---:|---:|---:|---:|---|
| `deepseek-v4-pro` | $0.528 | $0.0176 | $1.584 | $0.66 / $1.98 | [DeepSeek](https://api-docs.deepseek.com/quick_start/pricing) |
| `deepseek-v4.1-flash` (`deepseek-flash` at DeepSeek) | $0.12 | $0.0024 | $0.48 | $0.15 / $0.60 | [DeepSeek](https://api-docs.deepseek.com/quick_start/pricing) |
| `glm-5.3` | $1.12 | $0.208 | $3.52 | $1.40 / $4.40 | [Z.ai](https://docs.z.ai/guides/overview/pricing) |
| `glm-5.3-flash` | $0.12 | $0.024 | $0.40 | $0.15 / $0.50 | [Z.ai](https://docs.z.ai/guides/overview/pricing) |

"Gateway" is the default base URL, ArgoLink, priced 20% under each official rate on its [model pages](https://argolink.io/en/models/deepseek-v4-pro). DeepSeek rows show the off-peak price in both columns: DeepSeek and the gateway both bill DeepSeek models at **twice** these rates on weekdays 01:00–04:00 and 06:00–10:00 UTC, which is 09:00–12:00 and 14:00–18:00 Beijing time. DeepSeek exempts Chinese public holidays; the gateway's page does not mention them. Reasoning tokens count as output tokens.

One request with 20,000 input tokens (no cache hits) and 2,000 output tokens, reasoning included:

| Model | Off-peak | DeepSeek peak | Per 1,000 such requests (off-peak) |
|---|---:|---:|---:|
| `deepseek-v4-pro` | $0.0137 | $0.0275 | $13.73 |
| `deepseek-v4.1-flash` | $0.0034 | $0.0067 | $3.36 |
| `glm-5.3` | $0.0294 | same | $29.44 |
| `glm-5.3-flash` | $0.0032 | same | $3.20 |

Two things follow. V4 Pro at peak ($1.056 / $3.168) is still cheaper than GLM-5.3 ($1.12 / $3.52), so a quality chain can keep it first all day. Flash at peak ($0.24 / $0.96) costs more than GLM-5.3 Flash, so a cheap chain should swap order for those hours: [examples/peak_aware.py](examples/peak_aware.py) does it with `is_deepseek_peak()`. `Result.cost()` applies the peak multiplier to `deepseek-*` models; give other providers their own `Route(prices=(input, cached, output))`.

## Configuration

| Setting | Default | Per route / per chain |
|---|---|---|
| API key | `OPENAI_API_KEY` | `Route(api_key_env=...)` |
| Base URL | `OPENAI_BASE_URL`, else `https://argolink.io/v1` | `Route(base_url=...)` |
| Chain | `FALLBACK_CHAIN`, else the three models above | `FallbackChat([...])`, `--chain`, `--config` |
| Retries per route | 1 | `Route(retries=...)` |
| Read timeout | 300 s | `Route(timeout=...)` |
| Longest `Retry-After` worth waiting for | 5 s | `FallbackChat(max_retry_after=...)` |

A JSON config is a list of `Route` fields:

```json
{"routes": [
  {"model": "deepseek-v4-pro"},
  {"model": "deepseek-v4-pro", "base_url": "https://api.deepseek.com", "api_key_env": "DEEPSEEK_API_KEY",
   "prices": [0.66, 0.022, 1.98], "retries": 0},
  {"model": "glm-5.3", "params": {"reasoning_effort": "high"}}
]}
```

Model names differ between providers: DeepSeek's own API calls Flash `deepseek-flash`, and still accepts the retired `deepseek-v4-flash` name, serving it with V4.1 Flash.

Already on the `openai` package? [examples/with_openai_sdk.py](examples/with_openai_sdk.py) applies the same rules with the SDK's exception classes (`RateLimitError`, `InternalServerError`, `APIConnectionError`, ...). The SDK's own `max_retries` (default 2) retries the same model; the loop adds the move to the next one.

## Server-side alternatives

If you would rather not ship fallback logic in every service, the same idea exists as a gateway feature:

| Tool | How you declare the fallback | Docs |
|---|---|---|
| OpenRouter | a `models` array in the request; billed at the model that answered | [model fallbacks](https://openrouter.ai/docs/guides/routing/model-fallbacks) |
| LiteLLM proxy | `fallbacks: [{"model-a": ["model-b"]}]`, plus `num_retries` and `context_window_fallbacks` | [reliability](https://docs.litellm.ai/docs/proxy/reliability) |

This client covers what they don't: no extra hop or service to run, a chain that changes with the time of day, and per-model parameter fixes inside your own code.

## FAQ

**Does a failed attempt cost anything?**
An attempt that ends in an HTTP error returns no `usage`, so the client records no cost for it. The exception is a timeout or a broken connection: the provider may have finished, and billed, the answer you never received.

**Why not just retry the same model more times?**
A 429 means you are at a concurrency cap and a 503 means the model is overloaded; retrying in a tight loop adds load where there is none to spare. A second model has its own capacity, which is also what DeepSeek's error page recommends for 429.

**Can it fall back in the middle of a streamed answer?**
No, on purpose. After the first chunk the client raises `StreamInterrupted`; decide in your code whether to keep the partial answer or start over.

**Which errors are never retried?**
400, 413, 422 and other request errors: retrying costs latency and changes nothing. 401/402/403/404 are not retried on the same route but do move to the next, which may use a different key or provider.

**Is the peak window applied automatically?**
Only to cost estimates and to `is_deepseek_peak()`. The chain order changes only if you choose it by time, as in [examples/peak_aware.py](examples/peak_aware.py). Chinese public holidays are not modelled, so estimates on those days come out high.

**Will GLM give the same answer as DeepSeek?**
No. It is a different model; the fallback trades some consistency for availability. Keep the attempts list in your logs so you can tell which model wrote each answer.

## Tests

```bash
pip install pytest
pytest tests/
```

The tests replace `urlopen` with a scripted fake (503s, 429 with `Retry-After`, timeouts, SSE streams), so they need no key and make no network calls.

## License

MIT
