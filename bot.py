import asyncio
import logging
import os
import time

from telegram import Message
from telegram.ext import ApplicationBuilder, filters as tg_filters

from admin_dashboard import build_admin_handlers, refresh_runtime_config
from amazon_scraper import BrowserManager, scrape_amazon
from config import (
    ADMIN_USER_IDS,
    AMAZON_DOMAIN,
    BOT_TOKEN,
    DATABASE_PATH,
    DEDUP_MAX_SIZE,
    DEDUP_TTL_SECONDS,
    DESTINATION_CHANNEL_ID,
    LAST_PUBLISHED_LOOKBACK,
    PUBLISH_DELAY_SEC,
    SOURCE_CHANNEL_ID,
    TELEGRAM_CONNECT_TIMEOUT,
    TELEGRAM_POOL_TIMEOUT,
    TELEGRAM_READ_TIMEOUT,
    TELEGRAM_WRITE_TIMEOUT,
)
from database import Database
from dedup import TTLCache
from duplicate_moderation import (
    approval_timeout_loop,
    build_moderation_handlers,
    send_approval_request,
)
from file_cleanup import cleanup_files
from manual_posts import build_manual_handlers
from image_processor import apply_frame
from link_resolver import (
    build_clean_url,
    close_http_client,
    extract_all_urls_from_message,
    extract_asin,
    get_message_text,
    init_http_client,
    resolve_redirect,
)
from telegram_listener import start_telethon_listener, stop_telethon_listener
from ai_caption import build_product_caption
from telegram_publisher import build_caption, publish_to_channel
from upload_prep import to_jpeg_for_telegram

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def process_single_url(
    application,
    browser: BrowserManager,
    db: Database,
    destination_id: int,
    url: str,
    msg: Message,
    index: int,
    total: int,
) -> bool:
    """Process one URL; return True if published (not pending approval)."""
    message_id = msg.message_id
    source_channel_id = msg.chat_id
    temp_files: list[str] = []
    held_for_approval: str | None = None

    try:
        logger.info("PROCESSING URL %s/%s: %s", index, total, url)

        asin = extract_asin(url)
        if asin:
            logger.info("ASIN in URL — skip HTTP redirect")
        else:
            final_url = await resolve_redirect(url)
            logger.info("REDIRECT RESOLVED: %s", final_url)
            asin = extract_asin(final_url)

        if not asin:
            logger.warning("URL FAILED — no ASIN for %s", url)
            logger.info("CONTINUING")
            return False

        logger.info("ASIN FOUND: %s", asin)

        clean_url = build_clean_url(asin, AMAZON_DOMAIN)
        scrape_asin = f"{asin}_{message_id}_{index}"
        product = await scrape_amazon(browser, clean_url, scrape_asin)
        temp_files.append(product["screenshot"])
        logger.info("SCRAPE SUCCESS: %r %r", product["title"], product["price"])

        if product["title"] == "Not found":
            caption = build_caption(product["title"], product["price"], clean_url)
        else:
            caption = await build_product_caption(
                db,
                product["title"],
                product["price"],
                clean_url,
            )
        framed_image = apply_frame(product["screenshot"])
        temp_files.append(framed_image)

        upload_image = to_jpeg_for_telegram(framed_image)
        if upload_image != framed_image:
            temp_files.append(upload_image)

        if db.is_asin_in_last_published(asin, LAST_PUBLISHED_LOOKBACK):
            logger.info("DUPLICATE ASIN DETECTED: %s", asin)
            held_for_approval = framed_image
            if held_for_approval in temp_files:
                temp_files.remove(held_for_approval)
            cleanup_files(
                [p for p in temp_files if p != held_for_approval],
            )
            temp_files.clear()

            pending_id = db.create_pending_approval(
                asin=asin,
                title=product["title"],
                price=product["price"],
                clean_url=clean_url,
                source_channel_id=source_channel_id,
                caption=caption,
                image_path=held_for_approval,
            )
            await send_approval_request(
                application.bot,
                db,
                pending_id,
                asin,
                product["title"],
                source_channel_id,
                held_for_approval,
            )
            return False

        sent = await publish_to_channel(
            application.bot,
            destination_id,
            upload_image,
            caption,
        )
        db.add_published_product(
            asin,
            product["title"],
            source_channel_id,
            sent.message_id,
        )
        logger.info("PUBLISHED URL %s/%s asin=%s", index, total, asin)
        published_paths = [product["screenshot"], framed_image]
        if upload_image != framed_image:
            published_paths.append(upload_image)
        cleanup_files(published_paths)
        temp_files.clear()
        return True

    except Exception:
        logger.exception("URL FAILED: %s", url)
        logger.info("CONTINUING")
        return False
    finally:
        if temp_files:
            cleanup_files(list(temp_files))


