"""Bot configuration from environment variables."""
import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    bot_token: str
    max_file_size_mb: int
    cookies_file: str | None
    telegram_api_base: str | None
    auto_update_hours: int
    download_dir: str
    owner_id: int | None
    # Owner-only watch queue (watch.py): the channel and the private buffer
    # channel used to read its older posts.
    watch_channel_id: int | None = None
    watch_buffer_id: int | None = None

    @property
    def max_file_size_bytes(self) -> int:
        return self.max_file_size_mb * 1024 * 1024

    @property
    def is_local_server(self) -> bool:
        """Using our own Bot API server (files are passed by path, not uploaded)."""
        return bool(self.telegram_api_base)


def _chat_id(name: str) -> int | None:
    value = os.getenv(name, "").strip()
    try:
        return int(value) if value else None
    except ValueError:
        raise RuntimeError(f"{name} must be a chat id like -1001234567890") from None


def load_config() -> Config:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "BOT_TOKEN is not set. Copy .env.example to .env and put the @BotFather token in it."
        )

    # Not checked for existence: the owner may upload the file later.
    cookies = os.getenv("COOKIES_FILE", "").strip() or None

    api_base = os.getenv("TELEGRAM_API_BASE", "").strip() or None

    owner = os.getenv("OWNER_ID", "").strip()
    owner_id = int(owner) if owner.isdigit() else None

    return Config(
        bot_token=token,
        max_file_size_mb=int(os.getenv("MAX_FILE_SIZE_MB", "49")),
        cookies_file=cookies,
        telegram_api_base=api_base,
        auto_update_hours=int(os.getenv("YTDLP_AUTO_UPDATE_HOURS", "0")),
        download_dir=os.getenv("DOWNLOAD_DIR", "./downloads").strip() or "./downloads",
        owner_id=owner_id,
        watch_channel_id=_chat_id("WATCH_CHANNEL_ID"),
        watch_buffer_id=_chat_id("WATCH_BUFFER_ID"),
    )
