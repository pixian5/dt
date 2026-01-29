# -*- coding: utf-8 -*-
import io
import re
from datetime import datetime
import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING
os.environ.setdefault("PADDLE_DISABLE_ONEDNN", "1")
os.environ.setdefault("FLAGS_enable_mkldnn", "0")
os.environ.setdefault("FLAGS_use_mkldnn", "0")
os.environ.setdefault("FLAGS_enable_onednn", "0")
os.environ.setdefault("FLAGS_enable_pir_api", "0")
os.environ.setdefault("FLAGS_use_pir_api", "0")
os.environ.setdefault("FLAGS_use_new_executor", "0")
os.environ.setdefault("FLAGS_enable_new_executor", "0")
os.environ.setdefault("PADDLE_ENABLE_PIR", "0")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="ignore")
except Exception:
    pass
def _safe_print(msg: str) -> None:
    try:
        print(msg)
    except OSError:
        pass
from playwright.async_api import TimeoutError as PlaywrightTimeoutError, async_playwright, Page
try:
    import cv2
    import numpy as np
    from paddleocr import PaddleOCR
    from PIL import Image, ImageFilter, ImageOps
except Exception:
    cv2 = None
    np = None
    PaddleOCR = None
    Image = None
    ImageFilter = None
    ImageOps = None
if TYPE_CHECKING:
    from PIL import Image as PILImage
    ImageType = PILImage.Image
else:
    ImageType = object
LOGIN_URL = "https://sso.dtdjzx.gov.cn/sso/login"
MEMBER_URL = "https://www.dtdjzx.gov.cn/member/"
PERSONAL_CENTER_URL = "https://gbwlxy.dtdjzx.gov.cn/content#/personalCenter"
PW_TIMEOUT_MS = 4000
DEFAULT_STATE_FILE = Path("storage_state.json")
async def connect_chrome_over_cdp(p, endpoint: str):
    try:
        browser = await p.chromium.connect_over_cdp(endpoint)
        print(f"[INFO] 已连接本地 Chrome（CDP）：{endpoint}")
        return browser
    except Exception as exc:
        local_53333 = endpoint in {"http://127.0.0.1:53333", "http://localhost:53333"}
        if local_53333:
            open_extensions = False
            if os.getenv("CHROME_CDP_USER_DATA_DIR"):
                user_data_dir = os.getenv("CHROME_CDP_USER_DATA_DIR")
                user_data_dir = os.path.expanduser(os.path.expandvars(user_data_dir or ""))
                print(f"[INFO] 已设置 CHROME_CDP_USER_DATA_DIR：{user_data_dir}")
            else:
                src_profile = _default_chrome_user_data_dir()
                dest_profile = _default_cdp_user_data_dir()
                try:
                    empty_before = not Path(dest_profile).exists() or not any(Path(dest_profile).iterdir())
                    user_data_dir = _ensure_cdp_profile_dir(src_profile, dest_profile)
                    open_extensions = empty_before
                    print(f"[INFO] 已准备 CDP 用户数据目录：{user_data_dir}")
                except Exception as copy_exc:
                    print(f"[WARN] 准备 CDP 用户数据目录失败：{copy_exc}，使用临时目录")
                    user_data_dir = tempfile.mkdtemp(prefix="chrome-cdp-53333-")
            try:
                _launch_chrome_with_cdp(user_data_dir)
            except Exception:
                pass
            for _ in range(20):
                await asyncio.sleep(0.5)
                try:
                    browser = await p.chromium.connect_over_cdp(endpoint)
                    print(f"[INFO] 已连接本地 Chrome（CDP）：{endpoint}")
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
            f"无法连接到本地 Chrome CDP 端点：{endpoint}\n"
            f"{_platform_launch_hint()}\n"
            "可设置 PLAYWRIGHT_CDP_ENDPOINT 进行覆盖。"
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
        raise FileNotFoundError(f"Chrome user data dir not found: {src}")
    dest = Path(os.path.expanduser(dest_dir))
    dest.mkdir(parents=True, exist_ok=True)
    if any(dest.iterdir()):
        return str(dest)
    # Copy only extension-related data to avoid bringing login cache
    # Root: Extensions + flags
    root_ext = src / "Extensions"
    if root_ext.exists():
        shutil.copytree(root_ext, dest / "Extensions", dirs_exist_ok=True)
    root_local_state = src / "Local State"
    if root_local_state.exists():
        shutil.copy2(root_local_state, dest / "Local State")
    # Profile dirs: extension config/rules only
    allow_dirs = {"Default"} | {p.name for p in src.glob("Profile *") if p.is_dir()}
    for name in allow_dirs:
        src_path = src / name
        if not src_path.exists():
            continue
        dest_path = dest / name
        # Copy only extension-related files inside profile
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
    user_data_dir = os.path.expanduser(os.path.expandvars(user_data_dir or ""))
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
        return "macOS example:\nopen -na \"Google Chrome\" --args --remote-debugging-port=53333 --user-data-dir=\"$HOME/chrome-cdp-53333\""
    if os.name == "nt":
        return "Windows example:\n\"%LOCALAPPDATA%\\Google\\Chrome\\Application\\chrome.exe\" --remote-debugging-port=53333 --user-data-dir=\"%LOCALAPPDATA%\\chrome-cdp-53333\""
    return "Linux example:\ngoogle-chrome --remote-debugging-port=53333 --user-data-dir=\"$HOME/.config/chrome-cdp-53333\""
