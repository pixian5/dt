import argparse
import asyncio
import os
from pathlib import Path

from playwright.async_api import async_playwright, Page


LOGIN_URL = "https://sso.dtdjzx.gov.cn/sso/login"
INDEX_URL = "https://gbwlxy.dtdjzx.gov.cn/index"
COMMEND_URL = "https://gbwlxy.dtdjzx.gov.cn/content#/commendIndex"


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
        await page.goto(COMMEND_URL, wait_until="networkidle", timeout=30000)
        if "sso.dtdjzx.gov.cn" in page.url or "sso/login" in page.url:
            return False
        await page.wait_for_selector(".number.active", timeout=5000)
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
    await page.goto(LOGIN_URL, wait_until="load", timeout=30000)
    await page.wait_for_timeout(1000)

    auto_logged_in = False
    if "dtdjzx.gov.cn/member" in page.url:
        auto_logged_in = True
        print("[INFO] 已检测到跳转 member，视为已登录，跳过输入验证码")
    else:
        try:
            await page.wait_for_selector("#username", timeout=4000)
            await page.wait_for_selector("#password", timeout=4000)
            await page.wait_for_selector("#validateCode", timeout=4000)
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

        await page.wait_for_selector("a.js-submit.tianze-loginbtn", timeout=4000)
        await page.click("a.js-submit.tianze-loginbtn")
        await page.wait_for_timeout(3000)

    try:
        print(f"[INFO] 跳转到首页：{INDEX_URL}")
        await page.goto(INDEX_URL, wait_until="networkidle", timeout=30000)
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
        await page.goto(COMMEND_URL, wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(1500)
    except Exception:
        return


async def perform_login(
    username: str, password: str, open_only: bool, keep_open: bool, skip_login: bool = False
) -> None:
    async with async_playwright() as p:
        reuse_browser = False
        browser = None
        endpoint = os.getenv("PLAYWRIGHT_CDP_ENDPOINT", "http://127.0.0.1:9222")
        try:
            browser = await p.chromium.connect_over_cdp(endpoint)
            reuse_browser = True
            print(f"[INFO] 复用已启动的浏览器：{endpoint}")
        except Exception:
            browser = await p.chromium.launch(
                headless=False,
                args=["--remote-debugging-port=9222"],
            )
            print("[INFO] 启动新浏览器（启用 CDP 端口 9222 以便下次复用）")

        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        context.set_default_timeout(4000)
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
            if not reuse_browser:
                await browser.close()


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
