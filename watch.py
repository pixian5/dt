import argparse
import asyncio
import os
import re
import smtplib
import time
from datetime import datetime
from email.mime.text import MIMEText
from pathlib import Path

from playwright.async_api import async_playwright, Page

# 默认播放页定时刷新间隔（秒）
DEFAULT_REFRESH_INTERVAL = 30

from login import (
    LOGIN_URL,
    MEMBER_URL,
    PERSONAL_CENTER_URL,
    PW_TIMEOUT_MS,
    connect_chrome_over_cdp,
    ensure_logged_in,
    load_local_secrets,
    _save_storage_state,
)
STATE_FILE = Path(os.getenv("DT_STORAGE_STATE_FILE", "storage_state.json"))


#发邮件提醒
def send_email(subject, body, to_email='ibjxk0@gmail.com'):
    from_email = "hqlak47@gmail.com"
    password = "zkgamebmeqnyxwlj"

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = from_email
    msg["To"] = to_email
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(from_email, password)
            server.sendmail(from_email, to_email, msg.as_string())
    except Exception as e:
        print(f"邮件发送失败: {e}")

def _ts() -> str:
    now = datetime.now()
    return f"{now.day}日{now.strftime('%H:%M:%S')}"


def _ts_full() -> str:
    return datetime.now().strftime("%Y-%m-%d-%H:%M:%S")


def _log(msg: str) -> None:
    print(f"{_ts()} {msg}")




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
    parser.add_argument(
        "--refresh-interval",
        type=int,
        default=DEFAULT_REFRESH_INTERVAL,
        help=f"播放页定时刷新间隔（秒，默认 {DEFAULT_REFRESH_INTERVAL}）",
    )
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


async def _has_media_load_error(page: Page) -> bool:
    try:
        text = (
            (await page.locator('//*[@id="vjs_video_433"]/div[5]/div').first.inner_text())
            if await page.locator('//*[@id="vjs_video_433"]/div[5]/div').count()
            else ""
        )
        return (
            "The media could not be loaded, either because the server or network failed or because the format is not supported."
            in (text or "")
        )
    except Exception:
        return False

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


async def _read_watched_hours_text(page: Page) -> str:
    # First, try waiting briefly for the specific element to appear.
    try:
        await page.wait_for_selector(".plan-all-y", timeout=15000)
    except Exception:
        pass

    for _ in range(30):
        try:
            await page.wait_for_timeout(500)
            frames = [page] + list(page.frames)
            for fr in frames:
                try:
                    loc = fr.locator(".plan-right .plan-all-y, .plan-all-y").first
                    if await loc.count():
                        text = ((await loc.inner_text(timeout=1000)) or "").strip()
                        if text:
                            text = re.sub(r"\\s+", " ", text)
                            m = re.search(r"(已完成\\s*[:：]?\\s*\\d+(?:\\.\\d+)?\\s*(?:学时|课时)?)", text)
                            if m:
                                return m.group(1)
                            return text
                except Exception:
                    continue

            # fallback: read container text if present
            for fr in frames:
                try:
                    container = fr.locator(".plan-right").first
                    if await container.count():
                        t = ((await container.inner_text(timeout=1000)) or "").strip()
                        if t:
                            t = re.sub(r"\\s+", " ", t)
                            m = re.search(r"(已完成\\s*[:：]?\\s*\\d+(?:\\.\\d+)?\\s*(?:学时|课时)?)", t)
                            if m:
                                return m.group(1)
                except Exception:
                    continue

            # fallback: scan main page + frames
            frames = [page] + list(page.frames)
            for fr in frames:
                try:
                    body_text = await fr.evaluate(
                        """() => {
                            const t = document.body ? (document.body.innerText || '') : '';
                            return t;
                        }"""
                    )
                except Exception:
                    continue
                body_text = (body_text or "").strip()
                if not body_text:
                    continue
                body_text = re.sub(r"\\s+", " ", body_text)
                m = re.search(r"(已完成\\s*[:：]?\\s*\\d+(?:\\.\\d+)?\\s*(?:学时|课时)?)", body_text)
                if m:
                    return m.group(1)

            # last resort: evaluate at page context without frames list
            try:
                raw_text = await page.evaluate("() => document.body ? (document.body.innerText || '') : ''")
                raw_text = re.sub(r"\\s+", " ", (raw_text or "").strip())
                m = re.search(r"(已完成\\s*[:：]?\\s*\\d+(?:\\.\\d+)?\\s*(?:学时|课时)?)", raw_text)
                if m:
                    return m.group(1)
            except Exception:
                pass
        except Exception:
            pass
        await page.wait_for_timeout(1000)
    return ""


