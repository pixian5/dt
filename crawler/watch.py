import argparse
import asyncio
import os
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright, Page

from crawler.login import PW_TIMEOUT_MS, connect_chrome_over_cdp, ensure_logged_in, load_local_secrets


PERSONAL_CENTER_URL = "https://gbwlxy.dtdjzx.gov.cn/content#/personalCenter"


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d-%H:%M:%S")


def _log(msg: str) -> None:
    print(f"[{_ts()}] {msg}")


def _pick_url_file() -> Path:
    candidates = [Path("URL.txt"), Path("url.txt")]
    for p in candidates:
        if p.exists() and p.is_file():
            return p
    return candidates[-1]


def _parse_lines_range(lines_arg: str | None) -> tuple[int | None, int | None]:
    if not lines_arg:
        return None, None
    s = str(lines_arg).strip()
    if not s:
        return None, None

    if "-" not in s:
        try:
            n = int(s)
        except Exception as exc:
            raise SystemExit(f"--lines 参数格式错误：{lines_arg!r}（示例：32 / 32- / 32-34）") from exc
        if n <= 0:
            raise SystemExit(f"--lines 行号必须为正整数：{lines_arg!r}")
        return n, n

    start_s, end_s = [p.strip() for p in s.split("-", 1)]
    if not start_s:
        raise SystemExit(f"--lines 参数格式错误：{lines_arg!r}（示例：32- 或 32-34）")

    try:
        start = int(start_s)
    except Exception as exc:
        raise SystemExit(f"--lines 参数格式错误：{lines_arg!r}（示例：32- 或 32-34）") from exc
    if start <= 0:
        raise SystemExit(f"--lines 起始行号必须为正整数：{lines_arg!r}")

    if end_s == "":
        return start, None

    try:
        end = int(end_s)
    except Exception as exc:
        raise SystemExit(f"--lines 参数格式错误：{lines_arg!r}（示例：32- 或 32-34）") from exc
    if end <= 0:
        raise SystemExit(f"--lines 结束行号必须为正整数：{lines_arg!r}")
    if end < start:
        raise SystemExit(f"--lines 结束行号不能小于起始行号：{lines_arg!r}")

    return start, end


def _iter_urls(p: Path, *, lines_range: str | None = None):
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except Exception:
        try:
            lines = p.read_text(encoding="utf-8-sig").splitlines()
        except Exception as exc:
            raise SystemExit(f"无法读取 URL 文件：{p} ({exc})") from exc

    start, end = _parse_lines_range(lines_range)
    for idx, raw in enumerate(lines, start=1):
        if start is not None and idx < start:
            continue
        if end is not None and idx > end:
            break

        s = (raw or "").strip()
        if not s:
            continue
        if not s.startswith("https"):
            continue
        yield idx, s


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="看视频：登录→个人中心进度→按 URL.txt/url.txt 逐课播放（2x + 卡住刷新）")
    parser.add_argument("--url-file", default=None, help="URL 文件路径（默认优先 URL.txt，其次 url.txt）")
    parser.add_argument("--lines", default=None, help="读取的行范围：32 / 32- / 32-34（按 URL 文件行号）")
    return parser.parse_args(argv)


def _parse_clock_text_to_seconds(text: str) -> int | None:
    s = (text or "").strip()
    if not s:
        return None

    parts = [p.strip() for p in s.split(":")]
    if not all(p.isdigit() for p in parts):
        return None

    if len(parts) == 2:
        mm, ss = parts
        return int(mm) * 60 + int(ss)
    if len(parts) == 3:
        hh, mm, ss = parts
        return int(hh) * 3600 + int(mm) * 60 + int(ss)
    return None


async def _read_video_state_js(page: Page) -> dict | None:
    try:
        return await page.evaluate(
            """() => {
                const v = document.querySelector('video.vjs-tech');
                if (!v) return null;
                return {
                    currentTime: Number.isFinite(v.currentTime) ? v.currentTime : null,
                    duration: Number.isFinite(v.duration) ? v.duration : null,
                    paused: !!v.paused,
                    ended: !!v.ended,
                    readyState: v.readyState,
                };
            }"""
        )
    except Exception:
        return None


async def _read_progress_text(page: Page) -> str:
    loc = page.locator(".plan-all.pro").first
    for _ in range(30):
        try:
            if await loc.count() != 0:
                text = ((await loc.inner_text(timeout=1000)) or "").strip()
                if text:
                    return text
        except Exception:
            pass
        await page.wait_for_timeout(1000)
    return ""


