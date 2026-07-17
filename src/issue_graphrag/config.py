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
    embedding_provider: str
    embedding_model: str
    raw_data_dir: Path
    processed_data_dir: Path
    vector_db_path: Path
    vector_collection: str
    github_token: str | None


def load_settings(env_file: str | None = None) -> Settings:
    if env_file:
        load_dotenv(env_file)
    else:
        load_dotenv()

    return Settings(
        llm_provider=os.getenv("LLM_PROVIDER", "mock"),
        llm_base_url=os.getenv("LLM_BASE_URL") or None,
        llm_api_key=os.getenv("LLM_API_KEY") or None,
        llm_model=os.getenv("LLM_MODEL") or None,
        embedding_provider=os.getenv("EMBEDDING_PROVIDER", "mock"),
        embedding_model=os.getenv("EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2"),
        raw_data_dir=Path(os.getenv("RAW_DATA_DIR", "data/raw")),
        processed_data_dir=Path(os.getenv("PROCESSED_DATA_DIR", "data/processed")),
        vector_db_path=Path(os.getenv("VECTOR_DB_PATH", "data/processed/qdrant")),
        vector_collection=os.getenv("VECTOR_COLLECTION", "issue_graphrag"),
        github_token=os.getenv("GITHUB_TOKEN") or None,
    )
