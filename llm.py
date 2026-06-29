"""Thin LLM wrapper — swap base_url in config.yaml to point at any OpenAI-compatible endpoint."""
from __future__ import annotations
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import requests
import yaml
from openai import OpenAI

_cfg: dict | None = None

# Runtime backend/model overrides chosen in the UI (in-memory; not persisted to
# disk). When set, they win over the K2_LLM_* env vars and config.yaml so the
# user can flip between Hermes and Ollama without restarting the app.
_override: dict[str, str | None] = {"backend": None, "base_url": None, "model": None}

# Built-in backends. config.yaml `llm.backends` may add to / override these.
_DEFAULT_BACKENDS: dict[str, dict[str, str]] = {
    "hermes": {"base_url": "hermes://cli",               "model": "hermes:gpt-5.5"},
    "ollama": {"base_url": "http://localhost:11434/v1",  "model": "qwen3:8b"},
}


def _config() -> dict:
    global _cfg
    if _cfg is None:
        with open("config.yaml", encoding="utf-8") as f:
            _cfg = yaml.safe_load(f)
    return _cfg


def reload_config() -> None:
    global _cfg
    _cfg = None


def _backends() -> dict[str, dict[str, str]]:
    """Backend presets: built-in defaults merged with any config.yaml overrides."""
    out = {k: dict(v) for k, v in _DEFAULT_BACKENDS.items()}
    for name, preset in (_config().get("llm", {}).get("backends") or {}).items():
        out.setdefault(name, {}).update(preset or {})
    return out


# Hermes is the primary backend. We only fall back to Ollama when the Hermes CLI
# isn't installed/reachable. The availability probe is cached (refresh=True to
# re-check, e.g. after the user starts Hermes and reselects it).
_hermes_ok: bool | None = None
_autofallback_done = False
# True once the app has silently switched Hermes→Ollama because the Hermes CLI
# wasn't found. The UI reads this (via backend_info) to show a persistent
# warning banner. Cleared when the user manually picks a backend.
_auto_fell_back = False


def hermes_available(refresh: bool = False) -> bool:
    """True if the Hermes CLI is installed. Cached after the first probe.

    We resolve the executable on PATH (shutil.which) instead of running
    `hermes --version`: the version command triggers Hermes' startup update
    check, which can take longer than the probe timeout on a cold boot. The
    old subprocess probe then spuriously timed out and silently latched the
    whole session onto the Ollama fallback. Resolving the binary is instant
    and reliable; we only spawn a process if the name can't be resolved on
    PATH (e.g. K2_HERMES_COMMAND is a shell alias or an absolute path)."""
    global _hermes_ok
    if _hermes_ok is not None and not refresh:
        return _hermes_ok
    cmd = os.getenv("K2_HERMES_COMMAND") or "hermes"
    if shutil.which(cmd):
        _hermes_ok = True
        return _hermes_ok
    try:
        subprocess.run([cmd, "--version"], capture_output=True, timeout=10, check=False)
        _hermes_ok = True
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        _hermes_ok = False
    return _hermes_ok


def _maybe_autofallback() -> None:
    """Once per process: if the configured backend is Hermes but the Hermes CLI
    is unavailable, switch to Ollama so post generation still works. A manual
    backend choice (override) is always respected and skips this."""
    global _autofallback_done, _auto_fell_back
    if _autofallback_done or _override["backend"]:
        return
    _autofallback_done = True
    base = os.getenv("K2_LLM_BASE_URL") or _config()["llm"]["base_url"]
    if base == "hermes://cli" and not hermes_available():
        try:
            set_backend("ollama")
            _auto_fell_back = True
            print("[llm] Hermes CLI not detected - falling back to the Ollama backend.")
        except Exception as e:  # pragma: no cover - defensive
            print(f"[llm] Hermes unavailable and Ollama fallback failed: {e}")


def _active_base_url() -> str:
    return (_override["base_url"] or os.getenv("K2_LLM_BASE_URL")
            or _config()["llm"]["base_url"])


def _active_model() -> str:
    return (_override["model"] or os.getenv("K2_LLM_MODEL")
            or _config()["llm"]["model"])


def list_backends() -> list[str]:
    return list(_backends().keys())


def current_backend() -> str:
    """Name of the active backend, inferred from the active base_url."""
    if _override["backend"]:
        return _override["backend"]
    base = _active_base_url()
    for name, preset in _backends().items():
        if preset.get("base_url") == base:
            return name
    return "hermes" if base == "hermes://cli" else "ollama"


def set_backend(backend: str) -> dict:
    """Switch the active LLM backend (e.g. 'hermes' or 'ollama') at runtime."""
    backends = _backends()
    if backend not in backends:
        raise ValueError(f"Unknown backend '{backend}'. Choose from {list(backends)}.")
    global _auto_fell_back
    # Re-probe Hermes on a manual pick — the user may have just started it. If
    # they ask for Hermes but its CLI still isn't there, stay on Ollama and keep
    # the warning up rather than committing to an unusable backend.
    if backend == "hermes" and not hermes_available(refresh=True):
        _override.update(_backends()["ollama"], backend="ollama")
        _auto_fell_back = True
        return backend_info()
    preset = backends[backend]
    _override["backend"]  = backend
    _override["base_url"] = preset.get("base_url")
    _override["model"]    = preset.get("model")
    # A deliberate, usable pick clears the auto-fallback warning.
    _auto_fell_back = False
    return backend_info()


