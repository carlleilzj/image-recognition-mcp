#!/usr/bin/env python3
"""Vision 引擎自测：OCR / 分类 / 综合识别 / data URI 解析。

用法: python scripts/test_engine.py [图片路径]
"""

from __future__ import annotations

import base64
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import vision_engine as ve  # noqa: E402


def main() -> None:
    img = sys.argv[1] if len(sys.argv) > 1 else "sample/test_card.png"
    img = os.path.abspath(img)
    if not os.path.isfile(img):
        print(f"[错误] 测试图片不存在: {img}，请先运行 scripts/make_test_image.py")
        sys.exit(1)

    print(f"== 测试图片: {img} ==")

    print("\n[1] OCR 识别:")
    lines = ve.ocr_image(img)
    for l in lines:
        print(f"    conf={l['confidence']:.3f}  bbox={l['bbox']}  text={l['text']!r}")
    if not lines:
        print("    (未识别到文字)")

    print("\n[2] 图像主体/场景分类:")
    for c in ve.classify_image(img, top_k=5):
        print(f"    {c['label']}  conf={c['confidence']:.3f}")

    print("\n[3] 综合识别(摘要):")
    r = ve.analyze_image(img)
    print("   ", r["summary"].replace("\n", "\n    "))
    print(f"    耗时: {r['elapsed_ms']} ms")

    print("\n[4] data URI 解析:")
    with open(img, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    uri = f"data:image/png;base64,{b64}"
    resolved = ve.resolve_image_source(uri)
    print(f"    data URI -> {resolved} (exists={os.path.isfile(resolved)})")
    assert os.path.isfile(resolved), "data URI 解析失败"

    print("\n[5] 路径校验(错误处理):")
    try:
        ve.resolve_image_source("/no/such/file.png")
    except FileNotFoundError as exc:
        print(f"    FileNotFoundError 正常抛出: {exc}")

    print("\n全部通过 ✅")


if __name__ == "__main__":
    main()
