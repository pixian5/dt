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


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


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


def _parse_clock_text_to_seconds(text: str) -> int | None:
    s = (text or "").strip()
    if not s:
        return None
    parts = s.split(":")
    try:
        nums = [int(p) for p in parts]
    except Exception:
        return None

    total = 0
    for n in nums:
        total = total * 60 + n
    return total


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


async def _dom_click_first_menu_item_text(page: Page) -> bool:
    try:
        return bool(
            await page.evaluate(
                """() => {
                    const el = document.querySelectorAll('.vjs-menu-item-text')[0];
                    if (!el) return false;
                    el.click();
                    return true;
                }"""
            )
        )
    except Exception:
        return False


async def _open_personal_center(context) -> Page:
    page = await context.new_page()
    try:
        await call_with_timeout_retry(page.goto, "打开个人中心", PERSONAL_CENTER_URL, wait_until="domcontentloaded")
    except (SystemExit, Exception):
        try:
            await page.goto(PERSONAL_CENTER_URL, wait_until="domcontentloaded", timeout=15000)
        except Exception:
            pass
    return page


async def _check_progress(personal_page: Page) -> bool:
    try:
        await call_with_timeout_retry(
            personal_page.goto,
            "刷新个人中心",
            PERSONAL_CENTER_URL,
            wait_until="domcontentloaded",
            timeout=PW_TIMEOUT_MS,
        )
    except (SystemExit, Exception):
        try:
            await personal_page.goto(PERSONAL_CENTER_URL, wait_until="domcontentloaded", timeout=15000)
        except Exception:
            pass

    loc = personal_page.locator(".plan-all.pro").first
    for _ in range(30):
        try:
            if await loc.count() != 0:
                text = ((await loc.inner_text(timeout=1000)) or "").strip()
                if text:
                    if "100%" in text:
                        print(f"【{_ts()}-已看完100%】")
                        return True
                    return False
        except Exception:
            pass
        await personal_page.wait_for_timeout(1000)
    return False


