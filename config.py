from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from forward import ForwardMode


@dataclass(frozen=True)
class Config:
    source: str
    destination: str
    forward_mode: ForwardMode
    words: list[str]
    file_formats: list[str]
    poll_interval: float
    session_name: str
    clone_state_file: str
    temp_download_dir: str
    seven_zip_path: str | None


def _csv(name: str) -> list[str]:
    return [item.strip() for item in os.getenv(name, "").split(",") if item.strip()]


def _project_path(name: str, default: str) -> str:
    value = os.getenv(name, default).strip() or default
    path = Path(os.path.expandvars(value)).expanduser()
    if not path.is_absolute():
        path = Path(__file__).resolve().parent / path
    return str(path.resolve())


def _optional_project_path(name: str) -> str | None:
    value = os.getenv(name, "").strip()
    if not value:
        return None
    path = Path(os.path.expandvars(value)).expanduser()
    if not path.is_absolute():
        path = Path(__file__).resolve().parent / path
    return str(path.resolve())


def _forward_mode() -> ForwardMode:
    value = os.getenv("FORWARD_MODE", "ALL").strip().upper()
    try:
        return ForwardMode[value]
    except KeyError as exc:
        available = ", ".join(mode.name for mode in ForwardMode)
        raise ValueError(
            f"Unknown FORWARD_MODE={value!r}. Available modes: {available}"
        ) from exc


def load_config() -> Config:
    """Load and validate application settings from a local .env file."""
    load_dotenv()

    source = os.getenv("SOURCE", "").strip()
    destination = os.getenv("DESTINATION", "").strip()
    missing = [
        name
        for name, value in (("SOURCE", source), ("DESTINATION", destination))
        if not value
    ]
    if missing:
        raise ValueError(
            f"Missing required setting(s): {', '.join(missing)}. "
            "Copy .env.example to .env and fill in the values."
        )

    try:
        poll_interval = float(os.getenv("POLL_INTERVAL", "5"))
    except ValueError as exc:
        raise ValueError("POLL_INTERVAL must be a number") from exc
    if poll_interval <= 0:
        raise ValueError("POLL_INTERVAL must be greater than zero")

    return Config(
        source=source,
        destination=destination,
        forward_mode=_forward_mode(),
        words=_csv("WORDS"),
        file_formats=_csv("FILE_FORMATS"),
        poll_interval=poll_interval,
        session_name=os.getenv("SESSION_NAME", "account").strip() or "account",
        clone_state_file=(
            os.getenv("CLONE_STATE_FILE", ".clone-state.sqlite3").strip()
            or ".clone-state.sqlite3"
        ),
        temp_download_dir=_project_path("TEMP_DOWNLOAD_DIR", ".clone-temp"),
        seven_zip_path=_optional_project_path("SEVEN_ZIP_PATH"),
    )
