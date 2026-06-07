#!/bin/sh
set -eu

python - <<'PY'
import os
from pathlib import Path

import yaml

config_path = Path("config.yaml")
host_config_path = Path("config.host.yaml")
base_url = os.environ.get("OLLAMA_BASE_URL", "").strip()

if host_config_path.exists():
    config_path.write_text(host_config_path.read_text(encoding="utf-8"), encoding="utf-8")

if base_url and config_path.exists():
    with config_path.open("r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    config.setdefault("llm", {})["base_url"] = base_url
    with config_path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(config, f, sort_keys=False, allow_unicode=True)
PY

exec "$@"
