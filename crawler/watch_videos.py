import argparse
import asyncio
import json
import os
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright, Page

from crawler.login import PW_TIMEOUT_MS, connect_chrome_over_cdp, ensure_logged_in, load_local_secrets


PERSONAL_CENTER_URL = "https://gbwlxy.dtdjzx.gov.cn/content#/personalCenter"
URL_FILE = Path("url.txt")
DEFAULT_STATE_FILE = Path("storage_state.json")


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _log(msg: str) -> None:
    print(f"[{_ts()}] {msg}")


async def _apply_storage_state_to_context(context, state_file: Path) -> bool:
    try:
        raw = state_file.read_text(encoding="utf-8")
        state = json.loads(raw)
    except Exception:
        return False

    cookies = state.get("cookies") or []
    if cookies:
        try:
            await context.add_cookies(cookies)
        except Exception:
            pass

    origins = state.get("origins") or []
    for origin_entry in origins:
        origin = (origin_entry or {}).get("origin")
        items = (origin_entry or {}).get("localStorage") or []
        if not origin or not items:
            continue
        try:
            p = await context.new_page()
            try:
                await p.goto(origin, wait_until="domcontentloaded", timeout=15000)
                await p.evaluate(
                    """(items) => {
                        for (const it of items) {
                            if (!it || !it.name) continue;
                            try { localStorage.setItem(it.name, it.value ?? ''); } catch (e) {}
                        }
                    }""",
                    items,
                )
            finally:
                await p.close()
        except Exception:
            continue

    return True


async def _save_storage_state(context, state_file: Path) -> None:
    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    await context.storage_state(path=str(state_file))


async def _try_save_storage_state(context, state_file: Path, *, reason: str) -> bool:
    try:
        _log(f"保存登录态（{reason}）：{state_file}")
        await _save_storage_state(context, state_file)
        _log(f"已保存登录态：{state_file}")
        return True
    except Exception as exc:
        _log(f"保存登录态失败（{reason}）：{exc}")
        return False


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
    _log(f"打开个人中心页面：{PERSONAL_CENTER_URL}")
    await page.goto(PERSONAL_CENTER_URL, wait_until="domcontentloaded")
    return page


async def _goto_personal_center(page: Page) -> None:
    for attempt in range(2):
        try:
            _log(f"在当前标签跳转个人中心（第{attempt+1}次）：{PERSONAL_CENTER_URL}")
            await page.goto(PERSONAL_CENTER_URL, wait_until="domcontentloaded", timeout=15000)
        except Exception:
            _log("跳转个人中心失败（忽略，继续校验/重试）")

        try:
            await page.wait_for_function(
                "() => location.href.includes('personalCenter') || location.hash.includes('personalCenter')",
                timeout=15000,
            )
            _log(f"已进入个人中心：{page.url}")
            return
        except Exception:
            _log(f"个人中心URL校验失败，当前URL={page.url!r}")
            await page.wait_for_timeout(1000)

    raise SystemExit("打开个人中心失败：多次重试仍未进入 #/personalCenter")


async def _check_progress(personal_page: Page) -> bool:
    try:
        _log(f"刷新个人中心：{PERSONAL_CENTER_URL}")
        await personal_page.goto(PERSONAL_CENTER_URL, wait_until="domcontentloaded", timeout=15000)
    except Exception:
        _log("刷新个人中心 goto 失败（忽略，继续校验是否仍在 personalCenter）")

    if "personalCenter" not in (personal_page.url or ""):
        _log(f"刷新后疑似被重定向，当前URL={personal_page.url!r}，尝试跳回个人中心")
        await _goto_personal_center(personal_page)

    _log("等待 4s 让个人中心进度渲染")
    await personal_page.wait_for_timeout(4000)

    loc = personal_page.locator(".plan-all.pro").first
    zero_seen = 0
    for _ in range(30):
        try:
            if await loc.count() != 0:
                text = ((await loc.inner_text(timeout=1000)) or "").strip()
                if text:
                    _log(f"个人中心进度文本：{text}")
                    if "100%" in text:
                        print(f"【{_ts()}-已看完100%】")
                        return True
                    if text == "0%":
                        zero_seen += 1
                        if zero_seen < 5:
                            _log("进度为 0%（可能未渲染完成），继续等待")
                            await personal_page.wait_for_timeout(1000)
                            continue
                    return False
        except Exception:
            pass
        await personal_page.wait_for_timeout(1000)
    _log("个人中心进度元素读取超时（30s），视为未完成")
    return False


