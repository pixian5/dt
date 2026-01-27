import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from playwright.async_api import TimeoutError as PlaywrightTimeoutError, async_playwright, Page


LOGIN_URL = "https://sso.dtdjzx.gov.cn/sso/login"
MEMBER_URL = "https://www.dtdjzx.gov.cn/member/"
PERSONAL_CENTER_URL = "https://gbwlxy.dtdjzx.gov.cn/content#/personalCenter"

PW_TIMEOUT_MS = 4000
DEFAULT_STATE_FILE = Path("storage_state.json")


async def connect_chrome_over_cdp(p, endpoint: str):
    try:
        browser = await p.chromium.connect_over_cdp(endpoint)
        print(f"[INFO] 已连接本机 Chrome（CDP）：{endpoint}")
        return browser
    except Exception as exc:
        local_53333 = endpoint in {"http://127.0.0.1:53333", "http://localhost:53333"}
        if local_53333:
            open_extensions = False
            if os.getenv("CHROME_CDP_USER_DATA_DIR"):
                user_data_dir = os.getenv("CHROME_CDP_USER_DATA_DIR")
                user_data_dir = os.path.expanduser(user_data_dir)
                print(f"[INFO] 使用 CHROME_CDP_USER_DATA_DIR：{user_data_dir}")
            else:
                src_profile = _default_chrome_user_data_dir()
                dest_profile = _default_cdp_user_data_dir()
                try:
                    empty_before = not Path(dest_profile).exists() or not any(Path(dest_profile).iterdir())
                    user_data_dir = _ensure_cdp_profile_dir(src_profile, dest_profile)
                    open_extensions = empty_before
                    print(f"[INFO] 已准备 CDP 用户目录：{user_data_dir}")
                except Exception as copy_exc:
                    print(f"[WARN] 准备 CDP 用户目录失败（{copy_exc}），改用临时目录")
                    user_data_dir = tempfile.mkdtemp(prefix="chrome-cdp-53333-")
            try:
                _launch_chrome_with_cdp(user_data_dir)
            except Exception:
                pass

            for _ in range(20):
                await asyncio.sleep(0.5)
                try:
                    browser = await p.chromium.connect_over_cdp(endpoint)
                    print(f"[INFO] 已连接本机 Chrome（CDP）：{endpoint}")
                    if open_extensions:
                        try:
                            ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
                            page = await ctx.new_page()
                            await page.goto("chrome://extensions", wait_until="domcontentloaded")
                            print("[INFO] 已打开 chrome://extensions（请手动开启开发者模式）")
                            login_page = await ctx.new_page()
                            await login_page.goto(LOGIN_URL, wait_until="domcontentloaded")
                            print("[INFO] 已在新标签打开登录页")
                        except Exception:
                            pass
                    return browser
                except Exception:
                    continue

        raise SystemExit(
            "无法连接到本机 Chrome 的 CDP 端口："
            f"{endpoint}\n"
            "请先手动启动你的 Chrome 并开启远程调试端口，然后重试。\n"
            f"{_platform_launch_hint()}\n"
            "（如果你想用其它端口/地址，请设置环境变量 PLAYWRIGHT_CDP_ENDPOINT）"
        ) from exc


def _default_chrome_user_data_dir() -> str:
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/Google/Chrome")
    if os.name == "nt":
        local_appdata = os.getenv("LOCALAPPDATA", "")
        return str(Path(local_appdata) / "Google" / "Chrome" / "User Data")
    return os.path.expanduser("~/.config/google-chrome")


def _default_cdp_user_data_dir() -> str:
    if sys.platform == "darwin":
        return os.path.expanduser("~/chrome-cdp-53333")
    if os.name == "nt":
        local_appdata = os.getenv("LOCALAPPDATA", "")
        return str(Path(local_appdata) / "chrome-cdp-53333")
    return os.path.expanduser("~/.config/chrome-cdp-53333")


