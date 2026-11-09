import os
from pathlib import Path
from typing import Optional

class Settings:
    PROJECT_NAME: str = "HD2Lab Document Intelligence Service"
    PROJECT_VERSION: str = "2.0.0"
    API_V1_STR: str = "/api/v1"
    
    # Environment mode: 'development', 'test', 'production'
    ENV: str = os.getenv("APP_ENV", "development")
    
    # Base directory
    BASE_DIR: Path = Path(__file__).resolve().parent.parent
    
    # Storage paths (Work Order 7: configurable, not hardcoded)
    DATA_DIR: Path = Path(os.getenv("HD2_DATA_DIR", str(BASE_DIR / "data")))
    DOCUMENTS_DIR: Path = DATA_DIR / "documents"
    DATABASE_PATH: Path = DATA_DIR / "metadata.sqlite3"
    
    # Authentication & Access Control (Work Order A-2)
    SERVICE_TOKEN: str = os.getenv("DOCUMENT_SERVICE_TOKEN", "hd2-doc-secret-token-key-2026")
    AUTH_REQUIRED: bool = os.getenv("AUTH_REQUIRED", "true").lower() in ("true", "1", "yes")
    BYPASS_AUTH_IN_DEV: bool = os.getenv("BYPASS_AUTH_IN_DEV", "false").lower() in ("true", "1", "yes")
    
    # Security limits (Work Order 11 & A-4)
    MAX_FILE_SIZE_BYTES: int = 100 * 1024 * 1024  # 100 MB
    MAX_PAGES_LIMIT: int = 500
    MAX_ZIP_ENTRIES: int = 10000
    MAX_ZIP_UNCOMPRESSED_BYTES: int = 500 * 1024 * 1024  # 500 MB
    MAX_ZIP_RATIO: int = 100
    PARSING_TIMEOUT_SECONDS: int = int(os.getenv("PARSING_TIMEOUT_SECONDS", "300"))  # 5 minutes
    MAX_CONCURRENT_JOBS: int = int(os.getenv("MAX_CONCURRENT_JOBS", "4"))
    
    # Chunking defaults (Work Order 8 & B-5)
    CHUNK_TARGET_TOKENS: int = 1000
    CHUNK_MAX_TOKENS: int = 1500
    CHUNK_OVERLAP_TOKENS: int = 100
    
    # LibreOffice Path
    LIBREOFFICE_PATH: Optional[str] = os.getenv("LIBREOFFICE_PATH", None)

    def init_directories(self) -> None:
        self.DOCUMENTS_DIR.mkdir(parents=True, exist_ok=True)
        self.DATA_DIR.mkdir(parents=True, exist_ok=True)

settings = Settings()