async def _print_progress(page: Page) -> bool:
    for attempt in range(3):
        if attempt > 0:
            _log("进度为 0%，1s 后刷新个人中心并重新检查")
            await page.wait_for_timeout(1000)
            await _refresh_personal_center(page)
            await page.wait_for_timeout(2000)

        text = await _read_progress_text(page)
        if text:
            _log(f"个人中心进度（url={page.url!r}）：{text}")
        else:
            _log(f"个人中心进度读取失败（url={page.url!r}）")
            return False

        if "100%" in text:
            print(f"【{_ts()}-已看完100%】")
            return True
        if text != "0%":
            return False

    _log("个人中心进度多次刷新仍为 0%，放弃继续刷新")
    return False


async def _goto_personal_center_in_current_tab(page: Page) -> None:
    await page.wait_for_timeout(1000)
    for attempt in range(3):
        _log(f"在当前标签打开个人中心：{PERSONAL_CENTER_URL}（attempt={attempt + 1}/3）")
        await page.goto(PERSONAL_CENTER_URL, wait_until="domcontentloaded", timeout=15000)

        try:
            await page.wait_for_selector(".plan-all.pro", state="attached", timeout=5000)
        except Exception:
            pass

        if "/index" not in page.url:
            if await page.locator(".plan-all.pro").count() != 0:
                return

        _log(f"个人中心未就绪或疑似被重定向（url={page.url!r}），1s 后重试")
        await page.wait_for_timeout(1000)


async def _refresh_personal_center(page: Page) -> None:
    try:
        await page.reload(wait_until="domcontentloaded", timeout=15000)
    except Exception:
        await page.goto(PERSONAL_CENTER_URL, wait_until="domcontentloaded", timeout=15000)


async def _wait_player_ready(page: Page) -> None:
    await page.wait_for_selector(".vjs-tech", state="attached", timeout=PW_TIMEOUT_MS)
    await page.wait_for_selector(".vjs-current-time-display", state="attached", timeout=PW_TIMEOUT_MS)
    await page.wait_for_selector(".vjs-duration-display", state="attached", timeout=PW_TIMEOUT_MS)


async def _click_vjs_tech(page: Page, action: str) -> None:
    tech = page.locator(".vjs-tech").first
    if await tech.count() == 0:
        raise SystemExit(f"找不到 vjs-tech，无法执行：{action}")
    await tech.click(force=True, timeout=PW_TIMEOUT_MS)


async def _ensure_playing(page: Page, reason: str) -> None:
    try:
        state = await page.evaluate(
            """async () => {
                const v = document.querySelector('video.vjs-tech, .vjs-tech');
                if (!v) return { ok: false, err: 'no-video' };
                try { v.muted = true; } catch (e) {}
                try { await v.play(); } catch (e) { return { ok: false, err: String(e), paused: v.paused, readyState: v.readyState, currentTime: v.currentTime }; }
                return { ok: true, paused: v.paused, readyState: v.readyState, currentTime: v.currentTime };
            }"""
        )
        if isinstance(state, dict) and state.get("ok"):
            return
    except Exception:
        pass

    try:
        btn = page.locator("button.vjs-play-control").first
        if await btn.count() != 0:
            await btn.click(force=True, timeout=PW_TIMEOUT_MS)
    except Exception:
        pass


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


async def _play_and_set_2x(page: Page) -> None:
    await _wait_player_ready(page)

    _log("点击 vjs-tech：开始播放")
    await _click_vjs_tech(page, "开始播放")
    await page.wait_for_timeout(1000)

    _log("点击 vjs-tech：暂停（1s）")
    await _click_vjs_tech(page, "暂停（1s）")
    await page.wait_for_timeout(1000)

    _log("设置倍速：点击第一个 vjs-menu-item-text（期望 2x）")
    await _set_speed_2x(page)

    _log("点击 vjs-tech：恢复播放（2x）")
    await _click_vjs_tech(page, "恢复播放（2x）")
    await _ensure_playing(page, "设置 2x 后恢复播放")


async def _is_replay_state(page: Page) -> bool:
    btn = page.locator("button.vjs-play-control.vjs-control.vjs-button.vjs-paused.vjs-ended").first
    if await btn.count() == 0:
        return False
    title = (await btn.get_attribute("title")) or ""
    return title.strip() == "Replay"


async def _recover_course_page(page: Page, url: str, reason: str) -> None:
    _log(f"{reason}：尝试刷新页面恢复")
    try:
        await page.reload(wait_until="domcontentloaded", timeout=60000)
        return
    except Exception as exc:
        _log(f"刷新失败，改用重新打开课程链接恢复（err={exc}）")

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    except Exception as exc:
        _log(f"重新打开课程链接仍失败（err={exc}），稍后继续尝试")