def backend_info() -> dict:
    _maybe_autofallback()
    return {"backend":          current_backend(),
            "backends":         list_backends(),
            "base_url":         _active_base_url(),
            "model":            _active_model(),
            "hermes_available": hermes_available(),
            "auto_fell_back":   _auto_fell_back,
            "primary":          "hermes"}


def _messages_to_prompt(messages: list[dict], *, json_only: bool = False) -> str:
    parts = []
    for msg in messages:
        role = msg.get("role", "user").upper()
        content = msg.get("content") or ""
        parts.append(f"{role}:\n{content}")
    if json_only:
        parts.append("INSTRUCTION:\nReply with only one valid JSON object. Do not use markdown.")
    return "\n\n".join(parts)


def _hermes_model_arg(model: str) -> str | None:
    # Only honour explicit Hermes model names ("hermes:<x>"). A stale non-Hermes
    # model (e.g. an Ollama "qwen3:8b" left selected in the UI) must NOT be passed
    # as `hermes -m qwen3:8b` — that makes Hermes produce no final response and
    # fails every call. Fall back to Hermes' own default instead.
    if not model or not model.startswith("hermes:"):
        return None
    return model.removeprefix("hermes:")


class _HermesCompletions:
    def create(self, **kwargs):
        if kwargs.get("tools"):
            raise RuntimeError("Hermes CLI backend uses the JSON fallback for tool calls.")
        model = _hermes_model_arg(kwargs.get("model") or "")
        messages = kwargs.get("messages") or []
        response_format = kwargs.get("response_format") or {}
        prompt = _messages_to_prompt(messages, json_only=response_format.get("type") == "json_object")
        cmd = [os.getenv("K2_HERMES_COMMAND") or "hermes", "-z", prompt]
        if model:
            cmd[1:1] = ["-m", model]
        timeout = int(os.getenv("K2_HERMES_TIMEOUT", "300"))
        try:
            out = subprocess.run(
                cmd,
                cwd=os.getcwd(),
                text=True,
                encoding="utf-8",      # Hermes emits UTF-8; without this Windows
                errors="replace",      # decodes as cp1252 → mojibake (’ → â€™).
                capture_output=True,
                timeout=timeout,
                check=True,
            )
        except subprocess.CalledProcessError as e:
            detail = (e.stderr or e.stdout or str(e)).strip()
            raise RuntimeError(f"Hermes CLI failed: {detail}") from e
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=out.stdout.strip()))]
        )


class _HermesClient:
    def __init__(self):
        self.chat = SimpleNamespace(completions=_HermesCompletions())


def _client(model_override: str | None = None):
    _maybe_autofallback()
    llm = _config()["llm"]
    base_url = _active_base_url()
    model = model_override or _active_model()
    if base_url == "hermes://cli":
        return _HermesClient(), model
    client = OpenAI(
        base_url=base_url,
        api_key=os.getenv("K2_LLM_API_KEY") or llm.get("api_key") or "ollama",
    )
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


def chat_json(system: str, user: str, model: str | None = None,
              retries: int = 1) -> dict:
    """Send a chat request that returns structured JSON.

    Local models occasionally emit malformed JSON; retry a couple of times
    (nudging the model to output strict JSON) before giving up so a single
    flaky response doesn't fail a whole batch item.
    """
    client, mdl = _client(model)
    messages = [
        {"role": "system", "content": system},
        {"role": "user",   "content": user},
    ]
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        resp = client.chat.completions.create(
            model=mdl,
            messages=messages,
            response_format={"type": "json_object"},
            temperature=0.3 if attempt == 0 else 0.1,
        )
        try:
            return _clean_json(resp.choices[0].message.content)
        except (ValueError, json.JSONDecodeError) as e:
            last_err = e
            messages.append({"role": "user",
                             "content": "Your previous reply was not valid JSON. "
                                        "Reply again with ONLY a single valid JSON object."})
    raise last_err  # type: ignore[misc]


def list_models(base_url: str | None = None) -> list[str]:
    """Return models from Ollama or any OpenAI-compatible local server."""
    _maybe_autofallback()
    url = base_url or _active_base_url()
    if url == "hermes://cli":
        return [_active_model() or "hermes"]
    root = url.rstrip("/")
    ollama_root = root.removesuffix("/v1")
    try:
        r = requests.get(f"{ollama_root}/api/tags", timeout=5)
        r.raise_for_status()
        models = [m["name"] for m in r.json().get("models", [])]
        if models:
            return models
    except Exception:
        pass

    try:
        api_key = os.getenv("K2_LLM_API_KEY") or _config()["llm"].get("api_key") or "ollama"
        r = requests.get(
            f"{root}/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=5,
        )
        r.raise_for_status()
        return [m.get("id") for m in r.json().get("data", []) if m.get("id")]
    except Exception:
        return []


def set_model(model: str) -> None:
    """Update the active model in memory (not persisted to disk)."""
    _override["model"] = model
    _config()["llm"]["model"] = model
