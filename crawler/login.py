import argparse
import asyncio
import os
from pathlib import Path

from playwright.async_api import async_playwright


LOGIN_URL = "https://sso.dtdjzx.gov.cn/sso/login"


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


async def perform_login(username: str, password: str, open_only: bool, keep_open: bool) -> None:
    data_dir = Path("data")
    data_dir.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()
        print(f"[INFO] 打开登录页：{LOGIN_URL}")
        await page.goto(LOGIN_URL, wait_until="load")
        await page.wait_for_selector("#username")
        await page.wait_for_selector("#password")
        await page.wait_for_selector("#validateCode")

        if not open_only:
            await page.fill("#username", username)
            await page.fill("#password", password)

            captcha = input("请输入验证码（validateCode）：").strip()
            if not captcha:
                raise SystemExit("验证码不能为空")
            await page.fill("#validateCode", captcha)

            await page.wait_for_selector("a.js-submit.tianze-loginbtn")
            await page.click("a.js-submit.tianze-loginbtn")
            await page.wait_for_timeout(3000)

            screenshot_path = data_dir / "login_result.png"
            await page.screenshot(path=screenshot_path, full_page=True)
            print(f"[INFO] 已点击登录，截图保存：{screenshot_path}")

        if keep_open:
            print("[INFO] 浏览器已打开。按 Ctrl+C 退出并关闭浏览器。")
            try:
                while True:
                    await asyncio.sleep(3600)
            except KeyboardInterrupt:
                pass

        await browser.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="使用 Playwright 执行登录并保存截图（始终可视化模式）")
    parser.add_argument("--username", default=None, help="登录用户名")
    parser.add_argument("--password", default=None, help="登录密码")
    parser.add_argument("--open-only", action="store_true", help="仅打开登录页，不自动填写/提交")
    parser.add_argument("--keep-open", action="store_true", help="保持浏览器窗口，直到 Ctrl+C 退出")
    return parser.parse_args()


def main() -> None:
    load_local_secrets()
    args = parse_args()
    open_only = bool(args.open_only)
    keep_open = bool(args.keep_open) or open_only

    username = args.username or os.getenv("DT_CRAWLER_USERNAME") or ""
    password = args.password or os.getenv("DT_CRAWLER_PASSWORD") or ""
    if not open_only:
        if not username or not password:
            raise SystemExit(
                "缺少登录信息：请通过参数 --username/--password，或环境变量 DT_CRAWLER_USERNAME/DT_CRAWLER_PASSWORD，"
                "或在项目根目录创建 secrets.local.env 提供"
            )
    asyncio.run(perform_login(username, password, open_only=open_only, keep_open=keep_open))


if __name__ == "__main__":
    main()
