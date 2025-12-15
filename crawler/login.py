import argparse
import asyncio
import json
import os
import subprocess
from pathlib import Path

from playwright.async_api import TimeoutError as PlaywrightTimeoutError, async_playwright, Page


LOGIN_URL = "https://sso.dtdjzx.gov.cn/sso/login"
MEMBER_URL = "https://www.dtdjzx.gov.cn/member/"

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
            user_data_dir = os.getenv("CHROME_CDP_USER_DATA_DIR", "~/chrome-cdp-53333")
            user_data_dir = os.path.expanduser(user_data_dir)
            try:
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
            except Exception:
                pass

            for _ in range(20):
                await asyncio.sleep(0.5)
                try:
                    browser = await p.chromium.connect_over_cdp(endpoint)
                    print(f"[INFO] 已连接本机 Chrome（CDP）：{endpoint}")
                    return browser
                except Exception:
                    continue

        raise SystemExit(
            "无法连接到本机 Chrome 的 CDP 端口："
            f"{endpoint}\n"
            "请先手动启动你的 Chrome 并开启远程调试端口，然后重试。\n"
            "macOS 示例：\n"
            "open -na \"Google Chrome\" --args --remote-debugging-port=53333 --user-data-dir=\"~/chrome-cdp-53333\"\n"
            "（如果你想用其它端口/地址，请设置环境变量 PLAYWRIGHT_CDP_ENDPOINT）"
        ) from exc


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



async def ensure_logged_in(
    page: Page, username: str, password: str, open_only: bool, skip_login: bool = False
) -> None:
    if skip_login:
        print("[INFO] 跳过登录（--skip-login）")
        return

    print(f"[INFO] 打开登录页：{LOGIN_URL}")
    await call_with_timeout_retry(page.goto, "打开登录页", LOGIN_URL, wait_until="load", timeout=PW_TIMEOUT_MS)
    await page.wait_for_timeout(1000)

    if page.url.startswith(MEMBER_URL) or "www.dtdjzx.gov.cn/member" in page.url:
        print(f"[INFO] 已检测到自动跳转 member（{page.url}），视为已登录，跳过登录流程")
        return

    if open_only:
        print("[INFO] open-only：仅打开登录页，不自动填写/提交")
        return

    try:
        await call_with_timeout_retry(page.wait_for_selector, "等待用户名输入框", "#username", timeout=PW_TIMEOUT_MS)
        await call_with_timeout_retry(page.wait_for_selector, "等待密码输入框", "#password", timeout=PW_TIMEOUT_MS)
        await call_with_timeout_retry(page.wait_for_selector, "等待验证码输入框", "#validateCode", timeout=PW_TIMEOUT_MS)
    except Exception:
        if page.url.startswith(MEMBER_URL) or "www.dtdjzx.gov.cn/member" in page.url:
            print(f"[INFO] 已检测到自动跳转 member（{page.url}），视为已登录，跳过登录流程")
            return
        raise

    await page.fill("#username", username)
    await page.fill("#password", password)

    print("【请在网页输入验证码】")
    print("[INFO] 请在网页中输入验证码并点击登录按钮，程序将每隔 2 秒检查是否已登录")

    max_wait_seconds = 300
    for i in range(max_wait_seconds // 2):
        await page.wait_for_timeout(2000)

        if page.url.startswith(MEMBER_URL) or "www.dtdjzx.gov.cn/member" in page.url:
            print(f"[INFO] 已检测到跳转 member（{page.url}），登录成功")
            return

        if (i + 1) % 10 == 0:
            print(f"[INFO] 仍未登录，继续等待（{(i + 1) * 2}/{max_wait_seconds}s），当前URL={page.url!r}")

    raise SystemExit(f"等待登录超时（{max_wait_seconds}s）：请确认已在网页输入验证码并点击登录，当前URL={page.url!r}")
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

        if load_state and state_file.exists() and state_file.is_file():
            if await _apply_storage_state_to_context(context, state_file):
                print(f"[INFO] 已从本地加载登录态：{state_file}")

        page = await context.new_page()
        await ensure_logged_in(page, username=username, password=password, open_only=open_only, skip_login=skip_login)

        if save_state:
            try:
                await _save_storage_state(context, state_file)
                print(f"[INFO] 已保存登录态：{state_file}")
            except Exception as exc:
                print(f"[WARN] 保存登录态失败：{exc}")

        return


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="使用 Playwright 执行登录（始终可视化模式）")
    parser.add_argument("--username", default=None, help="登录用户名")
    parser.add_argument("--password", default=None, help="登录密码")
    parser.add_argument("--open-only", dest="open_only", action="store_true", default=True, help="仅打开登录页，不自动填写/提交")
    parser.add_argument("--no-open-only", dest="open_only", action="store_false", help="关闭 open-only（允许自动填写/提交）")
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
    open_only = bool(args.open_only)
    keep_open = (not bool(args.close_after)) or open_only
    skip_login = bool(args.skip_login)

    username = args.username or os.getenv("DT_CRAWLER_USERNAME") or ""
    password = args.password or os.getenv("DT_CRAWLER_PASSWORD") or ""
    if not open_only and not skip_login:
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
