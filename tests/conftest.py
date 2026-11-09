import pytest
from app.config import settings
from app.core.database import init_db

@pytest.fixture(autouse=True)
def setup_test_environment(tmp_path):
    """Automatically setups an isolated temporary database and storage for every test."""
    settings.DATA_DIR = tmp_path / "data"
    settings.DOCUMENTS_DIR = tmp_path / "data" / "documents"
    settings.DATABASE_PATH = tmp_path / "data" / "test_metadata.sqlite3"
    settings.init_directories()
    init_db()
    yield
