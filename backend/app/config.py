"""Environment-driven configuration.

Every value has a safe default for local development. Production overrides come
from environment variables only -- no secrets are read from files.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# pydantic-settings JSON-decodes list/dict env vars before field validators run, so a
# bare value like CORS_ALLOW_ORIGINS=* would fail json.loads(). NoDecode disables that
# decoding for these fields; the _split_csv validator below parses the raw string instead
# (accepting both `a,b,c` and a JSON array).
CsvList = Annotated[list[str], NoDecode]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # --- Core -----------------------------------------------------------------
    environment: str = Field(default="development")
    service_name: str = Field(default="nanda-recovery-beacon")
    public_base_url: str = Field(default="http://localhost:8000")
    log_level: str = Field(default="INFO")

    # --- Database -------------------------------------------------------------
    database_url: str = Field(
        default="postgresql+psycopg://beacon:beacon@localhost:5432/beacon"
    )
    db_pool_size: int = Field(default=5)
    db_max_overflow: int = Field(default=5)
    db_pool_pre_ping: bool = Field(default=True)
    db_statement_timeout_ms: int = Field(default=15_000)

    # --- Auth -----------------------------------------------------------------
    # When demo_mode is true, unauthenticated requests are accepted and attributed
    # to the agent named in X-Agent-Id (default "demo-agent"). Never enable in a
    # deployment holding real data.
    demo_mode: bool = Field(default=False)
    demo_agent_id: str = Field(default="demo-agent")

    # --- Leases / failure detection -------------------------------------------
    default_heartbeat_timeout_seconds: int = Field(default=120)
    min_heartbeat_timeout_seconds: int = Field(default=5)
    max_heartbeat_timeout_seconds: int = Field(default=86_400)
    # Extra time after the heartbeat deadline before a suspected_failed workflow
    # is promoted to recoverable. Gives a slow agent a chance to come back.
    suspect_grace_seconds: int = Field(default=30)
    default_lease_seconds: int = Field(default=300)
    min_lease_seconds: int = Field(default=10)
    max_lease_seconds: int = Field(default=3_600)
    default_max_recoveries: int = Field(default=3)

    # --- Reaper (background failure detection) --------------------------------
    run_reaper_in_api: bool = Field(default=True)
    reaper_interval_seconds: int = Field(default=15)
    # Opportunistic sweep triggered by reads; never runs more often than this.
    opportunistic_sweep_min_interval_seconds: int = Field(default=5)
    reaper_batch_size: int = Field(default=200)

    # --- Context evaluation ---------------------------------------------------
    resumable_min_score: int = Field(default=50)
    supported_checkpoint_schema_versions: CsvList = Field(default=["1.0"])

    # --- Artifacts ------------------------------------------------------------
    storage_backend: str = Field(default="none")  # none | s3
    artifact_verify_max_bytes: int = Field(default=25 * 1024 * 1024)
    artifact_verify_timeout_seconds: float = Field(default=10.0)
    artifact_allow_private_networks: bool = Field(default=False)
    s3_bucket: str | None = Field(default=None)
    s3_region: str | None = Field(default=None)
    s3_endpoint_url: str | None = Field(default=None)
    s3_presign_expiry_seconds: int = Field(default=900)

    # --- HTTP hardening -------------------------------------------------------
    cors_allow_origins: CsvList = Field(default=["*"])
    max_request_bytes: int = Field(default=1024 * 1024)  # 1 MiB
    rate_limit_enabled: bool = Field(default=True)
    rate_limit_requests: int = Field(default=120)
    rate_limit_window_seconds: int = Field(default=60)
    trusted_hosts: CsvList = Field(default=["*"])

    # --- Pagination -----------------------------------------------------------
    default_page_size: int = Field(default=25)
    max_page_size: int = Field(default=100)

    @field_validator(
        "cors_allow_origins",
        "trusted_hosts",
        "supported_checkpoint_schema_versions",
        mode="before",
    )
    @classmethod
    def _split_csv(cls, value: object) -> object:
        """Parse list-valued env vars. Accepts `A,B,C` or a JSON array `["A","B"]`.

        These fields use NoDecode, so this validator owns all string parsing --
        pydantic-settings no longer JSON-decodes them first.
        """
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("["):
                return json.loads(stripped)
            return [item.strip() for item in stripped.split(",") if item.strip()]
        return value

    @field_validator("database_url")
    @classmethod
    def _normalise_database_url(cls, value: str) -> str:
        """Accept the `postgres://` URLs that hosting providers hand out."""
        if value.startswith("postgres://"):
            return "postgresql+psycopg://" + value[len("postgres://") :]
        if value.startswith("postgresql://"):
            return "postgresql+psycopg://" + value[len("postgresql://") :]
        return value

    @property
    def is_production(self) -> bool:
        return self.environment.lower() in {"production", "prod"}


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
