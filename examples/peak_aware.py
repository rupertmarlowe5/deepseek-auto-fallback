"""Pick a chain by time of day.

DeepSeek bills twice its listed rate on weekdays 01:00-04:00 and 06:00-10:00 UTC.
Off peak, deepseek-v4.1-flash ($0.12 / $0.48 per 1M tokens on the default base
URL) and glm-5.3-flash ($0.12 / $0.40) cost about the same, so your preferred
model goes first. At peak the DeepSeek model doubles to $0.24 / $0.96, so cheap
traffic moves to GLM for those hours. deepseek-v4-pro at peak ($1.056 / $3.168)
is still below glm-5.3 ($1.12 / $3.52), so a quality chain can keep it first
all day.

    python examples/peak_aware.py "Summarise RFC 9110 section 15.5 in three bullets"
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from auto_fallback import FallbackChat, Route, is_deepseek_peak  # noqa: E402

CHEAP_OFF_PEAK = ["deepseek-v4.1-flash", "glm-5.3-flash"]
CHEAP_PEAK = ["glm-5.3-flash", "deepseek-v4.1-flash"]


def cheap_chat() -> FallbackChat:
    chain = CHEAP_PEAK if is_deepseek_peak() else CHEAP_OFF_PEAK
    return FallbackChat([Route(model, retries=1) for model in chain])


if __name__ == "__main__":
    prompt = sys.argv[1] if len(sys.argv) > 1 else "Say hello in five languages."
    result = cheap_chat().create([{"role": "user", "content": prompt}], reasoning_effort="high")
    print(result.text)
    cost = result.cost()
    print(f"\n{result.route.model} (peak now: {is_deepseek_peak()})"
          + (f", ~${cost:.6f}" if cost is not None else ""), file=sys.stderr)
