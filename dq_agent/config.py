"""Settings from .env. Secret values are never printed, logged or put in errors."""
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"
RULES_DIR = PROJECT_ROOT / "rules"
METHODOLOGY_PATH = PROJECT_ROOT / "docs" / "methodology.md"
REPORT_PATH = PROJECT_ROOT / "reports" / "data_quality_report.md"
SEMANTIC_LAYER_PATH = PROJECT_ROOT / "semantic_layer.yaml"

DEFAULT_MODEL = "gemini-2.5-flash"
REQUIRED_VARIABLES = ("GEMINI_API_KEY", "DATABASE_URL")
ALL_VARIABLES = (*REQUIRED_VARIABLES, "GEMINI_MODEL")


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    gemini_api_key: str = field(repr=False)
    database_url: str = field(repr=False)
    gemini_model: str = DEFAULT_MODEL

    def redact(self, text: str) -> str:
        """Remove secrets (API key, DB URL, DB password) from a message."""
        secrets = [self.gemini_api_key, self.database_url]
        password = urlsplit(self.database_url).password
        if password:
            secrets.append(password)
        for secret in secrets:
            if secret:
                text = text.replace(secret, "***")
        return text


def load_settings() -> Settings:
    """Read .env on every call without writing it into os.environ.

    load_dotenv() would pin the first value into the process environment, so a
    long-running process (Streamlit) would ignore later edits of .env.
    Variables exported in the shell still take precedence over .env.
    """
    values = {k: v for k, v in dotenv_values(ENV_PATH).items() if v}
    values.update({k: v for k, v in os.environ.items() if k in ALL_VARIABLES and v})
    missing = [name for name in REQUIRED_VARIABLES if not values.get(name)]
    if missing:
        raise ConfigError(
            f"Missing environment variables: {', '.join(missing)}. "
            "Copy .env.example to .env and fill them in."
        )
    return Settings(
        gemini_api_key=values["GEMINI_API_KEY"].strip(),
        database_url=values["DATABASE_URL"].strip(),
        gemini_model=values.get("GEMINI_MODEL", "").strip() or DEFAULT_MODEL,
    )
