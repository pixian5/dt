import argparse
import asyncio
import os
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright, Page

from crawler.login import (
    PW_TIMEOUT_MS,
    call_with_timeout_retry,
    connect_chrome_over_cdp,
    ensure_logged_in,
    load_local_secrets,
)


URL_FILE = Path("url.txt")


def _iter_urls_from_file(path: Path):
    if not path.exists() or not path.is_file():
        raise SystemExit(f"找不到 URL 文件：{path}")
    for raw in path.read_text(encoding="utf-8").splitlines():
        s = raw.strip()
        if not s:
            continue
        if not s.startswith("https://"):
            continue
        yield s


async def _wait_player_ready(page: Page) -> None:
    await call_with_timeout_retry(
        page.wait_for_selector,
        "等待播放器当前时间",
        ".vjs-current-time-display",
        state="visible",
        timeout=PW_TIMEOUT_MS,
    )
    await call_with_timeout_retry(
        page.wait_for_selector,
        "等待播放器总时长",
        ".vjs-duration-display",
        state="visible",
        timeout=PW_TIMEOUT_MS,
    )
    await call_with_timeout_retry(
        page.wait_for_selector,
        "等待播放控制按钮",
        ".vjs-play-control",
        state="attached",
        timeout=PW_TIMEOUT_MS,
    )


async def _click_big_play_button(page: Page) -> None:
    btn = page.locator(".vjs-big-play-button").first
    if await btn.count() == 0:
        return
    await call_with_timeout_retry(btn.click, "点击大播放按钮", timeout=PW_TIMEOUT_MS)


async def _click_first_speed_item(page: Page) -> None:
    items = page.locator(".vjs-menu-item-text")
    if await items.count() == 0:
        btn = page.locator(".vjs-playback-rate")
        if await btn.count():
            await call_with_timeout_retry(btn.first.click, "打开倍速菜单", timeout=PW_TIMEOUT_MS)
            await page.wait_for_timeout(300)
        items = page.locator(".vjs-menu-item-text")

    if await items.count() == 0:
        raise SystemExit("未找到 vjs-menu-item-text（倍速菜单项），无法设置 2x")

    first = items.nth(0)
    t = ((await first.inner_text()) or "").strip()
    if t != "2x":
        raise SystemExit(f"倍速菜单第一项不是 2x，实际为：{t!r}")
    target = first
    await call_with_timeout_retry(target.click, "设置倍速 2x", timeout=PW_TIMEOUT_MS)


async def _ensure_playing(page: Page) -> None:
    play = page.locator(".vjs-play-control").first
    cls = (
        await call_with_timeout_retry(play.get_attribute, "读取播放按钮class", "class", timeout=PW_TIMEOUT_MS)
    ) or ""
    if "vjs-paused" in cls and "vjs-ended" not in cls:
        await call_with_timeout_retry(play.click, "点击播放", timeout=PW_TIMEOUT_MS)


async def _pause_then_resume(page: Page) -> None:
    play = page.locator(".vjs-play-control").first
    cls = (
        await call_with_timeout_retry(play.get_attribute, "读取播放按钮class", "class", timeout=PW_TIMEOUT_MS)
    ) or ""

    if "vjs-playing" in cls:
        await call_with_timeout_retry(play.click, "暂停播放", timeout=PW_TIMEOUT_MS)
        await page.wait_for_timeout(30000)

    cls2 = (
        await call_with_timeout_retry(play.get_attribute, "读取播放按钮class", "class", timeout=PW_TIMEOUT_MS)
    ) or ""
    if "vjs-paused" in cls2 and "vjs-ended" not in cls2:
        await call_with_timeout_retry(play.click, "继续播放", timeout=PW_TIMEOUT_MS)


