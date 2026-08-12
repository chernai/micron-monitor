"""Load config.yaml once and expose it as a plain dict."""
from pathlib import Path
import yaml

CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"

_cache = None


def load_config():
    global _cache
    if _cache is None:
        with open(CONFIG_PATH) as f:
            _cache = yaml.safe_load(f)
    return _cache
