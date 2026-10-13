"""Read settings from config.toml and secrets from .env."""

import os
import sys
import tomllib
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_config() -> dict:
    load_dotenv(PROJECT_ROOT / ".env")
    with open(PROJECT_ROOT / "config.toml", "rb") as f:
        return tomllib.load(f)


def project_path(relative: str) -> Path:
    """Turn a path from config.toml (relative to the project folder) into a full path."""
    return PROJECT_ROOT / relative


def get_api_key() -> str:
    key = os.environ.get("AT_API_KEY", "").strip()
    if not key:
        print("AT_API_KEY is not set. Copy .env.example to .env and paste your key in.", file=sys.stderr)
        # Exit code 2 = "needs a human"; run_local.sh stops instead of restarting forever.
        sys.exit(2)
    return key