async def _check_ended(page: Page) -> bool:
    current_loc = page.locator(".vjs-current-time-display").first
    duration_loc = page.locator(".vjs-duration-display").first
    current = (
        (await call_with_timeout_retry(current_loc.inner_text, "读取当前播放时间", timeout=PW_TIMEOUT_MS))
        or ""
    ).strip()
    duration = (
        (await call_with_timeout_retry(duration_loc.inner_text, "读取总时长", timeout=PW_TIMEOUT_MS)) or ""
    ).strip()

    if not current or not duration:
        return False

    if current != duration:
        return False

    play = page.locator(".vjs-play-control").first
    cls = (
        await call_with_timeout_retry(play.get_attribute, "读取播放按钮class", "class", timeout=PW_TIMEOUT_MS)
    ) or ""
    title = (
        await call_with_timeout_retry(play.get_attribute, "读取播放按钮title", "title", timeout=PW_TIMEOUT_MS)
    ) or ""

    return ("vjs-ended" in cls) and (title.strip() == "Replay")


async def _watch_single_url(page: Page, url: str, *, already_opened: bool = False) -> None:
    if not already_opened:
        await call_with_timeout_retry(page.goto, "打开课程详情页", url, wait_until="domcontentloaded", timeout=PW_TIMEOUT_MS)
    await _wait_player_ready(page)
    await _click_big_play_button(page)
    await _click_first_speed_item(page)
    await _ensure_playing(page)

    last_pause = asyncio.get_running_loop().time()

    while True:
        now = asyncio.get_running_loop().time()
        if now - last_pause >= 60:
            await _pause_then_resume(page)
            last_pause = now

        if await _check_ended(page):
            await call_with_timeout_retry(page.reload, "播放结束后刷新页面", wait_until="domcontentloaded", timeout=PW_TIMEOUT_MS)
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"【{url}已看完。{ts}】")
            return

        await asyncio.sleep(4)


async def perform_watch(
    username: str,
    password: str,
    open_only: bool,
    skip_login: bool,
    url_file: Path,
) -> None:
    async with async_playwright() as p:
        endpoint = os.getenv("PLAYWRIGHT_CDP_ENDPOINT", "http://127.0.0.1:9222")
        browser = await connect_chrome_over_cdp(p, endpoint)

        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        context.set_default_timeout(PW_TIMEOUT_MS)
        login_page = await context.new_page()

        await ensure_logged_in(
            login_page, username=username, password=password, open_only=open_only, skip_login=skip_login
        )
        if open_only and not skip_login:
            input("请在浏览器中完成手动登录后，按 Enter 继续：")

        urls_iter = iter(_iter_urls_from_file(url_file))
        first_url = next(urls_iter, None)
        if not first_url:
            print("[WARN] url.txt 中未找到任何 https URL，结束")
            return

        current_page = await context.new_page()
        print(f"[INFO] 开始播放：{first_url}")
        await _watch_single_url(current_page, first_url)

        for url in urls_iter:
            next_page = await context.new_page()
            await call_with_timeout_retry(
                next_page.goto,
                "打开课程详情页",
                url,
                wait_until="domcontentloaded",
                timeout=PW_TIMEOUT_MS,
            )
            try:
                await current_page.close()
            except Exception:
                pass

            current_page = next_page
            print(f"[INFO] 开始播放：{url}")
            await _watch_single_url(current_page, url, already_opened=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="读取 url.txt 逐个打开课程详情页并自动观看视频")
    parser.add_argument("--username", default=None, help="登录用户名")
    parser.add_argument("--password", default=None, help="登录密码")
    parser.add_argument("--open-only", action="store_true", help="仅打开登录页，不自动填写/提交")
    parser.add_argument("--skip-login", action="store_true", help="已手动登录时使用，跳过登录流程")
    parser.add_argument("--url-file", default=str(URL_FILE), help="URL 文件路径（默认 url.txt）")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    load_local_secrets()
    args = parse_args(argv)

    open_only = bool(args.open_only)
    skip_login = bool(args.skip_login)

    username = args.username or os.getenv("DT_CRAWLER_USERNAME") or ""
    password = args.password or os.getenv("DT_CRAWLER_PASSWORD") or ""

    if not open_only and not skip_login:
        if not username or not password:
            raise SystemExit(
                "缺少登录信息：请通过参数 --username/--password，或环境变量 DT_CRAWLER_USERNAME/DT_CRAWLER_PASSWORD，"
                "或在项目根目录创建 secrets.local.env 提供"
            )

    asyncio.run(
        perform_watch(
            username=username,
            password=password,
            open_only=open_only,
            skip_login=skip_login,
            url_file=Path(args.url_file),
        )
    )


if __name__ == "__main__":
    main()