def _parse_hours_from_text(text: str | list[str]) -> float | None:
    if not text:
        return None
    if isinstance(text, list):
        for item in text:
            val = _parse_hours_from_text(item)
            if val is not None:
                return val
        joined = " ".join(str(x) for x in text if x)
        return _parse_hours_from_text(joined)
    m = re.search(r"(\d+(?:\.\d+)?)", str(text))
    if not m:
        return None
    try:
        return float(m.group(1))
    except Exception:
        return None


async def _read_watched_hours_value(page: Page) -> float | None:
    text = await _read_watched_hours_text(page)
    return _parse_hours_from_text(text)


def _format_hours_value(value: float) -> str:
    s = f"{value:.2f}"
    return s.rstrip("0").rstrip(".")


async def _read_plan_all_y_texts(page: Page) -> list[str]:
    texts: list[str] = []
    frames = [page] + list(page.frames)
    for fr in frames:
        try:
            loc = fr.locator(".plan-all-y")
            if await loc.count():
                items = await loc.all_inner_texts()
                for t in items:
                    t = re.sub(r"\s+", " ", (t or "").strip())
                    if t:
                        texts.append(t)
        except Exception:
            continue
    return texts


def _append_watched_diff(url: str, diff_hours: float, label: str = "差值") -> None:
    p = Path("已看")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    text = f"{_ts_full()}\n{url}\n{label}:{_format_hours_value(diff_hours)}课时\n\n"
    try:
        with p.open("a", encoding="utf-8") as f:
            f.write(text)
    except Exception as exc:
        _log(f"写入已看文件失败：{exc}")


async def _print_personal_center_status(context, page: Page) -> tuple[bool, Page]:
    for attempt in range(3):
        # 若不在个人中心路由，先跳回
        if "personalCenter" not in (page.url or ""):
            try:
                await page.goto(PERSONAL_CENTER_URL, wait_until="domcontentloaded", timeout=15000)
            except Exception:
                pass
            await page.wait_for_timeout(800)

        progress_text = await _read_progress_text(page)
        if progress_text:
            _log(f"个人中心进度：{progress_text}")
        else:
            _log(f"个人中心进度读取失败（url={page.url!r}）")
            return False, page

        watched_hours = await _read_watched_hours_text(page)
        value = _parse_hours_from_text(watched_hours)
        if value is None:
            plan_texts = await _read_plan_all_y_texts(page)
            value = _parse_hours_from_text(plan_texts)
        if value is not None:
            _log(f"个人中心已完成学时：{_format_hours_value(value)}")
        else:
            _log("个人中心已完成学时读取失败")

        if "100%" in progress_text:
            print(f"【{_ts_full()}-已看完100%】")
            return True, page

        if progress_text.strip() not in {"0", "0%"}:
            return False, page

        if attempt < 2:
            _log("进度为 0/0%，刷新后等待2s重新检查")
            page = await _refresh_personal_center(context, page)
            await page.wait_for_timeout(2000)

    _log("个人中心进度多次刷新仍为 0%，放弃继续刷新，你可能真没学")
    return False, page


def _remove_url_from_file(url_file: Path, url: str) -> None:
    if not url_file.exists() or not url_file.is_file():
        _log(f"URL 文件不存在，跳过删除：{url_file}")
        return

    try:
        lines = url_file.read_text(encoding="utf-8").splitlines()
    except Exception:
        try:
            lines = url_file.read_text(encoding="utf-8-sig").splitlines()
        except Exception as exc:
            _log(f"读取 URL 文件失败，跳过删除：{url_file} ({exc})")
            return

    removed = False
    new_lines: list[str] = []
    for raw in lines:
        if raw.strip() == url:
            removed = True
            continue
        new_lines.append(raw)

    if not removed:
        _log(f"未在 URL 文件中找到要删除的链接：{url}")
        return

    url_file.write_text("\n".join(new_lines) + ("\n" if new_lines else ""), encoding="utf-8")
    _log(f"已从 URL 文件删除：{url}")


async def _print_progress(context, page: Page) -> tuple[bool, Page]:
    return await _print_personal_center_status(context, page)


