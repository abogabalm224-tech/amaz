import asyncio
import logging

import httpx
from telegram import Bot, Message
from telegram.error import NetworkError, TimedOut

from config import (
    PUBLISH_MAX_RETRIES,
    TELEGRAM_READ_TIMEOUT,
    TELEGRAM_WRITE_TIMEOUT,
)

logger = logging.getLogger(__name__)

RETRY_BACKOFF_SECONDS = (2, 4)

RETRYABLE_ERRORS = (
    TimedOut,
    NetworkError,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
)


def build_caption(title: str, price: str, clean_url: str) -> str:
    return (
        f"📦 {title}\n\n"
        f"💰 {price}\n\n"
        f"🔗 {clean_url}"
    )


async def publish_to_channel(
    bot: Bot,
    channel_id: int,
    photo_path: str,
    caption: str,
) -> Message:
    last_error: Exception | None = None

    for attempt in range(1, PUBLISH_MAX_RETRIES + 1):
        logger.info("PUBLISH ATTEMPT %s", attempt)
        try:
            with open(photo_path, "rb") as photo:
                msg = await bot.send_photo(
                    chat_id=channel_id,
                    photo=photo,
                    caption=caption,
                    read_timeout=TELEGRAM_READ_TIMEOUT,
                    write_timeout=TELEGRAM_WRITE_TIMEOUT,
                )
            logger.info("PUBLISH SUCCESS")
            return msg
        except RETRYABLE_ERRORS as exc:
            last_error = exc
            logger.warning("UPLOAD TIMEOUT: %s", exc)
            if attempt < PUBLISH_MAX_RETRIES:
                wait = RETRY_BACKOFF_SECONDS[attempt - 1]
                logger.info("RETRYING in %s seconds", wait)
                await asyncio.sleep(wait)
            else:
                logger.error(
                    "Publish failed after %s attempts", PUBLISH_MAX_RETRIES
                )
        except Exception as exc:
            logger.exception("Publish failed (non-retryable): %s", exc)
            raise

    if last_error:
        raise last_error
    raise RuntimeError("Publish failed")
