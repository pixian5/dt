import argparse
import asyncio
from datetime import datetime
import os
from pathlib import Path

from playwright.async_api import TimeoutError as PlaywrightTimeoutError, async_playwright, Page

from crawler.login import COMMEND_URL, INDEX_URL, LOGIN_URL, connect_chrome_over_cdp, ensure_logged_in, load_local_secrets


VIDEO_CARD_SELECTOR = ".video-warp-start"
STATE_SELECTOR = ".state-paused"
URL_OUTPUT_FILE = Path("url.txt")

USER_LOGIN_REF_SELECTOR = ".el-popover__reference"

PW_TIMEOUT_MS = 5000


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


async def _get_active_page_number(page: Page) -> str:
    try:
        el = await call_with_timeout_retry(page.wait_for_selector, "获取当前页码", ".number.active", timeout=PW_TIMEOUT_MS)
        return (await el.inner_text()).strip()
    except Exception:
        return ""


async def _append_url(url: str) -> None:
    URL_OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with URL_OUTPUT_FILE.open("a", encoding="utf-8") as f:
        f.write(url + "\n")


async def _get_user_login_reference_text(page: Page) -> str:
    try:
        el = await call_with_timeout_retry(
            page.wait_for_selector,
            "读取用户登录按钮",
            USER_LOGIN_REF_SELECTOR,
            timeout=PW_TIMEOUT_MS,
        )
        return ((await el.inner_text()) or "").strip()
    except Exception:
        return ""


