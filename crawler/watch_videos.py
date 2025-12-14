import argparse
import asyncio
import os
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright, Page

from crawler.login import PW_TIMEOUT_MS, connect_chrome_over_cdp, ensure_logged_in, load_local_secrets


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
        s = (raw or "").strip()
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


async def _open_personal_center(context) -> Page:
    page = await context.new_page()
    await page.goto(PERSONAL_CENTER_URL, wait_until="domcontentloaded")
    return page


async def _check_progress(personal_page: Page) -> bool:
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
    await page.wait_for_selector(".vjs-tech", state="attached", timeout=PW_TIMEOUT_MS)
    await page.wait_for_selector(".vjs-current-time-display", state="attached", timeout=PW_TIMEOUT_MS)
    await page.wait_for_selector(".vjs-duration-display", state="attached", timeout=PW_TIMEOUT_MS)


async def _click_vjs_tech(page: Page) -> None:
    tech = page.locator(".vjs-tech").first
    if await tech.count() == 0:
        raise SystemExit("找不到 vjs-tech，无法点击播放/暂停")
    await tech.click(force=True, timeout=PW_TIMEOUT_MS)


async def _set_speed_2x(page: Page) -> None:
    btn = page.locator(".vjs-playback-rate").first
    if await btn.count():
        try:
            await btn.click(force=True, timeout=PW_TIMEOUT_MS)
        except Exception:
            pass

    await page.wait_for_timeout(200)

    items = page.locator(".vjs-menu-item-text")
    if await items.count() == 0:
        raise SystemExit("未找到 vjs-menu-item-text（倍速菜单项）")

    first = items.nth(0)
    text = ((await first.inner_text()) or "").strip()
    if text != "2x":
        raise SystemExit(f"倍速菜单第一项不是 2x，实际为：{text!r}")

    await first.click(force=True, timeout=PW_TIMEOUT_MS)


async def _is_replay_state(page: Page) -> bool:
    btn = page.locator("button.vjs-play-control.vjs-paused.vjs-ended").first
    if await btn.count() == 0:
        return False
    title = (await btn.get_attribute("title")) or ""
    return title.strip() == "Replay"


async def _watch_course_page(page: Page, url: str) -> None:
    await _wait_player_ready(page)

    await _click_vjs_tech(page)
    await page.wait_for_timeout(3000)

    await _click_vjs_tech(page)
    await page.wait_for_timeout(1000)

    await _set_speed_2x(page)

    await _click_vjs_tech(page)

    while True:
        try:
            current_text = ((await page.locator(".vjs-current-time-display").first.inner_text()) or "").strip()
            duration_text = ((await page.locator(".vjs-duration-display").first.inner_text()) or "").strip()
        except Exception:
            await page.wait_for_timeout(10000)
            continue

        cur = _parse_clock_text_to_seconds(current_text)
        dur = _parse_clock_text_to_seconds(duration_text)

        if cur is not None and dur is not None and cur == dur and await _is_replay_state(page):
            try:
                await page.reload(wait_until="domcontentloaded", timeout=15000)
            except Exception:
                pass
            print(f"【{url}已看完。{_ts()}】")
            return

        await page.wait_for_timeout(10000)


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
        endpoint = os.getenv("PLAYWRIGHT_CDP_ENDPOINT", "http://127.0.0.1:53333")
        browser = await connect_chrome_over_cdp(p, endpoint)

        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        context.set_default_timeout(PW_TIMEOUT_MS)

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
            await course_page.goto(url, wait_until="domcontentloaded")

            if prev_course_page is not None:
                await course_page.wait_for_timeout(3000)
                try:
                    await prev_course_page.close()
                except Exception:
                    pass

            prev_course_page = course_page

            await _watch_course_page(course_page, url)

            if await _check_progress(personal_page):
                return


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="看视频脚本：登录→个人中心进度→按 url.txt 新标签逐课播放（vjs-tech 控制）")
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
