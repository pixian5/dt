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


PERSONAL_CENTER_URL = "https://gbwlxy.dtdjzx.gov.cn/content#/personalCenter"
URL_FILE = Path("url.txt")


def _parse_lines_range(lines_arg: str | None) -> tuple[int | None, int | None]:
    if not lines_arg:
        return None, None
    s = str(lines_arg).strip()
    if not s:
        return None, None
    if "-" not in s:
        try:
            n = int(s)
        except Exception:
            raise SystemExit(f"--lines 参数格式错误：{lines_arg!r}（示例：1 / 1- / 1-5）")
        if n <= 0:
            raise SystemExit(f"--lines 行号必须为正整数：{lines_arg!r}")
        return n, n

    start_s, end_s = [p.strip() for p in s.split("-", 1)]
    if not start_s:
        raise SystemExit(f"--lines 参数格式错误：{lines_arg!r}（示例：1- 或 1-5）")
    try:
        start = int(start_s)
    except Exception:
        raise SystemExit(f"--lines 参数格式错误：{lines_arg!r}（示例：1- 或 1-5）")
    if start <= 0:
        raise SystemExit(f"--lines 起始行号必须为正整数：{lines_arg!r}")

    if end_s == "":
        return start, None

    try:
        end = int(end_s)
    except Exception:
        raise SystemExit(f"--lines 参数格式错误：{lines_arg!r}（示例：1- 或 1-5）")
    if end <= 0:
        raise SystemExit(f"--lines 结束行号必须为正整数：{lines_arg!r}")
    if end < start:
        raise SystemExit(f"--lines 结束行号不能小于起始行号：{lines_arg!r}")

    return start, end


def _iter_urls_from_file(path: Path, *, lines_range: str | None = None):
    if not path.exists() or not path.is_file():
        raise SystemExit(f"找不到 URL 文件：{path}")

    all_lines = path.read_text(encoding="utf-8").splitlines()
    start, end = _parse_lines_range(lines_range)
    if start is not None:
        start_idx = start - 1
        end_idx = None if end is None else end
        all_lines = all_lines[start_idx:end_idx]

    for raw in all_lines:
        s = raw.strip()
        if not s:
            continue
        if not s.startswith("https://"):
            continue
        yield s


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


async def _open_personal_center(context) -> Page:
    page = await context.new_page()
    await call_with_timeout_retry(page.goto, "打开个人中心", PERSONAL_CENTER_URL, wait_until="domcontentloaded")
    return page


async def _check_progress(personal_page: Page) -> bool:
    await call_with_timeout_retry(
        personal_page.goto,
        "刷新个人中心",
        PERSONAL_CENTER_URL,
        wait_until="domcontentloaded",
        timeout=PW_TIMEOUT_MS,
    )
    loc = personal_page.locator(".plan-all.pro").first
    try:
        await call_with_timeout_retry(loc.wait_for, "等待进度元素", state="visible", timeout=PW_TIMEOUT_MS)
    except Exception:
        return False
    text = ((await call_with_timeout_retry(loc.inner_text, "读取进度文本", timeout=PW_TIMEOUT_MS)) or "").strip()
    if "100%" in text:
        print(f"【{_ts()}-已看完100%】")
        return True
    return False


