#!/usr/bin/env python3
"""Smoke-test the configured LLM endpoint: a tiny request, then a ~60k-token request, printing timing and errors."""
import time
from common import llm_complete, load_config

cfg = load_config()
print("provider:", cfg["llm"]["provider"], "| base_url:", cfg["llm"].get("base_url"), "| model:", cfg["llm"]["model"])
for label, prompt in [("small", "Reply with exactly: ok"),
                      ("large (~60k tokens)", "Count the words in the following text and reply with the number only.\n\n" + ("pulling simulation " * 30000))]:
    t0 = time.time()
    try:
        out = llm_complete(cfg, prompt)
        print(f"  {label}: OK in {time.time()-t0:.1f}s -> {out[:80]!r}")
    except Exception as e:
        print(f"  {label}: FAILED in {time.time()-t0:.1f}s -> {str(e)[:600]}")