async def _goto_personal_center_in_current_tab(page: Page) -> None:
    await page.wait_for_timeout(1000)
    _log(f"在当前标签打开个人中心：{PERSONAL_CENTER_URL}")
    await page.goto(PERSONAL_CENTER_URL, wait_until="domcontentloaded", timeout=15000)

    start = time.monotonic()
    while time.monotonic() - start < 3:
        if "personalCenter" not in (page.url or ""):
            _log(f"检测到个人中心被跳转（url={page.url!r}），立即跳回个人中心")
            try:
                await page.goto(PERSONAL_CENTER_URL, wait_until="domcontentloaded", timeout=15000)
            except Exception:
                pass
        await page.wait_for_timeout(500)


async def _refresh_personal_center(context, page: Page | None, refocus_page: Page | None = None) -> Page:
    """
    新开标签打开个人中心；只关闭旧的“个人中心”标签，避免误关播放页/其它页面。
    """
    old_page = page if page is not None and not page.is_closed() else None

    new_page = await context.new_page()
    await new_page.goto(PERSONAL_CENTER_URL, wait_until="domcontentloaded", timeout=15000)
    if "personalCenter" not in (new_page.url or ""):
        try:
            await new_page.goto(PERSONAL_CENTER_URL, wait_until="domcontentloaded", timeout=15000)
        except Exception:
            pass
    if new_page.url.startswith(LOGIN_URL) or "sso/login" in (new_page.url or ""):
        _log("打开个人中心跳转到登录页，登录状态失效，发送邮件并退出")
        try:
            send_email(
                "登录失效提醒",
                f"打开个人中心跳转到登录页，登录已失效。\n当前URL={new_page.url}",
            )
        except Exception as exc:
            _log(f"发送邮件失败：{exc}")
        raise SystemExit("登录失效，已发送提醒邮件")

    # 仅当旧页本身就是个人中心时关闭它，避免误关播放页
    if old_page and old_page is not new_page and "personalCenter" in (old_page.url or ""):
        try:
            await old_page.close()
        except Exception:
            pass

    # 保证播放页在前台
    if refocus_page:
        try:
            await refocus_page.bring_to_front()
        except Exception:
            pass

    return new_page


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

    try:
        await first.click(force=True, timeout=PW_TIMEOUT_MS)
        return
    except Exception:
        pass

    try:
        parent = first.locator("xpath=..")
        await parent.click(force=True, timeout=PW_TIMEOUT_MS)
        return
    except Exception:
        pass

    await first.evaluate("(el) => el.click()")


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

    _log("点击全屏按钮")
    try:
        fs_btn = page.locator('xpath=//*[@id="vjs_video_433"]/div[4]/button[2]').first
        if await fs_btn.count():
            await fs_btn.click(force=True, timeout=PW_TIMEOUT_MS)
    except Exception as exc:
        _log(f"点击全屏按钮失败：{exc}")

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
        await page.reload(wait_until="domcontentloaded", timeout=15000)
        return
    except Exception as exc:
        _log(f"刷新失败，改用重新打开课程链接恢复（err={exc}）")

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=15000)
    except Exception as exc:
        _log(f"重新打开课程链接仍失败（err={exc}），稍后继续尝试")


async def _check_login_or_exit(page: Page, course_url: str) -> None:
    try:
        await page.goto(MEMBER_URL, wait_until="domcontentloaded", timeout=15000)
    except Exception:
        pass

    if page.url.startswith(LOGIN_URL) or "sso/login" in (page.url or ""):
        _log("检测到登录失效，发送邮件提醒并退出")
        try:
            send_email("登录失效提醒", f"检测到登录失效，请重新登录。\n当前URL={page.url}")
        except Exception as exc:
            _log(f"发送邮件失败：{exc}")
        raise SystemExit("登录失效，已发送提醒邮件")

    try:
        await page.goto(course_url, wait_until="domcontentloaded", timeout=15000)
        await _play_and_set_2x(page)
    except Exception:
        pass


