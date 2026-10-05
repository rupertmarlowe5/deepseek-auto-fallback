# Workflow

## Intent

This draft covers **fallback and retry state machine** for `deepseek-v4-pro`. It preserves a practical documentation shape—overview, contract, example, failure handling, and tests—so an engineer can find the answer without reading a marketing page.

## Submit once

1. Validate required fields locally.
2. Send one request with an idempotency key when the endpoint supports it.
3. Persist the response ID or task ID.
4. Poll according to the model page; use bounded backoff and a deadline.
5. Record terminal status and the returned output URL or message.

A network timeout leaves the outcome unknown. Query the task or request status before deciding whether a retry is safe.

## Error handling

Treat authentication, validation, rate-limit, upstream, and timeout errors as different classes. Keep the original request body and response ID in a redacted local log. Never log `ARGOLINK_API_KEY`.