async def _wait_player_ready(page: Page) -> None:
    _log("等待播放器元素就绪：.vjs-tech / .vjs-current-time-display / .vjs-duration-display")
    await page.wait_for_selector(".vjs-tech", state="attached", timeout=PW_TIMEOUT_MS)
    await page.wait_for_selector(".vjs-current-time-display", state="attached", timeout=PW_TIMEOUT_MS)
    await page.wait_for_selector(".vjs-duration-display", state="attached", timeout=PW_TIMEOUT_MS)


async def _click_vjs_tech(page: Page, action: str) -> None:
    _log(f"点击播放器 vjs-tech：{action}")
    tech = page.locator(".vjs-tech").first
    if await tech.count() == 0:
        raise SystemExit("找不到 vjs-tech，无法点击播放/暂停")
    await tech.click(force=True, timeout=PW_TIMEOUT_MS)


async def _ensure_playing(page: Page, reason: str) -> None:
    _log(f"确保播放中：{reason}")
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
        _log(f"播放状态（js-play）：{state}")
        if isinstance(state, dict) and state.get("ok"):
            return
    except Exception as exc:
        _log(f"js play 调用失败（忽略）：{exc}")

    try:
        btn = page.locator("button.vjs-play-control").first
        if await btn.count() != 0:
            _log("兜底：点击 vjs-play-control 按钮")
            await btn.click(force=True, timeout=PW_TIMEOUT_MS)
    except Exception as exc:
        _log(f"点击 vjs-play-control 失败（忽略）：{exc}")


async def _set_speed_2x(page: Page) -> None:
    _log("设置倍速：打开倍速菜单")
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

    _log("设置倍速：点击 2x")
    await first.click(force=True, timeout=PW_TIMEOUT_MS)


async def _is_replay_state(page: Page) -> bool:
    btn = page.locator("button.vjs-play-control.vjs-paused.vjs-ended").first
    if await btn.count() == 0:
        return False
    title = (await btn.get_attribute("title")) or ""
    return title.strip() == "Replay"


async def _watch_course_page(page: Page, url: str) -> None:
    _log(f"进入课程页，开始播放流程：{url}")
    await _wait_player_ready(page)

    await _click_vjs_tech(page, "开始播放")
    await page.wait_for_timeout(1000)

    await _click_vjs_tech(page, "暂停（1s）")
    await page.wait_for_timeout(1000)

    await _set_speed_2x(page)

    await _click_vjs_tech(page, "恢复播放（2x）")
    await _ensure_playing(page, "设置 2x 后恢复播放")

    last_cur: int | None = None
    stalled = 0

    while True:
        try:
            current_text = ((await page.locator(".vjs-current-time-display").first.inner_text()) or "").strip()
            duration_text = ((await page.locator(".vjs-duration-display").first.inner_text()) or "").strip()
        except Exception:
            _log("读取播放时间失败，3s 后重试")
            await page.wait_for_timeout(3000)
            continue

        cur = _parse_clock_text_to_seconds(current_text)
        dur = _parse_clock_text_to_seconds(duration_text)

        _log(f"播放检测：current={current_text!r} duration={duration_text!r}")

        if cur is not None:
            if last_cur is None:
                last_cur = cur
                stalled = 0
            else:
                if cur == last_cur:
                    stalled += 1
                elif cur < last_cur:
                    _log(f"检测到播放时间回退：{last_cur} -> {cur}")
                    stalled = 0
                else:
                    stalled = 0
                    last_cur = cur

        if stalled >= 3:
            _log(f"播放疑似卡住（连续{stalled}次未前进，cur={cur}），尝试恢复播放")
            await _ensure_playing(page, f"检测到卡住 cur={cur}")
            stalled = 0

        if cur is not None and dur is not None and cur == dur:
            replay = await _is_replay_state(page)
            _log(f"播放结束判断：cur==dur，replay={replay}")
        else:
            replay = False

        if cur is not None and dur is not None and cur == dur and replay:
            try:
                _log("检测到 Replay，刷新课程页")
                await page.reload(wait_until="domcontentloaded", timeout=15000)
            except Exception:
                _log("刷新课程页失败（忽略）")
                pass
            print(f"【{url}已看完。{_ts()}】")
            return

        await page.wait_for_timeout(3000)


