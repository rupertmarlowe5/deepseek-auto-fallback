"""Minimal Argolink example for deepseek-v4-pro; no request is made on import."""
from __future__ import annotations
import os
import requests

BASE = "https://api.argolink.io"
MODEL = "deepseek-v4-pro"

def request():
    key = os.environ["ARGOLINK_API_KEY"]
    body = {"model": MODEL, "messages": [{"role": "user", "content": "Return one short JSON object with key status."}]}
    response = requests.post(BASE + "/v1/chat/completions", headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, json=body, timeout=60)
    response.raise_for_status()
    return response.json()

if __name__ == "__main__":
    print(request())