async def _wait_player_ready(page: Page) -> None:
    await call_with_timeout_retry(
        page.wait_for_selector,
        "等待播放器当前时间",
        ".vjs-current-time-display",
        state="attached",
        timeout=PW_TIMEOUT_MS,
    )
    await call_with_timeout_retry(
        page.wait_for_selector,
        "等待播放器总时长",
        ".vjs-duration-display",
        state="attached",
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
    await _activate_player_controls(page)
    try:
        container = page.locator(".video-js").first
        if await container.count():
            try:
                await call_with_timeout_retry(container.hover, "悬停播放器控件", timeout=PW_TIMEOUT_MS)
            except Exception:
                pass
    except Exception:
        pass

    btn = page.locator(".vjs-big-play-button").first
    if await btn.count() != 0:
        try:
            await call_with_timeout_retry(btn.click, "点击大播放按钮", timeout=PW_TIMEOUT_MS)
            return
        except Exception:
            try:
                await call_with_timeout_retry(
                    btn.click, "点击大播放按钮（force）", timeout=PW_TIMEOUT_MS, force=True
                )
                return
            except Exception:
                pass

        if await _dom_click_first(page, ".vjs-big-play-button"):
            return

    play = page.locator(".vjs-play-control").first
    if await play.count() != 0:
        try:
            await call_with_timeout_retry(play.click, "启动播放（play-control）", timeout=PW_TIMEOUT_MS, force=True)
            return
        except Exception:
            pass

    if await _dom_click_first(page, ".vjs-play-control"):
        return

    tech = page.locator(".vjs-tech").first
    if await tech.count() != 0:
        try:
            await call_with_timeout_retry(tech.click, "启动播放（vjs-tech）", timeout=PW_TIMEOUT_MS, force=True)
            return
        except Exception:
            pass

    if await _dom_click_first(page, ".vjs-tech"):
        return

    raise SystemExit("无法启动播放：大播放按钮/播放按钮均不可用")


async def _activate_player_controls(page: Page) -> None:
    container = page.locator(".video-js").first
    if await container.count() == 0:
        return
    try:
        await call_with_timeout_retry(container.click, "激活播放器控件", timeout=PW_TIMEOUT_MS, force=True)
    except Exception:
        return


async def _dom_click_first(page: Page, selector: str) -> bool:
    try:
        return bool(
            await page.evaluate(
                """(sel) => {
                    const el = document.querySelector(sel);
                    if (!el) return false;
                    el.click();
                    return true;
                }""",
                selector,
            )
        )
    except Exception:
        return False


async def _set_speed_2x(page: Page) -> None:
    await _activate_player_controls(page)

    btn = page.locator(".vjs-playback-rate").first
    if await btn.count():
        try:
            await call_with_timeout_retry(btn.click, "打开倍速菜单", timeout=PW_TIMEOUT_MS, force=True)
            await call_with_timeout_retry(
                page.wait_for_selector,
                "等待倍速菜单项可见",
                ".vjs-menu-item-text",
                state="visible",
                timeout=PW_TIMEOUT_MS,
            )
        except Exception:
            pass

    items = page.locator(".vjs-menu-item-text")
    if await items.count() == 0:
        btn = page.locator(".vjs-playback-rate")
        if await btn.count():
            await call_with_timeout_retry(btn.first.click, "打开倍速菜单", timeout=PW_TIMEOUT_MS, force=True)
            await page.wait_for_timeout(300)
        items = page.locator(".vjs-menu-item-text")

    if await items.count() == 0:
        raise SystemExit("未找到 vjs-menu-item-text（倍速菜单项），无法设置 2x")

    first = items.nth(0)
    t = ((await first.inner_text()) or "").strip()
    if t != "2x":
        raise SystemExit(f"倍速菜单第一项不是 2x，实际为：{t!r}")

    try:
        await call_with_timeout_retry(first.click, "设置倍速 2x", timeout=PW_TIMEOUT_MS)
    except SystemExit:
        await call_with_timeout_retry(first.click, "设置倍速 2x（force）", timeout=PW_TIMEOUT_MS, force=True)


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

    if "vjs-playing" not in cls:
        return

    await call_with_timeout_retry(play.click, "暂停播放", timeout=PW_TIMEOUT_MS)
    await page.wait_for_timeout(2000)
    await call_with_timeout_retry(
        page.reload,
        "暂停后刷新页面",
        wait_until="domcontentloaded",
        timeout=PW_TIMEOUT_MS,
    )
    await _wait_player_ready(page)
    await _set_speed_2x(page)
    await page.wait_for_timeout(5000)
    await _click_big_play_button(page)
    await page.wait_for_timeout(30000)

    play2 = page.locator(".vjs-play-control").first
    cls2 = (
        await call_with_timeout_retry(play2.get_attribute, "读取播放按钮class", "class", timeout=PW_TIMEOUT_MS)
    ) or ""
    if "vjs-paused" in cls2 and "vjs-ended" not in cls2:
        await call_with_timeout_retry(play2.click, "继续播放", timeout=PW_TIMEOUT_MS)
        return

    print("【找不到继续播放按钮】")


async def _is_ended(page: Page) -> bool:
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


async def _watch_one_course(page: Page, url: str, *, already_opened: bool = False) -> None:
    if not already_opened:
        await call_with_timeout_retry(page.goto, "打开课程详情页", url, wait_until="domcontentloaded", timeout=PW_TIMEOUT_MS)

    await _wait_player_ready(page)
    await _click_big_play_button(page)
    await _set_speed_2x(page)
    await _ensure_playing(page)

    last_pause = asyncio.get_running_loop().time()

    while True:
        now = asyncio.get_running_loop().time()
        if now - last_pause >= 60:
            await _pause_then_resume(page)
            last_pause = now

        if await _is_ended(page):
            await call_with_timeout_retry(page.reload, "播放结束后刷新页面", wait_until="domcontentloaded", timeout=PW_TIMEOUT_MS)
            print(f"【{url}已看完。{_ts()}】")
            return

        await asyncio.sleep(4)


async def perform_watch(
    username: str,
    password: str,
    open_only: bool,
    skip_login: bool,
    url_file: Path,
    lines_range: str | None,
) -> None:
    async with async_playwright() as p:
        endpoint = os.getenv("PLAYWRIGHT_CDP_ENDPOINT", "http://127.0.0.1:9222")
        browser = await connect_chrome_over_cdp(p, endpoint)

        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        context.set_default_timeout(PW_TIMEOUT_MS)

        auth_page: Page | None = None
        if not skip_login:
            auth_page = await context.new_page()
            await ensure_logged_in(
                auth_page, username=username, password=password, open_only=open_only, skip_login=skip_login
            )
            if open_only:
                input("请在浏览器中完成手动登录后，按 Enter 继续：")
            else:
                try:
                    await auth_page.close()
                except Exception:
                    pass

        personal_page = await _open_personal_center(context)
        if await _check_progress(personal_page):
            return

        urls_iter = iter(_iter_urls_from_file(url_file, lines_range=lines_range))
        first_url = next(urls_iter, None)
        if not first_url:
            print("[WARN] url.txt 中未找到任何 https URL，结束")
            return

        current_course = await context.new_page()
        print(f"[INFO] 开始播放：{first_url}")
        await _watch_one_course(current_course, first_url)

        if await _check_progress(personal_page):
            return

        for url in urls_iter:
            next_course = await context.new_page()
            await call_with_timeout_retry(
                next_course.goto,
                "打开课程详情页",
                url,
                wait_until="domcontentloaded",
                timeout=PW_TIMEOUT_MS,
            )
            await next_course.wait_for_timeout(3000)
            try:
                await current_course.close()
            except Exception:
                pass

            current_course = next_course
            print(f"[INFO] 开始播放：{url}")
            await _watch_one_course(current_course, url, already_opened=True)

            if await _check_progress(personal_page):
                return


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="带个人中心进度检测的自动看视频脚本")
    parser.add_argument("--username", default=None, help="登录用户名")
    parser.add_argument("--password", default=None, help="登录密码")
    parser.add_argument("--open-only", action="store_true", help="仅打开登录页，不自动填写/提交")
    parser.add_argument("--skip-login", action="store_true", help="已手动登录时使用，跳过登录流程")
    parser.add_argument("--url-file", default=str(URL_FILE), help="URL 文件路径（默认 url.txt）")
    parser.add_argument("--lines", default=None, help="读取的行范围：1 / 1- / 1-5（按 url.txt 行号）")
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
            lines_range=args.lines,
        )
    )


if __name__ == "__main__":
    main()
