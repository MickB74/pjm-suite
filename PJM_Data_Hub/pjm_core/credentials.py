"""PJM Data Hub credential store.

PJM Data Miner 2 (api.pjm.com) only requires a single API subscription key —
no username/password OAuth flow like ERCOT. Register at:
    https://api.pjm.com/

config.json schema (git-ignored, chmod 600):
    {
      "subscription_key": "...",
      "eia_api_key": "...",
      "backfill_start": "2020-01-01"
    }
"""

from __future__ import annotations

import getpass
import json
import os

from pjm_core import paths

ENV_SUBKEY = "PJM_API_SUBSCRIPTION_KEY"
ENV_EIA = "EIA_API_KEY"

_REQUIRED = ("subscription_key",)


def load_config() -> dict:
    if paths.CONFIG_PATH.exists():
        try:
            return json.loads(paths.CONFIG_PATH.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_config(cfg: dict) -> None:
    paths.CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    try:
        os.chmod(paths.CONFIG_PATH, 0o600)
    except OSError:
        pass


def have_credentials(cfg: dict | None = None) -> bool:
    cfg = cfg if cfg is not None else load_config()
    return all(cfg.get(k) for k in _REQUIRED)


def get_eia_api_key() -> str:
    return load_config().get("eia_api_key", "") or os.environ.get(ENV_EIA, "")


def save_eia_api_key(api_key: str) -> None:
    cfg = load_config()
    cfg["eia_api_key"] = api_key.strip()
    save_config(cfg)
    if api_key.strip():
        os.environ[ENV_EIA] = api_key.strip()


def export_to_env(cfg: dict | None = None) -> bool:
    cfg = cfg if cfg is not None else load_config()
    mapping = {
        ENV_SUBKEY: cfg.get("subscription_key"),
        ENV_EIA: cfg.get("eia_api_key"),
    }
    for env_key, val in mapping.items():
        if val and not os.environ.get(env_key):
            os.environ[env_key] = str(val)
    return bool(os.environ.get(ENV_SUBKEY))


def set_credentials_interactive() -> None:
    print("\nPJM Data Miner 2 API credentials")
    print("Register at https://api.pjm.com/ to get a free subscription key.\n")
    cfg = load_config()
    key = getpass.getpass(f"PJM subscription key [{'set' if cfg.get('subscription_key') else 'not set'}]: ").strip()
    if key:
        cfg["subscription_key"] = key
    eia = input(f"EIA API key (optional) [{cfg.get('eia_api_key', '')}]: ").strip()
    if eia:
        cfg["eia_api_key"] = eia
    save_config(cfg)
    print(f"\nSaved to {paths.CONFIG_PATH}")
