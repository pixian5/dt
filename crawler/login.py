import argparse
import asyncio
import os
from pathlib import Path

from playwright.async_api import async_playwright


LOGIN_URL = "https://sso.dtdjzx.gov.cn/sso/login"


async def perform_login(username: str, password: str) -> None:
    data_dir = Path("data")
    data_dir.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()
        print(f"[INFO] 打开登录页：{LOGIN_URL}")
        await page.goto(LOGIN_URL, wait_until="load")

        await page.fill("#username", username)
        await page.fill("#password", password)

        captcha = input("请输入验证码（validateCode）：").strip()
        await page.fill("#validateCode", captcha)

        await page.click("a.js-submit.tianze-loginbtn")
        await page.wait_for_timeout(3000)

        screenshot_path = data_dir / "login_result.png"
        await page.screenshot(path=screenshot_path, full_page=True)
        print(f"[INFO] 已点击登录，截图保存：{screenshot_path}")

        await browser.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="使用 Playwright 执行登录并保存截图（始终可视化模式）")
    parser.add_argument("--username", default=None, help="登录用户名")
    parser.add_argument("--password", default=None, help="登录密码")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    username = args.username or os.getenv("DT_CRAWLER_USERNAME")
    password = args.password or os.getenv("DT_CRAWLER_PASSWORD")
    if not username or not password:
        raise SystemExit("请通过 --username/--password 或环境变量 DT_CRAWLER_USERNAME/DT_CRAWLER_PASSWORD 提供登录信息")
    asyncio.run(perform_login(username, password))


if __name__ == "__main__":
    main()