async def _watch_course(
    context,
    page: Page,
    url: str,
    course_no: int,
    personal_page: Page,
    state_file: Path,
    refresh_interval: int,
    completed_hours_cache: float | None,
) -> tuple[Page | None, Page, str]:
    if await _has_media_load_error(page):
        _log("检测到媒体加载失败提示，跳过该课程")
        try:
            await page.close()
        except Exception:
            pass
        return None, personal_page, "skipped"

    _log("进入课程页，开始播放")
    await _play_and_set_2x(page)

    last_cur: int | None = None
    last_progress_ts = time.monotonic()
    refresh_attempts = 0
    missing_time_count = 0
    completion_candidate_ts: float | None = None
    periodic_refresh_ts = time.monotonic()
    post_refresh_check = False
    small_start_count = 0

    while True:
        if await _has_media_load_error(page):
            _log("播放过程中检测到媒体加载失败提示，跳过该课程")
            try:
                await page.close()
            except Exception:
                pass
            return None, personal_page, "skipped"

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

        _log(f"current={current_text} duration={duration_text}")
        if completed_hours_cache is not None:
            print(f"已看学时：{_format_hours_value(completed_hours_cache)}")
        else:
            print("已看学时：未知")

        if cur is not None:
            if last_cur is None:
                last_cur = cur
                last_progress_ts = time.monotonic()
                refresh_attempts = 0
                missing_time_count = 0
            elif cur != last_cur:
                last_cur = cur
                last_progress_ts = time.monotonic()
                refresh_attempts = 0
                missing_time_count = 0
        else:
            missing_time_count += 1

        if post_refresh_check and cur is not None:
            if cur < 66:
                await _check_login_or_exit(page, url)
                small_start_count += 1
                _log(f"定时刷新后起始时间<66s（第{small_start_count}次）")
                if small_start_count >= 2:
                    _log("连续2次刷新后起始时间<66s，判定已看完本课，跳过该课程")
                    try:
                        await page.close()
                    except Exception:
                        pass
                    return None, personal_page, "completed"
            else:
                small_start_count = 0
            post_refresh_check = False

        if refresh_interval > 0 and time.monotonic() - periodic_refresh_ts >= refresh_interval:
            _log(f"每隔{refresh_interval}s刷新播放页面并重新播放")
            await _recover_course_page(page, url, "定时刷新")
            # 同步刷新个人中心：新开个人中心标签，若旧标签也是个人中心则关闭，播放页保持前台
            try:
                personal_page = await _refresh_personal_center(context, personal_page, refocus_page=page)
                await personal_page.wait_for_timeout(500)
                try:
                    await page.bring_to_front()
                except Exception:
                    pass
                await _save_storage_state(context, state_file)
                _log(f"已保存登录态：{state_file}")
            except Exception as exc:
                _log(f"定时刷新个人中心失败：{exc}")
            try:
                await _play_and_set_2x(page)
            except Exception as exc:
                _log(f"定时刷新后重播失败（err={exc}）")
            periodic_refresh_ts = time.monotonic()
            post_refresh_check = True

        if time.monotonic() - last_progress_ts >= 60 or missing_time_count >= 6:
            if refresh_attempts >= 3:
                _log("播放多次重试仍未变化：跳过该课程")
                try:
                    await page.close()
                except Exception:
                    pass
                return None, personal_page, "skipped"

            refresh_attempts += 1
            if missing_time_count >= 6:
                _log(f"连续多次无法读取播放时间，关闭当前标签并新标签重试（{refresh_attempts}/3）")
            else:
                _log(f"播放 60s 未变化，关闭当前标签并新标签重试（{refresh_attempts}/3）")
            try:
                await page.close()
            except Exception:
                pass
            page = await context.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=15000)
            except Exception as exc:
                _log(f"新标签打开课程失败（err={exc}），继续等待下一次检测")
                last_progress_ts = time.monotonic()
                last_cur = None
                continue

            await _close_other_pages(context, {personal_page, page})
            try:
                await _play_and_set_2x(page)
            except Exception as exc:
                _log(f"新标签播放初始化失败（err={exc}），继续检测")

            last_progress_ts = time.monotonic()
            last_cur = None
            missing_time_count = 0
            completion_candidate_ts = None
            periodic_refresh_ts = time.monotonic()

        #如果当前时间、总时间都存在，而且当前时间接近总时间，说明可能播放完（播放完成后会卡在最后一秒）
        ended = bool(js_state.get("ended")) if isinstance(js_state, dict) else False
        if cur is not None and dur is not None and cur >= max(dur - 1, 88):
            now_ts = time.monotonic()
            if completion_candidate_ts is None:
                completion_candidate_ts = now_ts
            elif now_ts - completion_candidate_ts >= 3:
                if ended or await _is_replay_state(page):
                    try:
                        await page.reload(wait_until="domcontentloaded", timeout=15000)
                    except Exception:
                        pass
                    print(f"【{_ts_full()} 第{course_no}个课程 {url} 已看完。】")
                    return page, personal_page, "completed"
        else:
            completion_candidate_ts = None

        await page.wait_for_timeout(10000)