def _ensure_cdp_profile_dir(src_dir: str, dest_dir: str) -> str:
    src = Path(os.path.expanduser(src_dir))
    if not src.exists() or not src.is_dir():
        raise FileNotFoundError(f"Chrome 用户目录不存在：{src}")

    dest = Path(os.path.expanduser(dest_dir))
    dest.mkdir(parents=True, exist_ok=True)
    if any(dest.iterdir()):
        return str(dest)

    # 仅复制插件相关目录与配置文件，避免带入用户的登录态/缓存
    # 根目录：扩展目录 + flags 等全局配置
    root_ext = src / "Extensions"
    if root_ext.exists():
        shutil.copytree(root_ext, dest / "Extensions", dirs_exist_ok=True)

    root_local_state = src / "Local State"
    if root_local_state.exists():
        shutil.copy2(root_local_state, dest / "Local State")

    # Profile 目录：扩展配置与规则（含油猴脚本、uBlock 规则等）
    allow_dirs = {"Default"} | {p.name for p in src.glob("Profile *") if p.is_dir()}
    for name in allow_dirs:
        src_path = src / name
        if not src_path.exists():
            continue
        dest_path = dest / name
        # 仅复制 profile 内扩展相关文件
        dest_path.mkdir(parents=True, exist_ok=True)
        for fname in ("Preferences", "Secure Preferences"):
            fsrc = src_path / fname
            if fsrc.exists():
                shutil.copy2(fsrc, dest_path / fname)

        ext_dir = src_path / "Extensions"
        if ext_dir.exists():
            shutil.copytree(ext_dir, dest_path / "Extensions", dirs_exist_ok=True)

        for dname in (
            "Extension State",
            "Local Extension Settings",
            "Sync Extension Settings",
            "Managed Extension Settings",
        ):
            dsrc = src_path / dname
            if dsrc.exists():
                shutil.copytree(dsrc, dest_path / dname, dirs_exist_ok=True)

    return str(dest)