async def _watch_course(page: Page, url: str) -> None:
    _log(f"进入课程页，开始播放流程：{url}")
    await _play_and_set_2x(page)

    last_cur: int | None = None
    stalled = 0

    while True:
        current_text = ""
        duration_text = ""
        try:
            current_text = ((await page.locator(".vjs-current-time-display").first.inner_text()) or "").strip()
            duration_text = ((await page.locator(".vjs-duration-display").first.inner_text()) or "").strip()
        except Exception:
            pass

        cur = _parse_clock_text_to_seconds(current_text)
        dur = _parse_clock_text_to_seconds(duration_text)

        js_state = None
        if cur is None or dur is None:
            js_state = await _read_video_state_js(page)
            if isinstance(js_state, dict):
                if cur is None and isinstance(js_state.get("currentTime"), (int, float)):
                    cur = int(js_state["currentTime"])
                if dur is None and isinstance(js_state.get("duration"), (int, float)):
                    dur = int(js_state["duration"])

        _log(f"播放检测：current={current_text} duration={duration_text}")

        if cur is not None:
            if last_cur is None:
                last_cur = cur
                stalled = 0
            else:
                if cur == last_cur:
                    stalled += 1
                else:
                    stalled = 0
                    last_cur = cur

        if stalled >= 2:
            _log(f"播放疑似卡住（连续{stalled}次时间未变化，cur={cur}），刷新页面并重试播放初始化")
            await _recover_course_page(page, url, "播放疑似卡住")
            try:
                await _play_and_set_2x(page)
            except Exception as exc:
                _log(f"重试播放初始化失败（err={exc}），稍后继续检测")
            stalled = 0
            last_cur = None
            await page.wait_for_timeout(10000)
            continue

        if cur is not None and dur is not None and cur == dur:
            if await _is_replay_state(page):
                try:
                    await page.reload(wait_until="domcontentloaded", timeout=60000)
                except Exception:
                    pass
                print(f"【{_ts()} {url}已看完。】")
                return

        await page.wait_for_timeout(10000)


async def _close_other_pages(context, keep_page: Page) -> None:
    for p in list(context.pages):
        if p is keep_page:
            continue
        try:
            await p.close()
        except Exception:
            pass


async def main(argv: list[str] | None = None) -> None:
    load_local_secrets()

    args = parse_args(argv)

    username = os.getenv("DT_CRAWLER_USERNAME") or ""
    password = os.getenv("DT_CRAWLER_PASSWORD") or ""

    async with async_playwright() as p:
        endpoint = os.getenv("PLAYWRIGHT_CDP_ENDPOINT", "http://127.0.0.1:53333")
        browser = await connect_chrome_over_cdp(p, endpoint)

        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        context.set_default_timeout(PW_TIMEOUT_MS)

        existing = list(context.pages)
        personal_page = existing[-1] if existing else await context.new_page()
        try:
            await personal_page.bring_to_front()
        except Exception:
            pass

        await ensure_logged_in(personal_page, username=username, password=password, open_only=False, skip_login=False)

        await _goto_personal_center_in_current_tab(personal_page)
        if await _print_progress(personal_page):
            await _close_other_pages(context, personal_page)
            return

        await _close_other_pages(context, personal_page)

        url_file = Path(str(args.url_file)) if args.url_file else _pick_url_file()
        items = list(_iter_urls(url_file, lines_range=args.lines))
        if not items:
            raise SystemExit(f"未找到任何 https URL：{url_file}（lines={args.lines!r}）")

        _log(f"读取到课程数量：{len(items)}（file={str(url_file)!r} lines={args.lines!r}）")

        prev_course_page: Page | None = None

        for line_no, url in items:
            course_page = await context.new_page()
            _log(f"第 {line_no} 行课程：{url}")
            _log(f"新标签打开课程：{url}")
            await course_page.goto(url, wait_until="domcontentloaded", timeout=15000)

            if prev_course_page is not None:
                await course_page.wait_for_timeout(2000)
                try:
                    await prev_course_page.close()
                except Exception:
                    pass

            prev_course_page = course_page

            await _watch_course(course_page, url)

            _log("课程结束：刷新个人中心检查进度")
            await _refresh_personal_center(personal_page)
            await personal_page.wait_for_timeout(2000)
            if await _print_progress(personal_page):
                return


if __name__ == "__main__":
    asyncio.run(main())
