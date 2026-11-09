#!/usr/bin/env python3
"""Smoke-test the configured LLM endpoint.
1. list models the endpoint offers (filtered to long-context open-weight families) so the right name can be put in config
2. tiny request, then a ~60k-token request, with the configured model + fallbacks
"""
import os, re, time
from common import http_get, llm_complete, load_config

cfg = load_config()
llm = cfg["llm"]
print("provider:", llm["provider"], "| base_url:", llm.get("base_url"), "| model:", llm["model"], "| fallbacks:", llm.get("fallback_models"))

key = os.environ.get(llm.get("api_key_env", ""), "")
try:
    js = http_get(f"{llm['base_url'].rstrip('/')}/models", params={"detailed": "true"},
                  headers={"Authorization": f"Bearer {key}"}).json()
    rows = js.get("data", js if isinstance(js, list) else [])
    picks = []
    for m in rows:
        mid = m.get("id", "")
        if re.search(r"deepseek|glm|kimi|qwen|minimax|mimo|llama", mid, re.I):
            ctx = m.get("context_length") or 0
            if not ctx or ctx >= 100000:
                picks.append((mid, ctx, (m.get("pricing") or {}).get("prompt")))
    print(f"models endpoint: {len(rows)} models; long-context open-weight candidates ({len(picks)}):")
    for mid, ctx, price in sorted(picks)[:60]:
        print(f"    {mid:45s} ctx={ctx or '?':<8} prompt$/M={price}")
except Exception as e:
    print("  models endpoint failed:", str(e)[:300])

for label, prompt in [("small", "Reply with exactly: ok"),
                      ("large (~60k tokens)", "Count the words in the following text and reply with the number only.\n\n" + ("pulling simulation " * 30000))]:
    t0 = time.time()
    try:
        out = llm_complete(cfg, prompt)
        print(f"  {label}: OK in {time.time()-t0:.1f}s -> {out[:80]!r}")
    except Exception as e:
        print(f"  {label}: FAILED in {time.time()-t0:.1f}s -> {str(e)[:600]}")