async def process_message(application, msg: Message) -> None:
    message_id = msg.message_id
    dedup: TTLCache = application.bot_data["dedup"]
    browser: BrowserManager = application.bot_data["browser"]
    db: Database = application.bot_data["db"]
    destination_id = application.bot_data.get("destination_channel_id")

    if not destination_id:
        logger.error("No destination channel configured — skipping message_id=%s", message_id)
        return

    msg_key = f"msg:{msg.chat_id}:{message_id}"
    if not dedup.add(msg_key):
        logger.info("Dedup hit: %s — skipping rapid reprocess", msg_key)
        return

    logger.info("PROCESSING MESSAGE message_id=%s", message_id)
    text = get_message_text(msg)
    logger.info("Message text/caption: %r", text[:300] if text else "")

    urls = extract_all_urls_from_message(msg)
    if not urls:
        logger.warning("FOUND 0 URLS — skipping message_id=%s", message_id)
        return

    total = len(urls)
    logger.info("FOUND %s URLS", total)

    published = 0
    t_total = time.perf_counter()

    for index, url in enumerate(urls, start=1):
        ok = await process_single_url(
            application,
            browser,
            db,
            destination_id,
            url,
            msg,
            index,
            total,
        )
        if ok:
            published += 1
            if index < total and PUBLISH_DELAY_SEC > 0:
                await asyncio.sleep(PUBLISH_DELAY_SEC)

    logger.info("PUBLISHED %s/%s", published, total)
    logger.info(
        "Message message_id=%s done in %.2fs",
        message_id,
        time.perf_counter() - t_total,
    )


async def worker(application) -> None:
    queue: asyncio.Queue = application.bot_data["queue"]
    logger.info("WORKER STARTED — waiting for channel posts")
    try:
        while True:
            msg: Message = await queue.get()
            try:
                logger.info(
                    "WORKER RECEIVED MESSAGE message_id=%s chat_id=%s",
                    msg.message_id,
                    msg.chat_id,
                )
                await process_message(application, msg)
            except Exception:
                logger.exception(
                    "Worker error message_id=%s", getattr(msg, "message_id", "?")
                )
            finally:
                queue.task_done()
    except asyncio.CancelledError:
        logger.info("Worker stopped")
        raise


async def on_startup(application) -> None:
    application.bot_data["ready"] = False

    db = Database(DATABASE_PATH)
    db.seed_from_env(SOURCE_CHANNEL_ID, DESTINATION_CHANNEL_ID)
    application.bot_data["db"] = db
    refresh_runtime_config(application)

    if not ADMIN_USER_IDS:
        logger.warning("ADMIN_USER_IDS is empty — /admin and approvals disabled")

    await init_http_client()

    browser = BrowserManager()
    await browser.start()
    application.bot_data["browser"] = browser

    application.bot_data["queue"] = asyncio.Queue()
    application.bot_data["worker_task"] = asyncio.create_task(
        worker(application),
        name="channel-post-worker",
    )
    application.bot_data["approval_timeout_task"] = asyncio.create_task(
        approval_timeout_loop(application),
        name="approval-timeout",
    )
    application.bot_data["dedup"] = TTLCache(
        ttl_seconds=DEDUP_TTL_SECONDS,
        max_size=DEDUP_MAX_SIZE,
    )

    await start_telethon_listener(application)

    application.bot_data["ready"] = True
    logger.info(
        "Startup complete — sources=%s destination=%s paused=%s",
        len(application.bot_data.get("active_source_ids", [])),
        application.bot_data.get("destination_channel_id"),
        application.bot_data.get("paused"),
    )


async def on_shutdown(application) -> None:
    application.bot_data["ready"] = False

    for key in ("worker_task", "approval_timeout_task"):
        task = application.bot_data.get(key)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    browser: BrowserManager | None = application.bot_data.get("browser")
    if browser:
        await browser.stop()

    await stop_telethon_listener(application)
    await close_http_client()
    logger.info("Shutdown complete")


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Set BOT_TOKEN environment variable")

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .connect_timeout(TELEGRAM_CONNECT_TIMEOUT)
        .read_timeout(TELEGRAM_READ_TIMEOUT)
        .write_timeout(TELEGRAM_WRITE_TIMEOUT)
        .pool_timeout(TELEGRAM_POOL_TIMEOUT)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )

    admin_filter = (
        tg_filters.User(user_id=ADMIN_USER_IDS) if ADMIN_USER_IDS else tg_filters.ALL
    )

    for handler in build_admin_handlers():
        app.add_handler(handler)
    for handler in build_moderation_handlers():
        app.add_handler(handler)
    for handler in build_manual_handlers(admin_filter):
        app.add_handler(handler)

    logger.info("Starting polling (bot API: admin + publish only)")
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