async def _wait_player_ready(page: Page) -> None:
    await call_with_timeout_retry(
        page.wait_for_selector,
        "等待播放器video",
        ".vjs-tech",
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


async def _click_vjs_tech(page: Page, action: str) -> None:
    tech = page.locator(".vjs-tech").first
    if await tech.count() != 0:
        try:
            await call_with_timeout_retry(tech.click, action, timeout=PW_TIMEOUT_MS, force=True)
            return
        except (SystemExit, Exception):
            pass

    if await _dom_click_first(page, ".vjs-tech"):
        return

    raise SystemExit("找不到 vjs-tech，无法点击播放/暂停")


async def _click_big_play_button(page: Page) -> None:
    btn = page.locator(".vjs-big-play-button").first
    if await btn.count() != 0:
        try:
            await call_with_timeout_retry(btn.click, "点击大播放按钮", timeout=PW_TIMEOUT_MS, force=True)
            return
        except (SystemExit, Exception):
            pass

    if await _dom_click_first(page, ".vjs-big-play-button"):
        return


async def _click_resume_play_control_if_paused(page: Page) -> bool:
    play = page.locator(".vjs-play-control").first
    if await play.count() == 0:
        return False

    cls = (await play.get_attribute("class")) or ""
    if "vjs-paused" in cls and "vjs-ended" not in cls:
        try:
            await call_with_timeout_retry(play.click, "继续播放", timeout=PW_TIMEOUT_MS, force=True)
            return True
        except (SystemExit, Exception):
            return await _dom_click_first(page, ".vjs-play-control")

    return False


async def _set_speed_2x(page: Page) -> None:
    btn = page.locator(".vjs-playback-rate").first
    if await btn.count():
        try:
            await call_with_timeout_retry(btn.click, "打开倍速菜单", timeout=PW_TIMEOUT_MS, force=True)
        except (SystemExit, Exception):
            await _dom_click_first(page, ".vjs-playback-rate")

    await page.wait_for_timeout(200)

    items = page.locator(".vjs-menu-item-text")
    if await items.count() == 0:
        raise SystemExit("未找到 vjs-menu-item-text（倍速菜单项）")

    first = items.nth(0)
    text = ((await first.inner_text()) or "").strip()
    if text != "2x":
        raise SystemExit(f"倍速菜单第一项不是 2x，实际为：{text!r}")

    try:
        await first.click(force=True)
        return
    except Exception:
        pass

    if await _dom_click_first_menu_item_text(page):
        return

    raise SystemExit("设置倍速 2x 失败")


async def _pause_reload_resume(page: Page) -> None:
    await _click_vjs_tech(page, "每分钟暂停")
    await page.wait_for_timeout(2000)

    await call_with_timeout_retry(
        page.reload,
        "暂停后刷新页面",
        wait_until="domcontentloaded",
        timeout=PW_TIMEOUT_MS,
    )

    await _wait_player_ready(page)
    await page.wait_for_timeout(5000)

    await _click_big_play_button(page)

    await page.wait_for_timeout(30000)

    if not await _click_resume_play_control_if_paused(page):
        print("【找不到继续播放按钮】")


async def _is_video_replay_state(page: Page) -> bool:
    btn = page.locator(".vjs-play-control.vjs-paused.vjs-ended").first
    if await btn.count() == 0:
        return False

    title = (await btn.get_attribute("title")) or ""
    return title.strip() == "Replay"


async def _watch_course_page(page: Page, url: str) -> None:
    await _wait_player_ready(page)

    await _click_vjs_tech(page, "点击视频开始播放")
    await page.wait_for_timeout(3000)

    await _click_vjs_tech(page, "暂停播放（3s）")
    await page.wait_for_timeout(1000)

    await _set_speed_2x(page)
    await _click_vjs_tech(page, "开始2x播放")

    loop = asyncio.get_running_loop()
    next_pause_at = loop.time() + 60.0

    while True:
        now = loop.time()
        if now >= next_pause_at:
            await _pause_reload_resume(page)
            next_pause_at = loop.time() + 60.0

        try:
            current_text = (
                (await page.locator(".vjs-current-time-display").first.inner_text()) or ""
            ).strip()
            duration_text = (
                (await page.locator(".vjs-duration-display").first.inner_text()) or ""
            ).strip()
        except Exception:
            await page.wait_for_timeout(4000)
            continue

        cur = _parse_clock_text_to_seconds(current_text)
        dur = _parse_clock_text_to_seconds(duration_text)

        if cur is not None and dur is not None and cur == dur and await _is_video_replay_state(page):
            await call_with_timeout_retry(
                page.reload,
                "播放结束后刷新页面",
                wait_until="domcontentloaded",
                timeout=PW_TIMEOUT_MS,
            )
            print(f"【{url}已看完。{_ts()}】")
            return

        await page.wait_for_timeout(4000)


async def perform_watch(
    *,
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

        login_page = None
        if not skip_login:
            login_page = await context.new_page()
            await ensure_logged_in(login_page, username=username, password=password, open_only=open_only, skip_login=False)
            if open_only:
                input("请在浏览器中完成手动登录后，按 Enter 继续：")
            else:
                try:
                    await login_page.close()
                except Exception:
                    pass

        personal_page = await _open_personal_center(context)
        if await _check_progress(personal_page):
            return

        urls = list(_iter_urls_from_file(url_file, lines_range=lines_range))
        if not urls:
            print("[WARN] url.txt 中未找到任何 https URL，结束")
            return

        prev_course_page: Page | None = None

        for url in urls:
            course_page = await context.new_page()
            await call_with_timeout_retry(course_page.goto, "打开课程页", url, wait_until="domcontentloaded")

            if prev_course_page is not None:
                await course_page.wait_for_timeout(3000)
                try:
                    await prev_course_page.close()
                except Exception:
                    pass

            prev_course_page = course_page

            print(f"[INFO] 开始播放：{url}")
            await _watch_course_page(course_page, url)

            if await _check_progress(personal_page):
                return


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="基于 vjs-tech 点击播放/暂停的自动看视频脚本（含个人中心进度检测）")
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
