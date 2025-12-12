import argparse
import asyncio
import os
from pathlib import Path

from playwright.async_api import async_playwright, Page


LOGIN_URL = "https://sso.dtdjzx.gov.cn/sso/login"
INDEX_URL = "https://gbwlxy.dtdjzx.gov.cn/index"
COMMEND_URL = "https://gbwlxy.dtdjzx.gov.cn/content#/commendIndex"

RIGHT_WARP_SELECTOR = (
    "#domhtml > div.app-wrapper.hideSidebar > div > section > div > div > "
    "div.container-warp-index > div.right-warp > div"
)
# 使用更宽松的卡片选择器，避免层级变化导致无法获取
VIDEO_CARD_SELECTOR = ".video-warp-start"
STATE_SELECTOR = ".state-paused"
DETAIL_SPAN_SELECTOR = (
    "#domhtml > div.app-wrapper.hideSidebar.withoutAnimation > div > section > div > "
    "div:nth-child(2) > div.MainVideo.el-row > div.top-right-warp > div > "
    "div:nth-child(1) > div:nth-child(7) > div.titleContent > span"
)
URL_OUTPUT_FILE = Path("url.txt")


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


async def _get_active_page_number(page: Page) -> str:
    try:
        el = await page.wait_for_selector(".number.active", timeout=5000)
        return (await el.inner_text()).strip()
    except Exception:
        return ""


async def _append_url(url: str) -> None:
    URL_OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with URL_OUTPUT_FILE.open("a", encoding="utf-8") as f:
        f.write(url + "\n")


async def _goto_page_number(page: Page, target_text: str) -> bool:
    if not target_text:
        return False
    try:
        numbers = await page.query_selector_all(".number")
        for n in numbers:
            txt = (await n.inner_text()).strip()
            if txt == target_text:
                await n.click()
                await page.wait_for_timeout(800)
                return True
    except Exception:
        return False

    for _ in range(10):
        try:
            quick = await page.query_selector(".btn-quicknext")
            if not quick:
                return False
            await quick.click()
            await page.wait_for_timeout(300)
            numbers = await page.query_selector_all(".number")
            for n in numbers:
                txt = (await n.inner_text()).strip()
                if txt == target_text:
                    await n.click()
                    await page.wait_for_timeout(800)
                    return True
        except Exception:
            return False
    return False


async def _recover_to_commend(page: Page, expected_page_text: str) -> None:
    if "/content#/commendIndex" not in page.url:
        try:
            await page.goto(COMMEND_URL, wait_until="networkidle")
            await page.wait_for_timeout(800)
        except Exception:
            return
    if expected_page_text:
        current = await _get_active_page_number(page)
        if current != expected_page_text:
            await _goto_page_number(page, expected_page_text)
            await page.wait_for_timeout(500)


async def _wait_detail_yes_no(page: Page) -> str:
    try:
        await page.wait_for_selector("div.titleContent > span", timeout=15000, state="visible")
        await page.wait_for_function(
            """() => {
                const el = document.querySelector('div.titleContent > span');
                if (!el) return false;
                const t = (el.innerText || '').trim();
                return t === '是' || t === '否';
            }""",
            timeout=15000,
        )
        el = await page.query_selector("div.titleContent > span")
        if not el:
            return ""
        return ((await el.inner_text()) or "").strip()
    except Exception:
        return ""


async def _get_next_page_target(current_text: str) -> str | None:
    try:
        num = int(current_text)
        return str(num + 1)
    except Exception:
        return None


