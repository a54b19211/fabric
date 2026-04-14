"""
全局配置中心 —— 使用 pydantic-settings 从 .env 文件 / 环境变量读取配置。
所有 API Key、路径、阈值统一在此管理，其他模块只需 `from config.settings import APP`。
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
    MINERU_API_KEY: str = ""
    MINERU_API_BASE: str = "https://mineru.net/api/v4"

    QWEN_API_KEY: str = ""
    QWEN_API_BASE: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    QWEN_MODEL_NAME: str = "qwen-plus"

    # ======================== 本地数据根目录 ========================
    DATA_ROOT: Path = Path("data")

    @property
    def RAW_DIR(self) -> Path:
        """原始文件存放目录"""
        return self.DATA_ROOT / "raw"

    @property
    def MARKDOWN_DIR(self) -> Path:
        """Markdown 全文存放目录"""
        return self.DATA_ROOT / "markdown"

    @property
    def JSON_DIR(self) -> Path:
        """JSON 元数据信封存放目录"""
        return self.DATA_ROOT / "json_envelopes"

    @property
    def QUARANTINE_DIR(self) -> Path:
        """隔离区——质量不达标的数据暂存"""
        return self.DATA_ROOT / "quarantine"

    @property
    def REVIEW_DIR(self) -> Path:
        """人工审核 CSV 导出目录"""
        return self.DATA_ROOT / "review"

    # ======================== 质量门控阈值 ========================
    MIN_MARKDOWN_LENGTH: int = 100          # Markdown 字符数低于此值判定为解析失败
    MIN_JSON_FILLED_RATIO: float = 0.4      # JSON 关键字段填充率低于此值进隔离区

    # ======================== 并发与重试 ========================
    MAX_CONCURRENCY: int = 3                # Prefect 并发任务数，防封 IP
    TASK_RETRIES: int = 3
    TASK_RETRY_DELAY: int = 10              # 秒

    # ======================== 远程数据库 ========================
    # 支持 PostgreSQL / MySQL，留空则不推送
    DB_HOST: str = ""
    DB_PORT: int = 5432
    DB_NAME: str = ""
    DB_USER: str = ""
    DB_PASSWORD: str = ""
    # 连接池大小
    DB_POOL_SIZE: int = 5

    @property
    def DB_URL(self) -> str:
        """SQLAlchemy 连接串，未配置时返回空字符串。"""
        if not self.DB_HOST or not self.DB_NAME:
            return ""
        return (
            f"postgresql+psycopg2://{self.DB_USER}:{self.DB_PASSWORD}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"
        )

    # ======================== MinIO 船票预留（二期使用）========================
    FUTURE_MINIO_BUCKET: str = "zk-data-fabric"


# 全局单例——整个项目统一用这个
APP = _AppSettings()