async def call_with_timeout_retry(func, action: str, /, *args, **kwargs):
    timeout = kwargs.get("timeout")
    if timeout is None:
        kwargs["timeout"] = PW_TIMEOUT_MS
    else:
        kwargs["timeout"] = min(int(timeout), PW_TIMEOUT_MS)
    try:
        return await func(*args, **kwargs)
    except PlaywrightTimeoutError:
        print(f"[WARN] {action} 超时（{PW_TIMEOUT_MS}ms），重试一次")
        try:
            return await func(*args, **kwargs)
        except PlaywrightTimeoutError as exc:
            raise SystemExit(f"{action} timed out after {PW_TIMEOUT_MS}ms; retry failed: {exc}") from exc
async def _get_user_login_reference_text(page: Page) -> str:
    try:
        el = await call_with_timeout_retry(
            page.wait_for_selector,
            "??????",
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
                await call_with_timeout_retry(p.goto, "加载登录态 origin", origin, wait_until="domcontentloaded")
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
    print(f"[INFO] 程序每 {interval_seconds}s 检查是否已登录")
    log_every = max(1, int(10 // interval_seconds))
    i = 0
    while True:
        await page.wait_for_timeout(interval_seconds * 1000)
        if page.url.startswith(MEMBER_URL) or "www.dtdjzx.gov.cn/member" in page.url:
            print(f"[INFO] 检测到跳转 member（{page.url}），登录成功")
            return
        i += 1
        if i % log_every == 0:
            elapsed = i * interval_seconds
            print(f"[INFO] 仍未登录（{elapsed}s），当前URL={page.url!r}")
def _is_logged_in_by_url(page: Page) -> bool:
    return page.url.startswith(MEMBER_URL) or "www.dtdjzx.gov.cn/member" in page.url
_paddle_ocr = None
_ocr_warmed = False
def _ensure_ocr_ready() -> None:
    global _paddle_ocr
    global _ocr_warmed
    if PaddleOCR is None or Image is None or ImageFilter is None or ImageOps is None or np is None:
        raise SystemExit("缺少 OCR 依赖，请安装 paddleocr/paddlepaddle/pillow/numpy")
    try:
        import paddle
        if hasattr(paddle, "set_flags"):
            paddle.set_flags(
                {
                    "FLAGS_enable_mkldnn": False,
                    "FLAGS_enable_onednn": False,
                    "FLAGS_use_new_executor": False,
                    "FLAGS_enable_pir_api": False,
                    "FLAGS_use_pir_api": False,
                }
            )
    except Exception:
        pass
    if _paddle_ocr is None:
        try:
            import inspect
            params = set(inspect.signature(PaddleOCR).parameters.keys())
        except Exception:
            params = set()
        kwargs = {"lang": "en"}
        if "det" in params:
            kwargs["det"] = False
        if "cls" in params:
            kwargs["cls"] = False
        if "use_angle_cls" in params and "cls" not in params:
            kwargs["use_angle_cls"] = False
        if "use_textline_orientation" in params:
            kwargs["use_textline_orientation"] = False
        try:
            _paddle_ocr = PaddleOCR(**kwargs)
            try:
                ver = getattr(PaddleOCR, "__version__", None) or os.getenv("PADDLEOCR_VERSION")
            except Exception:
                ver = None
            print(f"[INFO] PaddleOCR init params: {kwargs}" + (f", version={ver}" if ver else ""))
        except Exception:
            _paddle_ocr = PaddleOCR(lang="en")
    if _paddle_ocr is not None and not _ocr_warmed:
        try:
            # Warm up once to avoid first-call empty OCR results.
            warm = Image.new("L", (10, 10), 255)
            _run_ocr_on_array(np.array(warm))
        except Exception:
            pass
        _ocr_warmed = True
def _normalize_captcha(text: str, expected_len: int = 4) -> str:
    raw = re.sub(r"[^A-Z0-9]", "", (text or "").upper())
    if expected_len <= 0:
        return raw
    if len(raw) == expected_len:
        return raw
    if len(raw) > expected_len:
        # Prefer the first exact-length chunk (OCR often adds trailing noise)
        return raw[:expected_len]
    return raw
def _add_variant(variants: list[tuple[str, "ImageType"]], tag: str, img: "ImageType") -> None:
    variants.append((tag, img))
def _safe_filename(text: str) -> str:
    safe = re.sub(r"[\\\\/:*?\"<>|]", "_", text or "")
    safe = safe.strip().strip(".")
    return safe or "captcha"

def _variant_display_name(tag: str) -> str:
    mapping = {
        "raw": "原图",
        "raw_sharp": "原图_锐化",
        "raw_inv": "原图_反相",
        "otsu": "大津二值化",
        "adaptive": "自适应二值化",
        "otsu_dilate": "大津二值化_膨胀",
        "otsu_erode": "大津二值化_腐蚀",
        "norm": "归一化",
        "adaptive_noline": "自适应二值化 + 横线去除",
        "adaptive_noline_inv": "自适应二值化 + 横线去除_反相",
        "color_diff": "颜色差分",
        "color_diff_otsu": "颜色差分 + 大津二值化",
        "color_diff_adaptive": "颜色差分 + 自适应二值化",
        "adaptive_blur": "自适应二值化 + 模糊",
        "adaptive_blur_noline": "自适应二值化 + 横竖线形态学去线",
        "adaptive_blur_noline_inv": "自适应二值化 + 横竖线形态学去线_反相",
        "adaptive_blur_noline_close": "自适应二值化 + 横竖线形态学去线_闭运算",
    }
    if tag.startswith("bin_"):
        return f"固定阈值二值化_{tag.split('_', 1)[1]}"
    if tag.startswith("raw_x"):
        return f"原图_放大{tag.split('x', 1)[1]}倍"
    return mapping.get(tag, tag)

def _save_variant_image(img_bytes: bytes, tag: str, debug_dir: Path, ts: str) -> Path | None:
    try:
        variants = _build_captcha_variants(img_bytes)
        for v_tag, img in variants:
            if v_tag == tag:
                name = _variant_display_name(v_tag)
                out_path = debug_dir / f"{ts}_{_safe_filename(name)}.png"
                img.save(out_path)
                return out_path
    except Exception:
        return None
    return None

def _run_ocr_on_array(arr):
    try:
        import inspect
        ocr_params = set(inspect.signature(_paddle_ocr.ocr).parameters.keys())
    except Exception:
        ocr_params = set()
    if ocr_params:
        kwargs = {}
        if "det" in ocr_params:
            kwargs["det"] = False
        if "cls" in ocr_params:
            kwargs["cls"] = False
        if "rec" in ocr_params:
            kwargs["rec"] = True
        return _paddle_ocr.ocr(arr, **kwargs)
    return _paddle_ocr.ocr(arr)

def _extract_ocr_candidates(result) -> list[tuple[str, float]]:
    candidates: list[tuple[str, float]] = []
    if not result:
        return candidates
    if isinstance(result, dict):
        texts = result.get("rec_texts") or []
        scores = result.get("rec_scores") or []
        if result.get("rec_text"):
            texts = [result.get("rec_text")]
            scores = [result.get("rec_score", 0.0)]
        for i, t in enumerate(texts):
            s = float(scores[i]) if i < len(scores) else 0.0
            candidates.append((str(t), s))
    elif isinstance(result, (list, tuple)):
        for item in result:
            if isinstance(item, dict):
                texts = item.get("rec_texts") or []
                scores = item.get("rec_scores") or []
                if item.get("rec_text"):
                    texts = [item.get("rec_text")]
                    scores = [item.get("rec_score", 0.0)]
                for i, t in enumerate(texts):
                    s = float(scores[i]) if i < len(scores) else 0.0
                    candidates.append((str(t), s))
            elif isinstance(item, (list, tuple)):
                if len(item) >= 2 and isinstance(item[1], (list, tuple)) and item[1]:
                    candidates.append((str(item[1][0]), float(item[1][1]) if len(item[1]) > 1 else 0.0))
                elif len(item) >= 1 and isinstance(item[0], (list, tuple)) and item[0]:
                    candidates.append((str(item[0][0]), float(item[0][1]) if len(item[0]) > 1 else 0.0))
                elif len(item) >= 1 and isinstance(item[0], str):
                    candidates.append((str(item[0]), 0.0))
    return candidates

def _pick_best_candidate(candidates: list[tuple[str, float]]) -> str:
    if not candidates:
        return ""
    normed = [(_normalize_captcha(t, expected_len=4), s) for t, s in candidates]
    exact = [c for c in normed if len(c[0]) == 4]
    if exact:
        exact.sort(key=lambda x: x[1], reverse=True)
        return exact[0][0]
    normed.sort(key=lambda x: (len(x[0]), x[1]), reverse=True)
    return normed[0][0] if normed else ""

def _build_captcha_variants(img_bytes: bytes) -> list[tuple[str, "ImageType"]]:
    raw_rgb = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    raw = ImageOps.autocontrast(raw_rgb.convert("L"))
    variants: list[tuple[str, "ImageType"]] = []
    _add_variant(variants, "raw", raw)
    _add_variant(variants, "raw_sharp", raw.filter(ImageFilter.SHARPEN))
    for thr in (160, 180, 200, 220):
        bin_img = raw.point(lambda x, t=thr: 0 if x < t else 255)
        bin_img = bin_img.filter(ImageFilter.MedianFilter(size=3))
        _add_variant(variants, f"bin_{thr}", bin_img)
    if cv2 is not None and np is not None:
        try:
            arr = np.array(raw, dtype=np.uint8)
            _, otsu = cv2.threshold(arr, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            _add_variant(variants, "otsu", Image.fromarray(otsu))
            adap = cv2.adaptiveThreshold(arr, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, 2)
            _add_variant(variants, "adaptive", Image.fromarray(adap))
            kernel = np.ones((2, 2), np.uint8)
            dil = cv2.dilate(otsu, kernel, iterations=1)
            ero = cv2.erode(otsu, kernel, iterations=1)
            _add_variant(variants, "otsu_dilate", Image.fromarray(dil))
            _add_variant(variants, "otsu_erode", Image.fromarray(ero))
            norm = cv2.normalize(arr, None, 0, 255, cv2.NORM_MINMAX)
            _add_variant(variants, "norm", Image.fromarray(norm))
            try:
                line_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (20, 1))
                lines = cv2.morphologyEx(adap, cv2.MORPH_OPEN, line_kernel)
                no_line = cv2.subtract(adap, lines)
                _add_variant(variants, "adaptive_noline", Image.fromarray(no_line))
            except Exception:
                pass
            try:
                arr_rgb = np.array(raw_rgb, dtype=np.uint8)
                maxc = arr_rgb.max(axis=2)
                minc = arr_rgb.min(axis=2)
                diff = cv2.normalize((maxc - minc), None, 0, 255, cv2.NORM_MINMAX)
                _add_variant(variants, "color_diff", Image.fromarray(diff))
                _, diff_otsu = cv2.threshold(diff, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
                _add_variant(variants, "color_diff_otsu", Image.fromarray(diff_otsu))
                diff_adap = cv2.adaptiveThreshold(diff, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, 2)
                _add_variant(variants, "color_diff_adaptive", Image.fromarray(diff_adap))
            except Exception:
                pass
            try:
                blur = cv2.GaussianBlur(arr, (3, 3), 0)
                adap_blur = cv2.adaptiveThreshold(blur, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, 2)
                _add_variant(variants, "adaptive_blur", Image.fromarray(adap_blur))
                h_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (18, 1))
                v_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, 3))
                h_lines = cv2.morphologyEx(adap_blur, cv2.MORPH_OPEN, h_kernel)
                v_lines = cv2.morphologyEx(adap_blur, cv2.MORPH_OPEN, v_kernel)
                lines = cv2.bitwise_or(h_lines, v_lines)
                no_lines = cv2.subtract(adap_blur, lines)
                _add_variant(variants, "adaptive_blur_noline", Image.fromarray(no_lines))
                close = cv2.morphologyEx(no_lines, cv2.MORPH_CLOSE, np.ones((2, 2), np.uint8))
                _add_variant(variants, "adaptive_blur_noline_close", Image.fromarray(close))
            except Exception:
                pass
        except Exception:
            pass
    return variants
def _ocr_captcha_bytes(img_bytes: bytes, *, debug: bool = False, debug_dir: Path | None = None) -> str:
    _ensure_ocr_ready()
    variants = _build_captcha_variants(img_bytes)
    candidates: list[tuple[str, float]] = []
    try:
        preferred = None
        for tag, img in variants:
            if tag == "otsu_dilate":
                preferred = (tag, img)
                break
        if preferred is not None:
            tag, img = preferred
            if debug and debug_dir is not None:
                try:
                    debug_dir.mkdir(parents=True, exist_ok=True)
                    name = _variant_display_name(tag)
                    img.save(debug_dir / f"{_safe_filename(name)}.png")
                except Exception:
                    pass
            arr = np.array(img.convert("RGB"), dtype=np.uint8)
            result = _run_ocr_on_array(arr)
            candidates.extend(_extract_ocr_candidates(result))
        else:
            for tag, img in variants:
                if debug and debug_dir is not None:
                    try:
                        debug_dir.mkdir(parents=True, exist_ok=True)
                        name = _variant_display_name(tag)
                        img.save(debug_dir / f"{_safe_filename(name)}.png")
                    except Exception:
                        pass
                arr = np.array(img.convert("RGB"), dtype=np.uint8)
                result = _run_ocr_on_array(arr)
                candidates.extend(_extract_ocr_candidates(result))
    except Exception:
        return ""
    text = _pick_best_candidate(candidates)
    return _normalize_captcha(text, expected_len=4)

def _ocr_debug_variants(img_bytes: bytes, debug_dir: Path) -> list[tuple[str, str]]:
    _ensure_ocr_ready()
    variants = _build_captcha_variants(img_bytes)
    results: list[tuple[str, str]] = []
    for tag, img in variants:
        if debug_dir is not None:
            try:
                debug_dir.mkdir(parents=True, exist_ok=True)
                name = _variant_display_name(tag)
                img.save(debug_dir / f"{_safe_filename(name)}.png")
            except Exception:
                pass
        arr = np.array(img.convert("RGB"), dtype=np.uint8)
        result = _run_ocr_on_array(arr)
        text = _pick_best_candidate(_extract_ocr_candidates(result))
        results.append((_variant_display_name(tag), _normalize_captcha(text, expected_len=4)))
    return results
def _mark_captcha_image(src: Path | None, status: str, code: str | None = None) -> None:
    if not src or not src.exists():
        return
    try:
        base = src.stem
        if code:
            name = f"{base}_{code}"
        else:
            name = f"{base}_{status}"
        name = _safe_filename(name) + src.suffix
        dest = src.with_name(name)
        if dest.exists():
            dest = src.with_name(_safe_filename(name + "_1") + src.suffix)
        src.replace(dest)
    except Exception:
        return
async def _solve_captcha_text(page: Page) -> tuple[str, Path | None]:
    img = page.locator("#yanzhengma").first
    try:
        await img.wait_for(state="visible", timeout=PW_TIMEOUT_MS)
    except Exception:
        pass
    try:
        await page.wait_for_function(
            "(el) => el && el.complete && el.naturalWidth > 0",
            img,
            timeout=PW_TIMEOUT_MS,
        )
    except Exception:
        pass
    img_bytes = await img.screenshot(type="png")
    if len(img_bytes) < 200:
        await page.wait_for_timeout(500)
        img_bytes = await img.screenshot(type="png")
    img_path = None
    debug_dir = None
    try:
        base_dir = Path(__file__).resolve().parent
        debug_dir = base_dir / "captcha_debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%H%M%S")
        img_path = debug_dir / f"{ts}_原图.png"
        img_path.write_bytes(img_bytes)
        print(f"[INFO] 已保存验证码截图: {img_path}")
        processed_path = _save_variant_image(img_bytes, "otsu_dilate", debug_dir, ts)
        if processed_path:
            img_path = processed_path
    except Exception as exc:
        img_path = None
        debug_dir = None
        print(f"[WARN] 保存验证码截图失败: {exc}")
    code = _ocr_captcha_bytes(img_bytes, debug=False, debug_dir=debug_dir)
    if len(code) != 4:
        return code, img_path
    return code, img_path