async def _wait_for_cards_selector(page: Page, expected_page_text: str) -> str | None:
    sel = VIDEO_CARD_SELECTOR

    async def _poll() -> str | None:
        for _ in range(10):
            try:
                if await page.locator(sel).count():
                    return sel
            except Exception:
                pass
            await page.wait_for_timeout(500)
        return None

    await _recover_to_commend(page, expected_page_text)
    found = await _poll()
    if found:
        return found

    print("[WARN] 等待列表卡片超时，尝试刷新/重进列表页后重试 1 次")
    try:
        await call_with_timeout_retry(page.reload, "刷新列表页", wait_until="domcontentloaded", timeout=PW_TIMEOUT_MS)
    except Exception:
        try:
            await call_with_timeout_retry(
                page.goto, "重进列表页", COMMEND_URL, wait_until="domcontentloaded", timeout=PW_TIMEOUT_MS
            )
        except Exception:
            return None

    await page.wait_for_timeout(800)
    await _recover_to_commend(page, expected_page_text)
    return await _poll()


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
            await page.wait_for_timeout(900)
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
            await call_with_timeout_retry(
                page.goto, "恢复到列表页", COMMEND_URL, wait_until="domcontentloaded", timeout=PW_TIMEOUT_MS
            )
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
        await call_with_timeout_retry(
            page.wait_for_selector,
            "详情页等待是/否",
            "div.titleContent > span",
            timeout=PW_TIMEOUT_MS,
            state="visible",
        )
        await call_with_timeout_retry(
            page.wait_for_function,
            "详情页等待是/否脚本",
            """() => {
                const el = document.querySelector('div.titleContent > span');
                if (!el) return false;
                const t = (el.innerText || '').trim();
                return t === '是' || t === '否';
            }""",
            timeout=PW_TIMEOUT_MS,
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


def _parse_page_range(page_arg: str | None) -> tuple[int | None, int | None]:
    if not page_arg:
        return None, None
    s = str(page_arg).strip()
    if not s:
        return None, None
    if "-" not in s:
        try:
            start = int(s)
            return (start if start > 0 else None), None
        except Exception:
            return None, None
    parts = [p.strip() for p in s.split("-", 1)]
    if len(parts) != 2:
        return None, None
    try:
        start = int(parts[0])
        end = int(parts[1])
    except Exception:
        return None, None
    if start <= 0 or end <= 0:
        return None, None
    if end < start:
        start, end = end, start
    return start, end


async def perform_scan(
    username: str,
    password: str,
    open_only: bool,
    keep_open: bool,
    skip_login: bool = False,
    page_arg: str | None = None,
) -> None:
    start_page, end_page = _parse_page_range(page_arg)
    async with async_playwright() as p:
        endpoint = os.getenv("PLAYWRIGHT_CDP_ENDPOINT", "http://127.0.0.1:9222")
        browser = await connect_chrome_over_cdp(p, endpoint)

        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        context.set_default_timeout(PW_TIMEOUT_MS)
        page = await context.new_page()

        await ensure_logged_in(page, username=username, password=password, open_only=open_only, skip_login=skip_login)

        await page.wait_for_timeout(1000)
        await call_with_timeout_retry(
            page.goto, "登录后回到列表页", COMMEND_URL, wait_until="domcontentloaded", timeout=PW_TIMEOUT_MS
        )
        ref_text = await _get_user_login_reference_text(page)
        if ref_text == "用户登录":
            try:
                el = await page.query_selector(USER_LOGIN_REF_SELECTOR)
                if el:
                    await el.click()
            except Exception:
                pass
            await page.wait_for_timeout(2000)

        await call_with_timeout_retry(
            page.goto, "二次进入列表页", COMMEND_URL, wait_until="domcontentloaded", timeout=PW_TIMEOUT_MS
        )
        await page.wait_for_timeout(500)
        page_num = await _get_active_page_number(page)
        print(f"[INFO] 当前页码：{page_num}")

        if start_page is not None and start_page > 0:
            target_page_text = str(start_page)
            if page_num and page_num != target_page_text:
                if end_page is not None and end_page > 0:
                    print(f"[INFO] 扫描范围：{start_page}-{end_page}，跳转到第 {target_page_text} 页开始")
                else:
                    print(f"[INFO] 从指定页码开始扫描：跳转到第 {target_page_text} 页")
                await _goto_page_number(page, target_page_text)
                await page.wait_for_timeout(800)
                page_num = await _get_active_page_number(page)
                print(f"[INFO] 跳转后当前页码：{page_num}")

        while True:
            current_page_text = await _get_active_page_number(page)
            print(f"[INFO] ========== 开始处理第 {current_page_text} 页 ==========")
            cards_selector = await _wait_for_cards_selector(page, current_page_text)
            if not cards_selector:
                print(f"[WARN] 第 {current_page_text} 页未加载到卡片，跳过本页")
                if end_page is not None and end_page > 0 and current_page_text == str(end_page):
                    break
                target_text = await _get_next_page_target(current_page_text)
                if not target_text:
                    break
                print(f"[INFO] 尝试跳转到第 {target_text} 页")
                await _goto_page_number(page, target_text)
                await page.wait_for_timeout(800)
                continue

            cards = page.locator(cards_selector)
            card_count = await cards.count()
            unlearned_indices: list[int] = []
            for i in range(card_count):
                card = cards.nth(i)
                try:
                    state_loc = card.locator(STATE_SELECTOR)
                    if await state_loc.count():
                        state_text = ((await state_loc.inner_text()) or "").strip()
                    else:
                        state_text = ""
                except Exception:
                    state_text = ""
                if state_text == "未学习":
                    unlearned_indices.append(i)

            print(f"[INFO] 当前页未学习卡片索引：{unlearned_indices}")
            processed_count = 0
            no_test_url_count = 0
            total_unlearned = len(unlearned_indices)

            for seq, idx in enumerate(unlearned_indices, start=1):
                expected_page_text = current_page_text
                await _recover_to_commend(page, expected_page_text)
                cards_selector = await _wait_for_cards_selector(page, expected_page_text)
                if not cards_selector:
                    print(f"[WARN] 第 {expected_page_text} 页未加载到卡片，终止本页遍历")
                    break
                cards = page.locator(cards_selector)

                print(f"[INFO] 点击第 {seq}/{total_unlearned} 个未学习卡片")
                card = cards.nth(idx)

                pages_before = list(page.context.pages)
                try:
                    await call_with_timeout_retry(
                        card.scroll_into_view_if_needed,
                        "卡片滚动到可见",
                        timeout=PW_TIMEOUT_MS,
                    )
                except Exception:
                    pass

                img = card.locator("img").first
                try:
                    if await img.count():
                        await call_with_timeout_retry(img.click, "点击卡片图片", timeout=PW_TIMEOUT_MS)
                    else:
                        await call_with_timeout_retry(card.click, "点击卡片", timeout=PW_TIMEOUT_MS)
                except Exception as exc:
                    print(f"[WARN] 卡片 {idx+1} 点击失败：{exc}")
                    continue

                await page.wait_for_timeout(800)
                pages_after = list(page.context.pages)
                new_pages = [pg for pg in pages_after if pg not in pages_before]
                target_page = new_pages[-1] if new_pages else page

                if target_page is page:
                    try:
                        await call_with_timeout_retry(
                            page.wait_for_function,
                            "等待详情页跳转",
                            "() => location.href.includes('commend/coursedetail') || location.href.includes('commendIndex')",
                            timeout=PW_TIMEOUT_MS,
                        )
                    except Exception:
                        pass
                    if "coursedetail" not in page.url:
                        await _recover_to_commend(page, expected_page_text)
                        continue
                else:
                    try:
                        await target_page.bring_to_front()
                    except Exception:
                        pass
                    await target_page.wait_for_timeout(300)
                    if "coursedetail" not in target_page.url:
                        try:
                            await target_page.close()
                        except Exception:
                            pass
                        await _recover_to_commend(page, expected_page_text)
                        continue

                detail_url = target_page.url
                text = await _wait_detail_yes_no(target_page)
                if text:
                    print(f"[INFO] 详情页文本：{text}，URL：{detail_url}")
                    if text == "否":
                        await _append_url(detail_url)
                        no_test_url_count += 1
                        print(f"[INFO] 已记录：{detail_url}")
                    elif text == "是":
                        print('[INFO] 详情页为"是"，直接返回')
                else:
                    print(f"[WARN] 详情页未读取到是/否，URL：{detail_url}")

                processed_count += 1

                if target_page is page:
                    try:
                        await call_with_timeout_retry(
                            page.go_back, "返回列表页", wait_until="networkidle", timeout=PW_TIMEOUT_MS
                        )
                    except Exception:
                        pass
                else:
                    try:
                        await target_page.close()
                    except Exception:
                        pass
                    try:
                        await page.bring_to_front()
                    except Exception:
                        pass

                await page.wait_for_timeout(500)
                await _recover_to_commend(page, expected_page_text)

            print(
                f"[INFO] 第【{current_page_text}】页 {processed_count} 个未学习卡片处理完成，无随堂测验url数：{no_test_url_count}"
            )
            ts = datetime.now().strftime("%Y年%m月%d日%H时%M分%S秒")
            await _append_url(
                f"【{current_page_text}】页 {processed_count} 个未学习卡片处理完成，无随堂测验url数：{no_test_url_count} {ts}"
            )

            if end_page is not None and end_page > 0 and current_page_text == str(end_page):
                print(f"[INFO] 已到达末页 {end_page}，停止扫描")
                break

            try:
                target_text = await _get_next_page_target(current_page_text)
                if not target_text:
                    print("[INFO] 没有下一页目标，结束遍历")
                    break
                print(f"[INFO] 尝试跳转到第 {target_text} 页")
                target_btn = None
                while True:
                    numbers = await page.query_selector_all(".number")
                    current_numbers_text: list[str] = []
                    for n in numbers:
                        txt = (await n.inner_text()).strip()
                        current_numbers_text.append(txt)
                        if txt == target_text:
                            target_btn = n
                            break

                    if target_btn:
                        break

                    quick = await page.query_selector(".btn-quicknext")
                    if not quick:
                        print(f"[INFO] 没找到页码{target_text}")
                        break

                    quick_classes = (await quick.get_attribute("class")) or ""
                    quick_disabled = ("disabled" in quick_classes) or ("is-disabled" in quick_classes)
                    if quick_disabled:
                        print(f"[INFO] 没找到页码{target_text}")
                        break

                    print("[INFO] 点击 btn-quicknext 展开更多页码")
                    await quick.click()
                    await page.wait_for_timeout(800)

                    numbers_after = await page.query_selector_all(".number")
                    next_numbers_text: list[str] = []
                    for n in numbers_after:
                        next_numbers_text.append((await n.inner_text()).strip())

                    if next_numbers_text == current_numbers_text:
                        print(f"[INFO] 没找到页码{target_text}")
                        break

                if not target_btn:
                    break
                classes = (await target_btn.get_attribute("class")) or ""
                disabled = "disabled" in classes or "is-disabled" in classes
                if disabled:
                    print(f"[INFO] 第 {target_text} 页按钮已禁用，结束遍历")
                    break
                await target_btn.click()
                await page.wait_for_timeout(1500)
                await call_with_timeout_retry(
                    page.wait_for_selector,
                    "等待列表卡片",
                    VIDEO_CARD_SELECTOR,
                    timeout=PW_TIMEOUT_MS,
                    state="attached",
                )
                new_page_text = await _get_active_page_number(page)
                if new_page_text != target_text:
                    print(f"[WARN] 翻页后页码为 {new_page_text}，期望 {target_text}，尝试恢复")
                    await _goto_page_number(page, target_text)
                    await page.wait_for_timeout(800)
            except Exception as exc:
                print(f"[WARN] 翻页失败：{exc}")
                break

        if keep_open:
            print("[INFO] 扫描完成！浏览器已打开。按 Ctrl+C 退出并关闭浏览器。")
            try:
                while True:
                    await asyncio.sleep(3600)
            except (KeyboardInterrupt, asyncio.CancelledError):
                pass
        else:
            try:
                await page.close()
            except Exception:
                pass


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="获取无随堂测验(否)课程详情链接（始终可视化模式）")
    parser.add_argument("--username", default=None, help="登录用户名")
    parser.add_argument("--password", default=None, help="登录密码")
    parser.add_argument("--open-only", action="store_true", help="仅打开登录页，不自动填写/提交")
    parser.add_argument(
        "--close-after", action="store_true", help="扫描完成后自动关闭浏览器（默认保持打开，按 Ctrl+C 退出）"
    )
    parser.add_argument(
        "--skip-login", action="store_true", help="已手动登录时使用，跳过登录流程，直接执行后续跳转与点击"
    )
    parser.add_argument("--page", type=str, default=None, help='扫描页码："起始页" 或 "起始页-末页"（例如 23 或 23-30）')
    parser.add_argument("--start-page", type=int, default=None, help="（兼容参数，已废弃）等价于 --page 起始页")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    load_local_secrets()
    args = parse_args(argv)
    open_only = bool(args.open_only)
    keep_open = (not bool(args.close_after)) or open_only
    skip_login = bool(args.skip_login)
    page_arg = args.page
    if not page_arg and args.start_page is not None:
        page_arg = str(args.start_page)

    username = args.username or os.getenv("DT_CRAWLER_USERNAME") or ""
    password = args.password or os.getenv("DT_CRAWLER_PASSWORD") or ""
    if not open_only and not skip_login:
        if not username or not password:
            raise SystemExit(
                "缺少登录信息：请通过参数 --username/--password，或环境变量 DT_CRAWLER_USERNAME/DT_CRAWLER_PASSWORD，"
                "或在项目根目录创建 secrets.local.env 提供"
            )

    try:
        asyncio.run(
            perform_scan(
                username=username,
                password=password,
                open_only=open_only,
                keep_open=keep_open,
                skip_login=skip_login,
                page_arg=page_arg,
            )
        )
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
