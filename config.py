"""Config persistence — JSON file in /usr/local/bb-epaper/."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "config.json"


@dataclass
class Config:
    refresh_seconds: int = 300
    pages: list[str] = field(default_factory=lambda: ["watch", "cc"])
    host: str = "0.0.0.0"
    port: int = 8081
    data_ttl_seconds: int = 60  # cache fetched market data for this long

    @classmethod
    def load(cls) -> "Config":
        if CONFIG_PATH.exists():
            try:
                d = json.loads(CONFIG_PATH.read_text())
                return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
            except Exception:
                pass
        c = cls()
        c.save()
        return c

    def save(self) -> None:
        CONFIG_PATH.write_text(json.dumps(asdict(self), indent=2))


VALID_PAGES = {"watch", "cc"}