async def _has_captcha_error(page: Page) -> bool:
    loc = page.locator("#validateCodeMessage").first
    if await loc.count() == 0:
        return False
    text = ((await loc.inner_text()) or "").strip()
    return "验证码错误" in text
async def _submit_login_form(page: Page) -> bool:
    submitted = False
    try:
        btn = page.locator('xpath=//*[@id="loginForm"]/div[4]/a[1]').first
        if await btn.count() != 0:
            await btn.click(force=True, timeout=PW_TIMEOUT_MS)
            return True
    except Exception:
        pass
    try:
        await page.locator("#validateCode").press("Enter", timeout=PW_TIMEOUT_MS)
        submitted = True
    except Exception:
        pass
    return submitted
async def ensure_logged_in(page: Page, username: str, password: str, open_only: bool, skip_login: bool = False) -> None:
    if skip_login:
        return
    print(f"[INFO] 打开登录页：{LOGIN_URL}")
    await call_with_timeout_retry(page.goto, "打开登录页", LOGIN_URL, wait_until="load", timeout=PW_TIMEOUT_MS)
    await page.wait_for_timeout(1000)
    if _is_logged_in_by_url(page):
        print(f"[INFO] 检测到跳转 member（{page.url}），视为已登录")
        return
    try:
        await call_with_timeout_retry(page.wait_for_selector, "等待用户名输入框", "#username", timeout=PW_TIMEOUT_MS)
        await call_with_timeout_retry(page.wait_for_selector, "等待密码输入框", "#password", timeout=PW_TIMEOUT_MS)
        await call_with_timeout_retry(page.wait_for_selector, "等待验证码输入框", "#validateCode", timeout=PW_TIMEOUT_MS)
        await call_with_timeout_retry(page.wait_for_selector, "等待验证码图片", "#yanzhengma", timeout=PW_TIMEOUT_MS)
    except Exception:
        if _is_logged_in_by_url(page):
            print(f"[INFO] 检测到跳转 member（{page.url}），视为已登录")
            return
        raise
    if username:
        await page.fill("#username", username)
    if password:
        await page.fill("#password", password)
    login_attempts = 0
    while True:
        if _is_logged_in_by_url(page):
            print(f"[INFO] 检测到跳转 member（{page.url}），登录成功")
            return
        img_path = None
        try:
            code, img_path = await _solve_captcha_text(page)
        except Exception as exc:
            print(f"[WARN] OCR 失败，转人工输入：{exc}")
            _mark_captcha_image(img_path, "识别失败", None)
            code = ""
        safe_code = str(code)
        if len(code) != 4:
            safe_code = re.sub(r"[^A-Z0-9]", "?", safe_code)
            _safe_print(f"[WARN] 识别验证码是{safe_code!r}，不是4位，点击验证码图片刷新后重试")
            _mark_captcha_image(img_path, "长度不对", safe_code)
            try:
                await page.locator("#yanzhengma").click()
            except Exception:
                pass
            await page.wait_for_timeout(800)
            continue
        login_attempts += 1
        _safe_print(f"[INFO] 识别验证码是{code}，第{login_attempts}次尝试登录")
        await page.fill("#validateCode", code)
        await _submit_login_form(page)
        await page.wait_for_timeout(1500)
        if _is_logged_in_by_url(page):
            _mark_captcha_image(img_path, "成功", code)
            print(f"[INFO] 登录成功：{page.url}")
            return
        if await _has_captcha_error(page):
            login_attempts += 1
            safe_code = re.sub(r"[^A-Z0-9]", "?", str(code))
            _safe_print(f"[WARN] 验证码错误，点击验证码图片刷新后重试（{login_attempts}/{max_login_attempts}）：{safe_code!r}")
            _mark_captcha_image(img_path, "验证码错误", safe_code)
            try:
                await page.locator("#yanzhengma").click()
            except Exception:
                pass
            await page.wait_for_timeout(800)
            continue
    print("[WARN] OCR 登录失败 5 次，转人工输入")
    if not sys.stdin.isatty() or os.getenv("DT_ALLOW_MANUAL", "1") != "1":
        print("[WARN] 当前为非交互模式或已禁用人工输入，跳过人工输入")
        return
    for attempt in range(1, 4):
        captcha = (await asyncio.to_thread(input, "请手动输入验证码：")).strip()
        if not captcha:
            print("[WARN] 验证码为空，重新输入")
            continue
        await page.fill("#validateCode", captcha)
        await _submit_login_form(page)
        await page.wait_for_timeout(1500)
        if _is_logged_in_by_url(page):
            print(f"[INFO] 登录成功：{page.url}")
            return
        if await _has_captcha_error(page):
            print(f"[WARN] 验证码错误，重试（{attempt}/3）")
            try:
                await page.locator("#yanzhengma").click()
            except Exception:
                pass
            await page.wait_for_timeout(800)
            continue
    print("[WARN] 验证码多次错误，持续检测是否已登录")
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
                print(f"[INFO] 已加载登录态：{state_file}")
        page = await context.new_page()
        open_only_effective = False
        if loaded_state:
            state_valid = False
            try:
                print("[INFO] 校验登录态（打开个人中心）")
                await page.goto(PERSONAL_CENTER_URL, wait_until="domcontentloaded", timeout=PW_TIMEOUT_MS)
                await page.wait_for_timeout(1000)
                state_valid = _is_logged_in_by_url(page) or "personalCenter" in (page.url or "")
                if state_valid:
                    print(f"[INFO] 登录态有效（{page.url}）")
                else:
                    print(f"[WARN] 登录态无效（当前URL={page.url!r}），将重新登录")
            except Exception as exc:
                print(f"[WARN] 登录态校验失败（{exc}），将重新登录")
            if not state_valid:
                try:
                    state_file.unlink()
                    print(f"[WARN] 已删除无效登录态文件：{state_file}")
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
    parser = argparse.ArgumentParser(description="Playwright 登录辅助（可视模式）")
    parser.add_argument("--username", default=None, help="登录用户名")
    parser.add_argument("--password", default=None, help="登录密码")
    parser.add_argument("--close-after", action="store_true", help="登录后关闭浏览器")
    parser.add_argument("--skip-login", action="store_true", help="已登录时跳过登录流程")
    parser.add_argument("--state-file", default=str(DEFAULT_STATE_FILE), help="登录态文件")
    parser.add_argument("--no-load-state", action="store_true", help="不加载登录态")
    parser.add_argument("--no-save-state", action="store_true", help="不保存登录态")
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
            print(
                "[WARN] 缺少登录信息：请提供 --username/--password 或环境变量"
                "Provide DT_CRAWLER_USERNAME/DT_CRAWLER_PASSWORD env vars or create secrets.local.env"
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
