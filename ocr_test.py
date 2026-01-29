# -*- coding: utf-8 -*-
import base64
import os
import sys
from pathlib import Path

import requests

API_URL = "https://efr2p4g4ofd0s7p8.aistudio-app.com/ocr"
TOKEN = os.getenv("PADDLE_OCR_API_TOKEN", "01a29efb53b60e1ee6fb3549a59f09634e4c8ada")


def main() -> int:
    path = Path(r"C:\Users\Administrator\Desktop\dt\captcha_debug")
    out_dir = Path(r"C:\Users\Administrator\Desktop\dt\output")
    if len(sys.argv) > 1:
        path = Path(sys.argv[1])
    if len(sys.argv) > 2:
        out_dir = Path(sys.argv[2])
    if not path.exists():
        print(f"[ERROR] path not found: {path}")
        return 2
    out_dir.mkdir(parents=True, exist_ok=True)
    if path.is_dir():
        images = sorted([p for p in path.iterdir() if p.suffix.lower() in {".png", ".jpg", ".jpeg"}])
    else:
        images = [path]
    if not images:
        print(f"[ERROR] no images found in: {path}")
        return 4
    headers = {
        "Authorization": f"token {TOKEN}",
        "Content-Type": "application/json",
    }
    print(f"[INFO] OCR scan: {len(images)} image(s)")
    for img in images:
        try:
            file_bytes = img.read_bytes()
            file_data = base64.b64encode(file_bytes).decode("ascii")
            payload = {
                "file": file_data,
                "fileType": 1,
                "useDocOrientationClassify": False,
                "useDocUnwarping": False,
                "useTextlineOrientation": False,
            }
            response = requests.post(API_URL, json=payload, headers=headers, timeout=60)
            if response.status_code != 200:
                print(f"[ERROR] {img.name}: HTTP {response.status_code}")
                continue
            result = response.json().get("result") or {}
        except Exception as exc:
            print(f"[ERROR] {img.name}: OCR failed: {exc}")
            continue
        ocr_results = result.get("ocrResults") or []
        if not ocr_results:
            print(f"[WARN] {img.name}: no text")
            continue
        input_filename = img.stem
        for i, res in enumerate(ocr_results):
            pruned = res.get("prunedResult")
            if pruned:
                print(f"[OK] {img.name}: {pruned}")
            image_url = res.get("ocrImage")
            if image_url:
                img_response = requests.get(image_url, timeout=60)
                if img_response.status_code == 200:
                    filename = out_dir / f"{input_filename}_{i}.jpg"
                    filename.write_bytes(img_response.content)
                    print(f"[INFO] saved: {filename}")
                else:
                    print(f"[WARN] {img.name}: download failed ({img_response.status_code})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
