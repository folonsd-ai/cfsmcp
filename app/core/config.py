from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    host: str = "0.0.0.0"
    port: int = 8558
    metadata_dir: Path = Path("./data/metadata")
    db_path: Path = Path("./data/cfsmcp.sqlite3")
    zvec_dir: Path = Path("./data/zvec")
    lm_studio_url: str = "http://127.0.0.1:1234"
    default_embedding_model: str = "text-embedding-multilingual-e5-small"
    embedding_batch_size: int = 128
    # Cap texts per LM Studio /v1/embeddings call (objects may expand to many passages in chunks mode)
    embedding_max_texts_per_request: int = 256
    embedding_workers: int = 2
    search_default_limit: int = 50
    search_max_limit: int = 200


settings = Settings()
