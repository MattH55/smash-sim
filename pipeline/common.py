"""Shared helpers for the SMASH-SIM extraction pipeline."""
from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Iterable

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def load_config(path: str | Path = HERE / "config.yaml") -> dict:
    cfg = yaml.safe_load(Path(path).read_text())
    cfg["_root"] = ROOT
    cfg["_data"] = (ROOT / cfg.get("data_dir", "data")).resolve()
    cfg["_data"].mkdir(parents=True, exist_ok=True)
    return cfg


def load_vocab(name: str) -> dict[str, dict]:
    """Load vocab/<name>.csv keyed by its first column."""
    rows = {}
    with open(ROOT / "vocab" / f"{name}.csv", newline="") as f:
        for r in csv.DictReader(f):
            rows[r[next(iter(r))]] = r
    return rows


def load_schema(name: str) -> dict:
    return json.loads((ROOT / "schema" / f"{name}.schema.json").read_text())


def norm_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    doi = doi.strip()
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi, flags=re.I)
    doi = re.sub(r"^doi:\s*", "", doi, flags=re.I)
    return doi.lower()


def doi_to_fname(doi: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", doi)


def sha1(s: str) -> str:
    return hashlib.sha1(s.encode()).hexdigest()


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def write_jsonl(path: Path, rows: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def next_id(prefix: str, existing: Iterable[str]) -> str:
    nums = [int(x.split("-")[1]) for x in existing if x and x.startswith(prefix + "-")]
    return f"{prefix}-{(max(nums) + 1 if nums else 1):06d}"


class RateLimiter:
    def __init__(self, per_second: float):
        self.interval = 1.0 / per_second
        self._last = 0.0

    def wait(self):
        now = time.monotonic()
        delta = now - self._last
        if delta < self.interval:
            time.sleep(self.interval - delta)
        self._last = time.monotonic()


def http_get(url: str, params: dict | None = None, headers: dict | None = None, retries: int = 3, timeout: int = 60):
    import requests

    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=timeout)
            if r.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            return r
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)


def llm_complete(cfg: dict, prompt: str) -> str:
    """Single-turn completion. Provider-agnostic so the pipeline runs with any JSON-capable model."""
    llm = cfg["llm"]
    api_key = os.environ.get(llm.get("api_key_env", "ANTHROPIC_API_KEY"))
    if not api_key:
        raise RuntimeError(f"Set {llm.get('api_key_env')} in the environment (GitHub: repo secret of the same name)")
    import requests

    if llm["provider"] == "anthropic":
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={
                "model": llm["model"],
                "max_tokens": llm.get("max_tokens", 8000),
                "temperature": llm.get("temperature", 0),
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=600,
        )
        r.raise_for_status()
        return "".join(b.get("text", "") for b in r.json()["content"])
    elif llm["provider"] == "openai_compatible":
      models = [llm["model"]] + list(llm.get("fallback_models") or [])
      last_err = None
      for model in models:
       for attempt in range(2):
        r = requests.post(
            f"{llm['base_url'].rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "content-type": "application/json"},
            json={
                "model": model,
                "max_tokens": llm.get("max_tokens", 8000),
                "temperature": llm.get("temperature", 0),
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=600,
        )
        if r.status_code in (429, 500, 502, 503, 504) and attempt == 0:
            time.sleep(10); continue
        if r.status_code in (402, 404):          # not in subscription / unknown model -> try next model
            last_err = f"{model}: HTTP {r.status_code}: {r.text[:300]}"
            break
        if r.status_code >= 400:
            raise RuntimeError(f"{llm['base_url']} [{model}] -> HTTP {r.status_code}: {r.text[:400]}")
        js = r.json()
        if "choices" not in js:
            raise RuntimeError(f"unexpected response shape: {str(js)[:400]}")
        return js["choices"][0]["message"]["content"]
      raise RuntimeError(f"all models failed; last: {last_err}")
    raise ValueError(f"unknown provider {llm['provider']}")


def extract_json_array(text: str) -> list[Any]:
    """Pull the first JSON array out of a model response, tolerating code fences."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        raise ValueError("no JSON array in response")
    return json.loads(text[start : end + 1])
