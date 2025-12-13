import argparse
import asyncio
import os
import subprocess
from pathlib import Path

from playwright.async_api import TimeoutError as PlaywrightTimeoutError, async_playwright, Page


LOGIN_URL = "https://sso.dtdjzx.gov.cn/sso/login"
INDEX_URL = "https://gbwlxy.dtdjzx.gov.cn/index"
COMMEND_URL = "https://gbwlxy.dtdjzx.gov.cn/content#/commendIndex"

PW_TIMEOUT_MS = 5000


async def connect_chrome_over_cdp(p, endpoint: str):
    try:
        browser = await p.chromium.connect_over_cdp(endpoint)
        print(f"[INFO] 已连接本机 Chrome（CDP）：{endpoint}")
        return browser
    except Exception as exc:
        local_9222 = endpoint in {"http://127.0.0.1:9222", "http://localhost:9222"}
        if local_9222:
            user_data_dir = os.getenv("CHROME_CDP_USER_DATA_DIR", "/tmp/chrome-cdp-9222")
            try:
                subprocess.Popen(
                    [
                        "open",
                        "-na",
                        "Google Chrome",
                        "--args",
                        "--remote-debugging-port=9222",
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
            "open -na \"Google Chrome\" --args --remote-debugging-port=9222 --user-data-dir=\"/tmp/chrome-cdp-9222\"\n"
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
        return await func(*args, **kwargs)


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



async def _is_logged_in(page: Page) -> bool:
    try:
        await call_with_timeout_retry(
            page.goto, "检测登录状态：打开列表页", COMMEND_URL, wait_until="networkidle", timeout=PW_TIMEOUT_MS
        )
        if "sso.dtdjzx.gov.cn" in page.url or "sso/login" in page.url:
            return False
        await call_with_timeout_retry(
            page.wait_for_selector, "检测登录状态：等待页码", ".number.active", timeout=PW_TIMEOUT_MS
        )
        return "commendIndex" in page.url
    except Exception:
        return False


async def ensure_logged_in(
    page: Page, username: str, password: str, open_only: bool, skip_login: bool = False
) -> None:
    if skip_login:
        print("[INFO] 跳过登录（--skip-login）")
        return

    if await _is_logged_in(page):
        print("[INFO] 已检测到当前会话已登录，跳过登录流程")
        return

    print(f"[INFO] 未登录，打开登录页：{LOGIN_URL}")
    await call_with_timeout_retry(page.goto, "打开登录页", LOGIN_URL, wait_until="load", timeout=PW_TIMEOUT_MS)
    await page.wait_for_timeout(1000)

    auto_logged_in = False
    if "dtdjzx.gov.cn/member" in page.url:
        auto_logged_in = True
        print("[INFO] 已检测到跳转 member，视为已登录，跳过输入验证码")
    else:
        try:
            await call_with_timeout_retry(page.wait_for_selector, "等待用户名输入框", "#username", timeout=PW_TIMEOUT_MS)
            await call_with_timeout_retry(page.wait_for_selector, "等待密码输入框", "#password", timeout=PW_TIMEOUT_MS)
            await call_with_timeout_retry(page.wait_for_selector, "等待验证码输入框", "#validateCode", timeout=PW_TIMEOUT_MS)
        except Exception:
            if "dtdjzx.gov.cn/member" in page.url:
                auto_logged_in = True
                print("[INFO] 已检测到跳转 member，视为已登录，跳过输入验证码")
            else:
                raise

    if not auto_logged_in and not open_only:
        await page.fill("#username", username)
        await page.fill("#password", password)

        captcha = input("请输入验证码（validateCode）：").strip()
        if not captcha:
            raise SystemExit("验证码不能为空")
        await page.fill("#validateCode", captcha)

        await call_with_timeout_retry(
            page.wait_for_selector, "等待登录提交按钮", "a.js-submit.tianze-loginbtn", timeout=PW_TIMEOUT_MS
        )
        await page.click("a.js-submit.tianze-loginbtn")
        await page.wait_for_timeout(3000)

    try:
        print(f"[INFO] 跳转到首页：{INDEX_URL}")
        await call_with_timeout_retry(page.goto, "跳转到首页", INDEX_URL, wait_until="networkidle", timeout=PW_TIMEOUT_MS)
        await page.wait_for_timeout(2000)
        try:
            login_btn = await page.query_selector("text=用户登录")
            if login_btn:
                print("[INFO] 检测到【用户登录】按钮，点击登录")
                await login_btn.click()
                await page.wait_for_timeout(2000)
        except Exception:
            pass
        print(f"[INFO] 跳转到列表页：{COMMEND_URL}")
        await call_with_timeout_retry(
            page.goto, "跳转到列表页", COMMEND_URL, wait_until="networkidle", timeout=PW_TIMEOUT_MS
        )
        await page.wait_for_timeout(1500)
    except Exception:
        return


async def perform_login(
    username: str, password: str, open_only: bool, keep_open: bool, skip_login: bool = False
) -> None:
    async with async_playwright() as p:
        endpoint = os.getenv("PLAYWRIGHT_CDP_ENDPOINT", "http://127.0.0.1:9222")
        browser = await connect_chrome_over_cdp(p, endpoint)

        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        context.set_default_timeout(PW_TIMEOUT_MS)
        page = await context.new_page()
        await ensure_logged_in(page, username=username, password=password, open_only=open_only, skip_login=skip_login)

        if keep_open:
            print("[INFO] 登录完成！浏览器已打开。按 Ctrl+C 退出并关闭浏览器。")
            try:
                while True:
                    await asyncio.sleep(3600)
            except KeyboardInterrupt:
                pass
        else:
            try:
                await page.close()
            except Exception:
                pass


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="使用 Playwright 执行登录（始终可视化模式）")
    parser.add_argument("--username", default=None, help="登录用户名")
    parser.add_argument("--password", default=None, help="登录密码")
    parser.add_argument("--open-only", action="store_true", help="仅打开登录页，不自动填写/提交")
    parser.add_argument(
        "--close-after", action="store_true", help="登录完成后自动关闭浏览器（默认保持打开，按 Ctrl+C 退出）"
    )
    parser.add_argument(
        "--skip-login", action="store_true", help="已手动登录时使用，跳过登录流程，直接执行后续跳转与点击"
    )
    return parser.parse_args(argv)


def login_flow(
    username: str, password: str, open_only: bool, keep_open: bool, skip_login: bool
) -> None:
    asyncio.run(perform_login(username, password, open_only=open_only, keep_open=keep_open, skip_login=skip_login))


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
    login_flow(username, password, open_only=open_only, keep_open=keep_open, skip_login=skip_login)


if __name__ == "__main__":
    main()