async def _close_other_pages(context, keep_pages: set[Page]) -> None:
    for p in list(context.pages):
        if p in keep_pages:
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
        # 如有 storage_state.json，优先加载复用登录态
        state_file_path = STATE_FILE if STATE_FILE.exists() else None
        if state_file_path:
            context = await browser.new_context(storage_state=str(state_file_path))
        context.set_default_timeout(PW_TIMEOUT_MS)

        existing = list(context.pages)
        personal_page = existing[-1] if existing else await context.new_page()
        try:
            await personal_page.bring_to_front()
        except Exception:
            pass

        await ensure_logged_in(personal_page, username=username, password=password, open_only=False, skip_login=False)

        await _close_other_pages(context, {personal_page})

        personal_page = await _refresh_personal_center(context, personal_page)
        await personal_page.wait_for_timeout(1000)
        initial_hours = await _read_watched_hours_value(personal_page)
        done_initial, personal_page = await _print_progress(context, personal_page)
        if done_initial:
            await _close_other_pages(context, {personal_page})
            return

        await _close_other_pages(context, {personal_page})

        url_file = Path(str(args.url_file)) if args.url_file else _pick_url_file()
        items = list(_iter_urls(url_file, lines_range=args.lines))
        if not items:
            raise SystemExit(f"未找到任何 https URL：{url_file}（lines={args.lines!r}）")

        _log(f"读取到课程数量：{len(items)}（file={str(url_file)!r} lines={args.lines!r}）")

        prev_course_page: Page | None = None
        completed_hours_cache: float | None = initial_hours

        for course_no, (line_no, url) in enumerate(items, start=1):
            # 开课前刷新个人中心并记录“开课前”学时
            personal_page = await _refresh_personal_center(context, personal_page)
            await personal_page.wait_for_timeout(1000)
            pre_hours = await _read_watched_hours_value(personal_page)
            done_pre, personal_page = await _print_progress(context, personal_page)
            if done_pre:
                await _close_other_pages(context, {personal_page})
                return
            if pre_hours is not None:
                completed_hours_cache = pre_hours

            course_page = await context.new_page()
            _log(f"新标签打开课程：\n{url}")
            await course_page.goto(url, wait_until="domcontentloaded", timeout=15000)

            if prev_course_page is not None:
                await _close_other_pages(context, {personal_page, course_page, prev_course_page})
            else:
                await _close_other_pages(context, {personal_page, course_page})

            if prev_course_page is not None:
                await course_page.wait_for_timeout(2000)
                try:
                    await prev_course_page.close()
                except Exception:
                    pass

            prev_course_page = course_page

            course_page, personal_page, status = await _watch_course(
                context,
                course_page,
                url,
                course_no,
                personal_page,
                STATE_FILE,
                int(args.refresh_interval),
                completed_hours_cache,
            )
            prev_course_page = course_page

            _log("课程结束：刷新个人中心并计算本课增量")
            personal_page = await _refresh_personal_center(context, personal_page, refocus_page=course_page)
            await personal_page.wait_for_timeout(1500)
            after_hours = await _read_watched_hours_value(personal_page)
            done_after, personal_page = await _print_progress(context, personal_page)
            diff_hours = None
            if pre_hours is not None and after_hours is not None:
                diff_hours = after_hours - pre_hours
                _log(f"本课新增学时：{_format_hours_value(diff_hours)}课时")
                _append_watched_diff(url, diff_hours, label="差值")
                completed_hours_cache = after_hours
            if diff_hours is not None and diff_hours != 0:
                _remove_url_from_file(url_file, url)
                status = "completed"

            if status in {"completed", "skipped"} and course_page is not None:
                try:
                    await course_page.close()
                except Exception:
                    pass
                prev_course_page = None

            if done_after:
                return

            if prev_course_page is not None:
                await _close_other_pages(context, {personal_page, prev_course_page})
            else:
                await _close_other_pages(context, {personal_page})


if __name__ == "__main__":
    asyncio.run(main())
