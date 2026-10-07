"""The same fallback rules on top of the official OpenAI Python SDK.

Use this if your code already depends on `openai`. The SDK retries the same
model on its own (max_retries, default 2); this loop adds the move to the next
model and keeps 4xx request errors from being retried anywhere.

    pip install openai
    export OPENAI_API_KEY=... OPENAI_BASE_URL=https://argolink.io/v1
    python examples/with_openai_sdk.py
"""
import openai
from openai import OpenAI

CHAIN = ["deepseek-v4-pro", "deepseek-v4.1-flash", "glm-5.3"]
NEXT_MODEL = (
    openai.RateLimitError,          # 429 after the SDK's own retries
    openai.InternalServerError,     # 5xx after the SDK's own retries
    openai.APIConnectionError,      # includes APITimeoutError
    openai.AuthenticationError,     # 401: maybe another route has a working key
    openai.PermissionDeniedError,   # 403: the key may not be allowed this model
    openai.NotFoundError,           # 404: model not served here
)

client = OpenAI(max_retries=1, timeout=300)  # reads OPENAI_API_KEY and OPENAI_BASE_URL


def chat(messages, **params):
    errors = []
    for model in CHAIN:
        try:
            return client.chat.completions.create(model=model, messages=messages, **params)
        except NEXT_MODEL as error:
            errors.append(f"{model}: {type(error).__name__}")
        # BadRequestError (400) and UnprocessableEntityError (422) propagate: the request is wrong.
    raise RuntimeError("every model failed: " + "; ".join(errors))


if __name__ == "__main__":
    reply = chat([{"role": "user", "content": "One sentence on why idempotency keys matter."}],
                 reasoning_effort="high")
    print(reply.model, "->", reply.choices[0].message.content)
