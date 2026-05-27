"""

Visual scrape logic — frozen to original base code.

Infrastructure only: shared Chromium process + fresh context per scrape.

"""



import logging



from playwright.async_api import Browser, Playwright, async_playwright



from config import CHROMIUM_ARGS, PRICE_SELECTORS, TITLE_SELECTORS, USER_AGENT



logger = logging.getLogger(__name__)



_ACCEPT_LANGUAGE = "ar-EG,ar;q=0.9,en;q=0.8"





class BrowserManager:

    """Reuses Chromium process only — fresh context per scrape."""



    def __init__(self):

        self._playwright: Playwright | None = None

        self._browser: Browser | None = None



    async def start(self) -> None:

        if self._browser:

            return

        logger.info("Starting shared Chromium process")

        self._playwright = await async_playwright().start()

        self._browser = await self._playwright.chromium.launch(

            headless=True,

            args=CHROMIUM_ARGS,

        )

        logger.info("Chromium process ready")



    async def stop(self) -> None:

        if self._browser:

            await self._browser.close()

            self._browser = None

        if self._playwright:

            await self._playwright.stop()

            self._playwright = None

        logger.info("Chromium process stopped")





async def _wait_for_product_title(page) -> bool:

    """Wait until any known title selector appears."""

    for selector in TITLE_SELECTORS:

        try:

            await page.wait_for_selector(selector, state="attached", timeout=10000)

            logger.info("TITLE SELECTOR FOUND: %s", selector)

            return True

        except Exception:

            continue

    return False





async def _extract_title_and_price(page) -> tuple[str, str]:

    title = "Not found"

    price = "Not found"



    for selector in TITLE_SELECTORS:

        locator = page.locator(selector)

        if await locator.count() > 0:

            txt = await locator.first.text_content()

            if txt and txt.strip():

                title = txt.strip()

                logger.info("TITLE SELECTOR FOUND: %s", selector)

                break



    for selector in PRICE_SELECTORS:

        locator = page.locator(selector)

        if await locator.count() > 0:

            txt = await locator.first.text_content()

            if txt and txt.strip():

                price = txt.strip()

                logger.info("PRICE SELECTOR FOUND: %s", selector)

                break



    return title, price





async def scrape_amazon(

    browser_mgr: BrowserManager,

    clean_url: str,

    asin: str,

) -> dict:

    browser = browser_mgr._browser

    if not browser:

        raise RuntimeError("Browser not started")



    context = await browser.new_context(

        viewport={"width": 1920, "height": 1600},

        user_agent=USER_AGENT,

        locale="ar-EG",

        timezone_id="Africa/Cairo",

        extra_http_headers={"Accept-Language": _ACCEPT_LANGUAGE},

    )

    page = await context.new_page()

    screenshot_path = f"{asin}.png"



    try:

        for attempt in range(2):

            try:

                await page.goto(

                    clean_url,

                    timeout=30000,

                    wait_until="domcontentloaded",

                )

                break

            except Exception as exc:

                if attempt == 0:

                    logger.warning("page.goto failed, retrying once: %s", exc)

                else:

                    raise



        await page.wait_for_timeout(3000)



        await page.evaluate("""

            document.body.style.zoom = '130%'

        """)



        await page.wait_for_timeout(2000)

        await _wait_for_product_title(page)

        title, price = await _extract_title_and_price(page)

        if title == "Not found":
            logger.info("SCRAPE RETRY")
            await page.wait_for_timeout(3000)
            await _wait_for_product_title(page)
            retry_title, retry_price = await _extract_title_and_price(page)
            if retry_title != "Not found":
                title = retry_title
            if price == "Not found" and retry_price != "Not found":
                price = retry_price

        if title != "Not found":
            await page.screenshot(
                path=screenshot_path,
                full_page=False,
            )
            logger.info("SCRAPE SUCCESS")

        logger.info("Scraped: %s %s", title, price)



        return {

            "title": title,

            "price": price,

            "screenshot": screenshot_path,

        }

    finally:

        await context.close()


