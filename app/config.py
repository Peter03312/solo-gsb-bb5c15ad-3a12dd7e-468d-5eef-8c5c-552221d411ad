"""Runtime configuration via environment variables."""
from __future__ import annotations

import os


class Settings:
    def __init__(self) -> None:
        self.DATABASE_URL: str = os.getenv(
            "DATABASE_URL",
            "postgresql+psycopg2://planner:planner@localhost:5432/planner",
        )
        self.DB_CONNECT_RETRIES: int = int(os.getenv("DB_CONNECT_RETRIES", "30"))
        self.DB_CONNECT_DELAY_SECONDS: float = float(
            os.getenv("DB_CONNECT_DELAY_SECONDS", "1")
        )


settings = Settings()
