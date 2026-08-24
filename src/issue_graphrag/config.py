from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    llm_provider: str
    llm_base_url: str | None
    llm_api_key: str | None
    llm_model: str | None
    llm_daily_calls: int
    llm_daily_input_tokens: int
    llm_daily_output_tokens: int
    llm_monthly_cost_usd: float
    llm_bootstrap_calls: int
    llm_bootstrap_input_tokens: int
    llm_bootstrap_output_tokens: int
    llm_batch_calls: int
    llm_batch_input_tokens: int
    llm_batch_output_tokens: int
    llm_max_output_tokens_per_call: int
    llm_input_price_per_million_usd: float
    llm_output_price_per_million_usd: float
    llm_cost_safety_multiplier: float
    embedding_provider: str
    embedding_model: str
    raw_data_dir: Path
    processed_data_dir: Path
    repo_data_dir: Path
    vector_db_path: Path
    vector_collection: str
    github_token: str | None
    github_repos: tuple[str, ...]
    github_sync_interval_seconds: int
    github_webhook_repo: str | None
    github_webhook_secret: str | None


def load_settings(env_file: str | None = None) -> Settings:
    if env_file:
        load_dotenv(env_file)
    else:
        load_dotenv()

    webhook_repo = os.getenv("GITHUB_WEBHOOK_REPO") or None
    configured_repos = tuple(
        dict.fromkeys(
            repo.strip()
            for repo in os.getenv("GITHUB_REPOS", "").split(",")
            if repo.strip()
        )
    )
    if webhook_repo and webhook_repo not in configured_repos:
        configured_repos = (*configured_repos, webhook_repo)

    def integer(name: str, default: int, *, positive: bool = False) -> int:
        value = int(os.getenv(name, str(default)))
        if value < (1 if positive else 0):
            qualifier = "positive" if positive else "non-negative"
            raise ValueError(f"{name} must be {qualifier}")
        return value

    def decimal(name: str, default: float, *, positive: bool = False) -> float:
        value = float(os.getenv(name, str(default)))
        if value < (0.0000001 if positive else 0):
            qualifier = "positive" if positive else "non-negative"
            raise ValueError(f"{name} must be {qualifier}")
        return value

    llm_provider = os.getenv("LLM_PROVIDER", "mock")
    default_base_url = "https://openrouter.ai/api/v1" if llm_provider == "openrouter" else None
    default_model = "google/gemini-3.1-flash-lite" if llm_provider == "openrouter" else None

    return Settings(
        llm_provider=llm_provider,
        llm_base_url=os.getenv("LLM_BASE_URL") or default_base_url,
        llm_api_key=os.getenv("LLM_API_KEY") or None,
        llm_model=os.getenv("LLM_MODEL") or default_model,
        llm_daily_calls=integer("LLM_DAILY_CALLS", 250),
        llm_daily_input_tokens=integer("LLM_DAILY_INPUT_TOKENS", 300_000),
        llm_daily_output_tokens=integer("LLM_DAILY_OUTPUT_TOKENS", 125_000),
        llm_monthly_cost_usd=decimal("LLM_MONTHLY_COST_USD", 3.0),
        llm_bootstrap_calls=integer("LLM_BOOTSTRAP_CALLS", 200),
        llm_bootstrap_input_tokens=integer("LLM_BOOTSTRAP_INPUT_TOKENS", 250_000),
        llm_bootstrap_output_tokens=integer("LLM_BOOTSTRAP_OUTPUT_TOKENS", 100_000),
        llm_batch_calls=integer("LLM_BATCH_CALLS", 12, positive=True),
        llm_batch_input_tokens=integer("LLM_BATCH_INPUT_TOKENS", 20_000, positive=True),
        llm_batch_output_tokens=integer("LLM_BATCH_OUTPUT_TOKENS", 10_000, positive=True),
        llm_max_output_tokens_per_call=integer(
            "LLM_MAX_OUTPUT_TOKENS_PER_CALL", 800, positive=True
        ),
        llm_input_price_per_million_usd=decimal(
            "LLM_INPUT_PRICE_PER_MILLION_USD", 0.25, positive=True
        ),
        llm_output_price_per_million_usd=decimal(
            "LLM_OUTPUT_PRICE_PER_MILLION_USD", 1.50, positive=True
        ),
        llm_cost_safety_multiplier=decimal(
            "LLM_COST_SAFETY_MULTIPLIER", 2.0, positive=True
        ),
        embedding_provider=os.getenv("EMBEDDING_PROVIDER", "mock"),
        embedding_model=os.getenv("EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2"),
        raw_data_dir=Path(os.getenv("RAW_DATA_DIR", "data/raw")),
        processed_data_dir=Path(os.getenv("PROCESSED_DATA_DIR", "data/processed")),
        repo_data_dir=Path(os.getenv("REPO_DATA_DIR", "data/repos")),
        vector_db_path=Path(os.getenv("VECTOR_DB_PATH", "data/processed/qdrant")),
        vector_collection=os.getenv("VECTOR_COLLECTION", "issue_graphrag"),
        github_token=os.getenv("GITHUB_TOKEN") or None,
        github_repos=configured_repos,
        github_sync_interval_seconds=integer(
            "GITHUB_SYNC_INTERVAL_SECONDS", 900, positive=True
        ),
        github_webhook_repo=webhook_repo,
        github_webhook_secret=os.getenv("GITHUB_WEBHOOK_SECRET") or None,
    )
