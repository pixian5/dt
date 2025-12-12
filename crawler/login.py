import argparse
import asyncio
from pathlib import Path

from playwright.async_api import async_playwright


LOGIN_URL = "https://sso.dtdjzx.gov.cn/sso/login"


async def perform_login(username: str, password: str, headless: bool) -> None:
    data_dir = Path("data")
    data_dir.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
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
    parser = argparse.ArgumentParser(description="使用 Playwright 执行登录并保存截图")
    parser.add_argument("--headless", action="store_true", help="无头模式运行（默认可视化）")
    parser.add_argument("--username", default="15610654296", help="登录用户名")
    parser.add_argument("--password", default="136763FGS", help="登录密码")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    asyncio.run(perform_login(args.username, args.password, args.headless))


if __name__ == "__main__":
    main()