async def perform_login(
    username: str,
    password: str,
    open_only: bool,
    keep_open: bool,
    skip_login: bool = False,
    start_page: int | None = None,
) -> None:
    data_dir = Path("data")
    data_dir.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        reuse_browser = False
        browser = None
        page = None
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
        if skip_login:
            print("[INFO] 跳过登录，直接进行后续操作")
        else:
            print(f"[INFO] 打开登录页：{LOGIN_URL}")
            await page.goto(LOGIN_URL, wait_until="load", timeout=10000)
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

        # 跳转到 index，检查【用户登录】按钮并点击（如有），再跳转 commendIndex，记录当前页码
        try:
            print(f"[INFO] 跳转到首页：{INDEX_URL}")
            await page.goto(INDEX_URL, wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(2000)
            # 检查是否有【用户登录】按钮，如有则点击
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
            await page.wait_for_timeout(2000)
            page_num = await _get_active_page_number(page)
            print(f"[INFO] 当前页码：{page_num}")
            if not page_num:
                print("[WARN] 未能读取页码，可能页面未完全加载，等待后重试")
                await page.wait_for_timeout(3000)
                page_num = await _get_active_page_number(page)
                print(f"[INFO] 重试后页码：{page_num}")

            if start_page is not None and start_page > 0:
                target_page_text = str(start_page)
                if page_num and page_num != target_page_text:
                    print(f"[INFO] 从指定页码开始扫描：跳转到第 {target_page_text} 页")
                    await _goto_page_number(page, target_page_text)
                    await page.wait_for_timeout(800)
                    page_num = await _get_active_page_number(page)
                    print(f"[INFO] 跳转后当前页码：{page_num}")
        except Exception as exc:  # pylint: disable=broad-except
            print(f"[WARN] 目标页操作失败：{exc}")

        # 遍历分页
        while True:
            current_page_text = await _get_active_page_number(page)
            print(f"[INFO] ========== 开始处理第 {current_page_text} 页 ==========")
            await page.wait_for_selector(VIDEO_CARD_SELECTOR, timeout=8000)
            # 用 JS 一次性获取所有未学习卡片的索引
            unlearned_indices = await page.evaluate(
                """([cardSel, stateSel]) => {
                    const cards = Array.from(document.querySelectorAll(cardSel));
                    const result = [];
                    cards.forEach((c, idx) => {
                        const s = c.querySelector(stateSel);
                        const t = s ? (s.innerText || '').trim() : '';
                        if (t === '未学习') result.push(idx);
                    });
                    return result;
                }""",
                [VIDEO_CARD_SELECTOR, STATE_SELECTOR],
            )
            print(f"[INFO] 当前页未学习卡片索引：{unlearned_indices}")
            processed_count = 0
            for idx in unlearned_indices:
                # 每次点击前重新确认在列表页
                if "commendIndex" not in page.url:
                    await _recover_to_commend(page, current_page_text)
                    await page.wait_for_selector(VIDEO_CARD_SELECTOR, timeout=8000)
                print(f"[INFO] 点击第 {idx+1} 个未学习卡片")
                expected_page_text = current_page_text
                # JS 直接点击以避免可见性/覆盖问题
                pages_before = list(page.context.pages)
                try:
                    click_ok = await page.evaluate(
                        """([sel, n]) => {
                            const els = document.querySelectorAll(sel);
                            const el = els[n];
                            if (!el) return false;
                            el.scrollIntoView({behavior:'instant', block:'center'});
                            const img = el.querySelector('img');
                            (img || el).click();
                            return true;
                        }""",
                        [VIDEO_CARD_SELECTOR, idx],
                    )
                    if not click_ok:
                        print(f"[WARN] 卡片 {idx+1} 未找到，跳过")
                        continue
                except Exception:
                    print(f"[WARN] 卡片 {idx+1} 点击失败，跳过")
                    continue
                await page.wait_for_timeout(1000)
                pages_after = list(page.context.pages)
                new_pages = [pg for pg in pages_after if pg not in pages_before]
                target_page = new_pages[-1] if new_pages else page

                if target_page is page:
                    try:
                        await page.wait_for_function(
                            "() => location.href.includes('commend/coursedetail') || location.href.includes('/index') || location.href.includes('commendIndex')",
                            timeout=5000,
                        )
                    except Exception:
                        pass
                    if "coursedetail" not in page.url:
                        await _recover_to_commend(page, expected_page_text)
                        await page.wait_for_selector(VIDEO_CARD_SELECTOR, timeout=8000)
                        continue
                else:
                    try:
                        await target_page.bring_to_front()
                    except Exception:
                        pass
                    await target_page.wait_for_timeout(500)
                    if "coursedetail" not in target_page.url:
                        try:
                            await target_page.close()
                        except Exception:
                            pass
                        await _recover_to_commend(page, expected_page_text)
                        await page.wait_for_selector(VIDEO_CARD_SELECTOR, timeout=8000)
                        continue

                detail_url = target_page.url
                text = await _wait_detail_yes_no(target_page)
                if text:
                    print(f"[INFO] 详情页文本：{text}，URL：{detail_url}")
                    if text == "否":
                        await _append_url(detail_url)
                        print(f"[INFO] 已记录：{detail_url}")
                    elif text == "是":
                        print('[INFO] 详情页为"是"，直接返回')
                else:
                    print(f"[WARN] 详情页未读取到是/否，URL：{detail_url}")
                processed_count += 1
                # 返回上一页或关闭新标签
                if target_page is page:
                    try:
                        await page.go_back(wait_until="networkidle", timeout=15000)
                    except Exception:
                        pass
                    await page.wait_for_timeout(800)
                    await _recover_to_commend(page, expected_page_text)
                    try:
                        await page.wait_for_selector(VIDEO_CARD_SELECTOR, timeout=8000)
                    except Exception:
                        print("[WARN] 返回后未找到卡片，尝试恢复到列表页")
                        await _recover_to_commend(page, expected_page_text)
                        await page.wait_for_timeout(1000)
                else:
                    try:
                        await target_page.close()
                    except Exception:
                        pass
                    await page.bring_to_front()
                    await _recover_to_commend(page, expected_page_text)
                    try:
                        await page.wait_for_selector(VIDEO_CARD_SELECTOR, timeout=8000)
                    except Exception:
                        print("[WARN] 关闭标签后未找到卡片，尝试恢复到列表页")
                        await _recover_to_commend(page, expected_page_text)
                        await page.wait_for_timeout(1000)
            print(f"[INFO] 本页处理完成，共处理 {processed_count} 个未学习卡片")

            # 尝试下一页
            try:
                target_text = await _get_next_page_target(current_page_text)
                if not target_text:
                    print("[INFO] 没有下一页目标，结束遍历")
                    break
                print(f"[INFO] 尝试跳转到第 {target_text} 页")
                numbers = await page.query_selector_all(".number")
                target_btn = None
                for n in numbers:
                    txt = (await n.inner_text()).strip()
                    if txt == target_text:
                        target_btn = n
                        break
                if not target_btn:
                    quick = await page.query_selector(".btn-quicknext")
                    if quick:
                        print("[INFO] 点击 btn-quicknext 展开更多页码")
                        await quick.click()
                        await page.wait_for_timeout(800)
                        numbers = await page.query_selector_all(".number")
                        for n in numbers:
                            txt = (await n.inner_text()).strip()
                            if txt == target_text:
                                target_btn = n
                                break
                if not target_btn:
                    print(f"[WARN] 未找到第 {target_text} 页按钮，结束遍历")
                    break
                classes = (await target_btn.get_attribute("class")) or ""
                disabled = "disabled" in classes or "is-disabled" in classes
                if disabled:
                    print(f"[INFO] 第 {target_text} 页按钮已禁用，结束遍历")
                    break
                await target_btn.click()
                await page.wait_for_timeout(1500)
                await page.wait_for_selector(VIDEO_CARD_SELECTOR, timeout=8000)
                # 验证是否真的翻到了目标页
                new_page_text = await _get_active_page_number(page)
                if new_page_text != target_text:
                    print(f"[WARN] 翻页后页码为 {new_page_text}，期望 {target_text}，尝试恢复")
                    await _goto_page_number(page, target_text)
                    await page.wait_for_timeout(800)
            except Exception as exc:
                print(f"[WARN] 翻页失败：{exc}")
                break

        if keep_open:
            print("[INFO] 浏览器已打开。按 Ctrl+C 退出并关闭浏览器。")
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
    parser = argparse.ArgumentParser(description="使用 Playwright 执行登录并保存截图（始终可视化模式）")
    parser.add_argument("--username", default=None, help="登录用户名")
    parser.add_argument("--password", default=None, help="登录密码")
    parser.add_argument("--open-only", action="store_true", help="仅打开登录页，不自动填写/提交")
    parser.add_argument(
        "--close-after", action="store_true", help="登录完成后自动关闭浏览器（默认保持打开，按 Ctrl+C 退出）"
    )
    parser.add_argument(
        "--skip-login", action="store_true", help="已手动登录时使用，跳过登录流程，直接执行后续跳转与点击"
    )
    parser.add_argument("--start-page", type=int, default=None, help="从指定页码开始扫描（例如 23）")
    return parser.parse_args(argv)


def login_flow(
    username: str,
    password: str,
    open_only: bool,
    keep_open: bool,
    skip_login: bool,
    start_page: int | None,
) -> None:
    asyncio.run(
        perform_login(
            username,
            password,
            open_only=open_only,
            keep_open=keep_open,
            skip_login=skip_login,
            start_page=start_page,
        )
    )


def main(argv: list[str] | None = None) -> None:
    load_local_secrets()
    args = parse_args(argv)
    open_only = bool(args.open_only)
    keep_open = (not bool(args.close_after)) or open_only
    skip_login = bool(args.skip_login)
    start_page = args.start_page

    username = args.username or os.getenv("DT_CRAWLER_USERNAME") or ""
    password = args.password or os.getenv("DT_CRAWLER_PASSWORD") or ""
    if not open_only and not skip_login:
        if not username or not password:
            raise SystemExit(
                "缺少登录信息：请通过参数 --username/--password，或环境变量 DT_CRAWLER_USERNAME/DT_CRAWLER_PASSWORD，"
                "或在项目根目录创建 secrets.local.env 提供"
            )
    login_flow(
        username,
        password,
        open_only=open_only,
        keep_open=keep_open,
        skip_login=skip_login,
        start_page=start_page,
    )


if __name__ == "__main__":
    main()
