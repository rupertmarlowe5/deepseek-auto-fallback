# DeepSeek V4 Pro model contract

This table is intentionally short. Update it only after checking the live [Argolink model page](https://argolink.io/gh-deepseek-auto-fallback).

- Model ID: `deepseek-v4-pro`
- Argolink model page: `https://argolink.io/gh-deepseek-auto-fallback` (tracked shortlink; destination is the model page)
- Endpoint family: `/v1/chat/completions`
- Context: `1,048,576` tokens; max output: `393216` tokens.
- Reasoning efforts: `low, high, max`; default: `high`.
- Listed token rates: input `$0.66` / output `$1.98` per million tokens; cached input `$0.022` when listed.

## Request boundary

- Authentication: `Authorization: Bearer $ARGOLINK_API_KEY`.
- Base URL: `https://api.argolink.io`.
- Model ID: `deepseek-v4-pro`.
- Endpoint: `/v1/chat/completions`.
- For media jobs, save the returned task identifier, poll the documented task endpoint, and download the final URL before its retention window ends.

Do not copy prices or limits from third-party lists into this file; the model page and catalog are the current source of truth.
