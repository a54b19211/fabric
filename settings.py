"""
全局配置中心 —— 使用 pydantic-settings 从 .env 文件 / 环境变量读取配置。
所有 API Key、路径、阈值统一在此管理，其他模块只需 `from settings import APP`。
"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class _AppSettings(BaseSettings):
    """
    只需在项目根目录放一个 .env 文件（参考 .env.example），
    pydantic-settings 会自动读取，也支持直接设置系统环境变量覆盖。
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",            # 忽略 .env 中多余的变量，不报错
    )

    # ======================== 云端 API 密钥 ========================
    MINERU_API_BASE: str = "http://100.121.201.73:8000"

    # ======================== 本地数据根目录 ========================
    DATA_ROOT: Path = Path("data")

    @property
    def RAW_DIR(self) -> Path:
        """原始文件存放目录"""
        return self.DATA_ROOT / "raw"

    # ======================== 并发与重试 ========================
    MAX_CONCURRENCY: int = 3                # 并发任务数，防封 IP
    TASK_RETRIES: int = 3
    TASK_RETRY_DELAY: int = 10              # 秒

    # ======================== zk 文档库（只读：站点附件链接，fetch/site_crawler.py）========================
    DB_HOST: str = ""
    DB_PORT: int = 5432
    DB_NAME: str = ""
    DB_USER: str = ""
    DB_PASSWORD: str = ""

    @property
    def DB_URL(self) -> str:
        """SQLAlchemy 连接串，未配置时返回空字符串。"""
        if not self.DB_HOST or not self.DB_NAME:
            return ""
        return (
            f"postgresql+psycopg2://{self.DB_USER}:{self.DB_PASSWORD}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"
        )


# 全局单例——整个项目统一用这个
APP = _AppSettings()

