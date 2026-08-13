"""Load config.yaml and expose it as a plain dict.

Deliberately NOT cached at module level: a process-lifetime cache went
stale in production when Streamlit Cloud's redeploy reused the same
running Python process for a new commit — app.py picked up the new code,
but load_config() kept serving the config dict parsed before that deploy,
missing newly-added top-level keys. Re-parsing a small YAML file on every
call is cheap enough that the caching wasn't buying anything worth this
risk.
"""
from pathlib import Path
import yaml

CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"


def load_config():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)