async def perform_watch(
    *,
    username: str,
    password: str,
    skip_login: bool,
    url_file: Path,
    lines_range: str | None,
    state_file: Path,
    load_state: bool,
    save_state: bool,
) -> None:
    async with async_playwright() as p:
        endpoint = os.getenv("PLAYWRIGHT_CDP_ENDPOINT", "http://127.0.0.1:53333")
        _log(f"连接 Chrome CDP：{endpoint}")
        browser = await connect_chrome_over_cdp(p, endpoint)

        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        context.set_default_timeout(PW_TIMEOUT_MS)

        if load_state and state_file.exists() and state_file.is_file():
            if await _apply_storage_state_to_context(context, state_file):
                _log(f"已从本地加载登录态：{state_file}")

        try:
            personal_page: Page
            existing_pages = list(context.pages)
            if existing_pages:
                personal_page = existing_pages[-1]
                try:
                    await personal_page.bring_to_front()
                except Exception:
                    pass
                _log(f"复用已有标签作为个人中心页：{personal_page.url}")
            else:
                personal_page = await context.new_page()
                _log("未发现已有标签，创建新标签作为个人中心页")

            if not skip_login:
                _log("开始登录检测/登录流程")
                await ensure_logged_in(
                    personal_page,
                    username=username,
                    password=password,
                    open_only=False,
                    skip_login=False,
                )
                if save_state:
                    await _try_save_storage_state(context, state_file, reason="登录后")
            else:
                _log("skip-login：跳过登录流程")

            await personal_page.wait_for_timeout(1000)
            await _goto_personal_center(personal_page)
            if save_state:
                await _try_save_storage_state(context, state_file, reason="进入个人中心后")
            if await _check_progress(personal_page):
                return

            urls = list(_iter_urls_from_file(url_file, lines_range=lines_range))
            if not urls:
                print("[WARN] url.txt 中未找到任何 https URL，结束")
                return

            _log(f"读取到课程 URL 数量：{len(urls)}")

            prev_course_page: Page | None = None

            for url in urls:
                course_page = await context.new_page()
                _log(f"新标签打开课程页：{url}")
                await course_page.goto(url, wait_until="domcontentloaded")

                if prev_course_page is not None:
                    await course_page.wait_for_timeout(3000)
                    try:
                        _log("关闭上一课程标签页")
                        await prev_course_page.close()
                    except Exception:
                        _log("关闭上一课程标签页失败（忽略）")
                        pass

                prev_course_page = course_page

                await _watch_course_page(course_page, url)

                _log("课程播放结束，回个人中心检查进度")
                if await _check_progress(personal_page):
                    return
        finally:
            if save_state:
                try:
                    await asyncio.shield(_try_save_storage_state(context, state_file, reason="退出前"))
                except BaseException as exc:
                    _log(f"退出前保存登录态被中断/失败：{exc}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="看视频脚本：登录→个人中心进度→按 url.txt 新标签逐课播放（vjs-tech 控制）")
    parser.add_argument("--username", default=None, help="登录用户名")
    parser.add_argument("--password", default=None, help="登录密码")
    parser.add_argument("--skip-login", action="store_true", help="已手动登录时使用，跳过登录流程")
    parser.add_argument("--url-file", default=str(URL_FILE), help="URL 文件路径（默认 url.txt）")
    parser.add_argument("--lines", default=None, help="读取的行范围：1 / 1- / 1-5（按 url.txt 行号）")
    parser.add_argument("--state-file", default=str(DEFAULT_STATE_FILE), help="登录态保存文件（storage_state）")
    parser.add_argument("--no-load-state", action="store_true", help="不从本地文件加载登录态")
    parser.add_argument("--no-save-state", action="store_true", help="不保存登录态到本地文件")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    load_local_secrets()
    args = parse_args(argv)
    skip_login = bool(args.skip_login)

    username = args.username or os.getenv("DT_CRAWLER_USERNAME") or ""
    password = args.password or os.getenv("DT_CRAWLER_PASSWORD") or ""

    if not skip_login:
        if not username or not password:
            raise SystemExit(
                "缺少登录信息：请通过参数 --username/--password，或环境变量 DT_CRAWLER_USERNAME/DT_CRAWLER_PASSWORD，"
                "或在项目根目录创建 secrets.local.env 提供"
            )

    state_file = Path(str(args.state_file))
    load_state = not bool(args.no_load_state)
    save_state = not bool(args.no_save_state)

    asyncio.run(
        perform_watch(
            username=username,
            password=password,
            skip_login=skip_login,
            url_file=Path(args.url_file),
            lines_range=args.lines,
            state_file=state_file,
            load_state=load_state,
            save_state=save_state,
        )
    )


if __name__ == "__main__":
    main()
