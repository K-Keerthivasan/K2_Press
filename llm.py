"""Thin LLM wrapper — swap base_url in config.yaml to point at any OpenAI-compatible endpoint."""
from __future__ import annotations
import json
import re
from pathlib import Path

import requests
import yaml
from openai import OpenAI

_cfg: dict | None = None


def _config() -> dict:
    global _cfg
    if _cfg is None:
        with open("config.yaml", encoding="utf-8") as f:
            _cfg = yaml.safe_load(f)
    return _cfg


def reload_config() -> None:
    global _cfg
    _cfg = None


def _client(model_override: str | None = None) -> tuple[OpenAI, str]:
    llm = _config()["llm"]
    client = OpenAI(
        base_url=llm["base_url"],
        api_key="ollama",
    )
    model = model_override or llm["model"]
    return client, model


def _clean_json(text: str) -> dict:
    """Strip thinking tags and extract the first JSON object."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            return json.loads(m.group())
        raise ValueError(f"No valid JSON in model output:\n{text[:400]}")


def chat_json(system: str, user: str, model: str | None = None) -> dict:
    """Send a chat request that returns structured JSON."""
    client, mdl = _client(model)
    resp = client.chat.completions.create(
        model=mdl,
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
        response_format={"type": "json_object"},
        temperature=0.3,
    )
    return _clean_json(resp.choices[0].message.content)


def list_models(base_url: str | None = None) -> list[str]:
    """Return list of models available in the local Ollama instance."""
    url = base_url or _config()["llm"]["base_url"]
    # Ollama base_url is like http://localhost:11434/v1 — strip /v1
    ollama_root = url.replace("/v1", "").rstrip("/")
    try:
        r = requests.get(f"{ollama_root}/api/tags", timeout=5)
        r.raise_for_status()
        return [m["name"] for m in r.json().get("models", [])]
    except Exception:
        return []


def set_model(model: str) -> None:
    """Update the active model in memory (not persisted to disk)."""
    _config()["llm"]["model"] = model
