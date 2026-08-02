from pathlib import Path

from pydantic_settings import BaseSettings

BASE_DIR = Path(__file__).resolve().parent.parent
DB_DIR = BASE_DIR / "database"
DB_DIR.mkdir(exist_ok=True)


class Settings(BaseSettings):
    APP_NAME: str = "Cron News"
    APP_VERSION: str = "3.0.0"
    DEBUG: bool = False

    DB_PATH: str = str(DB_DIR / "cron_news.db")

    LLM_BASE_URL: str = ""
    LLM_API_KEY: str = ""
    LLM_MODEL: str = ""

    SEARCH_BASE_URL: str = ""
    SEARCH_API_KEY: str = ""

    SEARCH_CRON: str = "0 */6 * * *"
    LAST_SEARCH_TIME: str = ""

    @property
    def DATABASE_URL(self) -> str:
        return f"sqlite+aiosqlite:///{self.DB_PATH}"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


settings = Settings()