def _find_chrome_executable() -> str | None:
    if sys.platform == "darwin":
        return "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if os.name == "nt":
        candidates = []
        for base in (os.getenv("PROGRAMFILES"), os.getenv("PROGRAMFILES(X86)"), os.getenv("LOCALAPPDATA")):
            if not base:
                continue
            candidates.append(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe")
        for path in candidates:
            if path.exists():
                return str(path)
        return "chrome.exe"
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        if shutil.which(name):
            return name
    return None


def _launch_chrome_with_cdp(user_data_dir: str) -> None:
    user_data_dir = os.path.expanduser(user_data_dir)
    if sys.platform == "darwin":
        subprocess.Popen(
            [
                "open",
                "-na",
                "Google Chrome",
                "--args",
                "--remote-debugging-port=53333",
                f"--user-data-dir={user_data_dir}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return
    chrome_exec = _find_chrome_executable() or "chrome"
    subprocess.Popen(
        [
            chrome_exec,
            "--remote-debugging-port=53333",
            f"--user-data-dir={user_data_dir}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _platform_launch_hint() -> str:
    if sys.platform == "darwin":
        return "macOS 示例：\nopen -na \"Google Chrome\" --args --remote-debugging-port=53333 --user-data-dir=\"$HOME/chrome-cdp-53333\""
    if os.name == "nt":
        return "Windows 示例：\n\"%LOCALAPPDATA%\\Google\\Chrome\\Application\\chrome.exe\" --remote-debugging-port=53333 --user-data-dir=\"%LOCALAPPDATA%\\chrome-cdp-53333\""
    return "Linux 示例：\ngoogle-chrome --remote-debugging-port=53333 --user-data-dir=\"$HOME/.config/chrome-cdp-53333\""


async def call_with_timeout_retry(func, action: str, /, *args, **kwargs):
    timeout = kwargs.get("timeout")
    if timeout is None:
        kwargs["timeout"] = PW_TIMEOUT_MS
    else:
        kwargs["timeout"] = min(int(timeout), PW_TIMEOUT_MS)
    try:
        return await func(*args, **kwargs)
    except PlaywrightTimeoutError:
        print(f"[WARN] {action} 超时 {PW_TIMEOUT_MS}ms，重试 1 次")
        try:
            return await func(*args, **kwargs)
        except PlaywrightTimeoutError as exc:
            raise SystemExit(f"{action} 超时 {PW_TIMEOUT_MS}ms，重试仍失败：{exc}") from exc


async def _get_user_login_reference_text(page: Page) -> str:
    try:
        el = await call_with_timeout_retry(
            page.wait_for_selector,
            "检测用户登录按钮",
            ".el-popover__reference",
            timeout=PW_TIMEOUT_MS,
        )
        return ((await el.inner_text()) or "").strip()
    except Exception:
        return ""


def load_local_secrets() -> None:
    candidates = [Path("secrets.local.env"), Path(__file__).resolve().parents[1] / "secrets.local.env"]
    for p in candidates:
        if not p.exists() or not p.is_file():
            continue
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and value and not os.getenv(key):
                    os.environ[key] = value
        except Exception:
            return


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
                await call_with_timeout_retry(p.goto, "加载登录态：打开origin", origin, wait_until="domcontentloaded")
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


async def _wait_for_login(page: Page, *, interval_seconds: int = 2) -> None:
    print(f"[INFO] 程序将每隔 {interval_seconds} 秒检查是否已登录")
    log_every = max(1, int(10 // interval_seconds))
    i = 0

    while True:
        await page.wait_for_timeout(interval_seconds * 1000)

        if page.url.startswith(MEMBER_URL) or "www.dtdjzx.gov.cn/member" in page.url:
            print(f"[INFO] 已检测到跳转 member（{page.url}），登录成功")
            return

        i += 1
        if i % log_every == 0:
            elapsed = i * interval_seconds
            print(f"[INFO] 仍未登录，继续等待（{elapsed}s），当前URL={page.url!r}")


def _is_logged_in_by_url(page: Page) -> bool:
    return page.url.startswith(MEMBER_URL) or "www.dtdjzx.gov.cn/member" in page.url


async def ensure_logged_in(
    page: Page, username: str, password: str, open_only: bool, skip_login: bool = False
) -> None:
    if skip_login:
        print("[INFO] 跳过登录（--skip-login）")
        return

    print(f"[INFO] 打开登录页：{LOGIN_URL}")
    await call_with_timeout_retry(page.goto, "打开登录页", LOGIN_URL, wait_until="load", timeout=PW_TIMEOUT_MS)
    await page.wait_for_timeout(1000)

    if _is_logged_in_by_url(page):
        print(f"[INFO] 已检测到自动跳转 member（{page.url}），视为已登录，跳过登录流程")
        return

    try:
        await call_with_timeout_retry(page.wait_for_selector, "等待用户名输入框", "#username", timeout=PW_TIMEOUT_MS)
        await call_with_timeout_retry(page.wait_for_selector, "等待密码输入框", "#password", timeout=PW_TIMEOUT_MS)
        await call_with_timeout_retry(page.wait_for_selector, "等待验证码输入框", "#validateCode", timeout=PW_TIMEOUT_MS)
    except Exception:
        if _is_logged_in_by_url(page):
            print(f"[INFO] 已检测到自动跳转 member（{page.url}），视为已登录，跳过登录流程")
            return
        raise

    await page.fill("#username", username)
    await page.fill("#password", password)

    captcha_task = asyncio.create_task(asyncio.to_thread(input, "请输入网页验证码："))
    while True:
        if _is_logged_in_by_url(page):
            print(f"[INFO] 已检测到跳转 member（{page.url}），登录成功")
            return
        if captcha_task.done():
            captcha = (captcha_task.result() or "").strip()
            break
        await page.wait_for_timeout(1000)

    if not captcha:
        print("[INFO] 验证码为空，请在浏览器输入验证码并提交，程序每隔 1 秒检查是否已登录")
        await _wait_for_login(page, interval_seconds=1)
        return
    await page.fill("#validateCode", captcha)

    submitted = False
    for sel in (
        'button:has-text("登录")',
        'button:has-text("登 录")',
        "button[type=submit]",
        "input[type=submit]",
        "#login",
        "#loginBtn",
        ".login-btn",
        ".btn-login",
    ):
        try:
            loc = page.locator(sel).first
            if await loc.count() != 0:
                await loc.click(force=True, timeout=PW_TIMEOUT_MS)
                submitted = True
                break
        except Exception:
            continue

    if not submitted:
        try:
            await page.locator("#validateCode").press("Enter", timeout=PW_TIMEOUT_MS)
            submitted = True
        except Exception:
            pass

    if not submitted:
        try:
            await page.evaluate(
                """() => {
                    const el = document.querySelector('#validateCode');
                    const form = el ? el.closest('form') : document.querySelector('form');
                    if (form) { form.submit(); return true; }
                    return false;
                }"""
            )
        except Exception:
            pass

    if submitted:
        print("[INFO] 已提交登录")
        await _wait_for_login(page, interval_seconds=2)
    else:
        print("[INFO] 未确认提交登录，仍将每隔 1 秒检查是否已登录")
        await _wait_for_login(page, interval_seconds=1)
    return


async def perform_login(
    username: str,
    password: str,
    open_only: bool,
    keep_open: bool,
    skip_login: bool = False,
    state_file: Path = DEFAULT_STATE_FILE,
    load_state: bool = True,
    save_state: bool = True,
) -> None:
    async with async_playwright() as p:
        endpoint = os.getenv("PLAYWRIGHT_CDP_ENDPOINT", "http://127.0.0.1:53333")
        browser = await connect_chrome_over_cdp(p, endpoint)

        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        context.set_default_timeout(PW_TIMEOUT_MS)

        loaded_state = False
        if load_state and state_file.exists() and state_file.is_file():
            if await _apply_storage_state_to_context(context, state_file):
                loaded_state = True
                print(f"[INFO] 已从本地加载登录态：{state_file}")

        page = await context.new_page()
        open_only_effective = False
        if loaded_state:
            state_valid = False
            try:
                print("[INFO] 校验已加载的登录态是否有效（打开个人中心）")
                await page.goto(PERSONAL_CENTER_URL, wait_until="domcontentloaded", timeout=PW_TIMEOUT_MS)
                await page.wait_for_timeout(1000)
                state_valid = _is_logged_in_by_url(page) or "personalCenter" in (page.url or "")
                if state_valid:
                    print(f"[INFO] 登录态有效（{page.url}）")
                else:
                    print(f"[WARN] 登录态失效（当前URL={page.url!r}），将走正常登录流程")
            except Exception as exc:
                print(f"[WARN] 登录态校验失败（{exc}），将走正常登录流程")

            if not state_valid:
                try:
                    state_file.unlink()
                    print(f"[WARN] 已删除失效登录态文件：{state_file}")
                except Exception:
                    print(f"[WARN] 删除登录态文件失败：{state_file}")

                try:
                    await context.clear_cookies()
                except Exception:
                    pass

                try:
                    await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=PW_TIMEOUT_MS)
                    await page.evaluate(
                        """() => {
                            try { localStorage.clear(); } catch (e) {}
                            try { sessionStorage.clear(); } catch (e) {}
                        }"""
                    )
                except Exception:
                    pass
                open_only_effective = False

        await ensure_logged_in(
            page,
            username=username,
            password=password,
            open_only=open_only_effective,
            skip_login=skip_login,
        )

        if save_state and _is_logged_in_by_url(page):
            try:
                await _save_storage_state(context, state_file)
                print(f"[INFO] 已保存登录态：{state_file}")
            except Exception as exc:
                print(f"[WARN] 保存登录态失败：{exc}")
        elif save_state:
            print("[WARN] 未检测到有效登录态，跳过保存 storage_state.json")

        return


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="使用 Playwright 执行登录（始终可视化模式）")
    parser.add_argument("--username", default=None, help="登录用户名")
    parser.add_argument("--password", default=None, help="登录密码")
    parser.add_argument(
        "--close-after", action="store_true", help="登录完成后自动关闭浏览器（默认保持打开，按 Ctrl+C 退出）"
    )
    parser.add_argument(
        "--skip-login", action="store_true", help="已手动登录时使用，跳过登录流程，直接执行后续跳转与点击"
    )
    parser.add_argument("--state-file", default=str(DEFAULT_STATE_FILE), help="登录态保存文件（storage_state）")
    parser.add_argument("--no-load-state", action="store_true", help="不从本地文件加载登录态")
    parser.add_argument("--no-save-state", action="store_true", help="不保存登录态到本地文件")
    return parser.parse_args(argv)


def login_flow(
    username: str, password: str, open_only: bool, keep_open: bool, skip_login: bool
) -> None:
    state_file = Path(os.getenv("DT_STORAGE_STATE_FILE", str(DEFAULT_STATE_FILE)))
    asyncio.run(
        perform_login(
            username,
            password,
            open_only=open_only,
            keep_open=keep_open,
            skip_login=skip_login,
            state_file=state_file,
            load_state=True,
            save_state=True,
        )
    )


def main(argv: list[str] | None = None) -> None:
    load_local_secrets()
    args = parse_args(argv)
    open_only = False
    keep_open = not bool(args.close_after)
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
        perform_login(
            username,
            password,
            open_only=open_only,
            keep_open=keep_open,
            skip_login=skip_login,
            state_file=state_file,
            load_state=load_state,
            save_state=save_state,
        )
    )


if __name__ == "__main__":
    main()
