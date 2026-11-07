from pathlib import Path

class Settings:
    PROJECT_NAME: str = "HD2Lab Document Intelligence Service"
    PROJECT_VERSION: str = "1.0.0-mvp"
    API_V1_STR: str = "/api/v1"
    
    # Base directory
    BASE_DIR: Path = Path(__file__).resolve().parent.parent
    
    # Storage paths (Work Order 7: configurable, not hardcoded)
    DATA_DIR: Path = BASE_DIR / "data"
    DOCUMENTS_DIR: Path = DATA_DIR / "documents"
    DATABASE_PATH: Path = DATA_DIR / "metadata.sqlite3"
    
    # Security limits (Work Order 11)
    MAX_FILE_SIZE_BYTES: int = 100 * 1024 * 1024  # 100 MB
    MAX_PAGES_LIMIT: int = 500
    PARSING_TIMEOUT_SECONDS: int = 300  # 5 minutes
    
    # Chunking defaults (Work Order 8)
    CHUNK_TARGET_TOKENS: int = 1000
    CHUNK_MAX_TOKENS: int = 1500
    CHUNK_OVERLAP_TOKENS: int = 100

    def init_directories(self) -> None:
        self.DOCUMENTS_DIR.mkdir(parents=True, exist_ok=True)
        self.DATA_DIR.mkdir(parents=True, exist_ok=True)

settings = Settings()
