#!/usr/bin/env bash
set -euo pipefail
: "${ARGOLINK_API_KEY:?Set ARGOLINK_API_KEY in the environment}"

curl -fsS https://api.argolink.io/v1/chat/completions \
  -H "Authorization: Bearer $ARGOLINK_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-v4-pro","messages":[{"role":"user","content":"Return one short JSON object with key status."}]}'
