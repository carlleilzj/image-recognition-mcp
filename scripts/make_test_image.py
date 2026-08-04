#!/usr/bin/env python3
"""生成一张含中文/英文文字和简单图形的测试图片，用于验证识别链路。

用法: python scripts/make_test_image.py [输出路径]（默认 sample/test_card.png）
"""

from __future__ import annotations

import os
import sys

from PIL import Image, ImageDraw, ImageFont

# Arial Unicode 覆盖全部 CJK/拉丁字符；macOS 自带，单文件 ttf 加载最稳定。
FONT_CN = "/System/Library/Fonts/Supplemental/Arial Unicode.ttf"
FONT_EN = "/System/Library/Fonts/Supplemental/Arial.ttf"


def main() -> None:
    # 默认输出到项目内的 sample/ 目录，避免依赖当前工作目录
    default_out = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "sample",
        "test_card.png",
    )
    out = sys.argv[1] if len(sys.argv) > 1 else default_out
    out = os.path.abspath(out)
    os.makedirs(os.path.dirname(out), exist_ok=True)

    img = Image.new("RGB", (1200, 420), "white")
    draw = ImageDraw.Draw(img)

    try:
        font_cn = ImageFont.truetype(FONT_CN, 56)
    except OSError:
        font_cn = ImageFont.load_default()
    try:
        font_en = ImageFont.truetype(FONT_EN, 56)
    except OSError:
        font_en = font_cn

    draw.text((60, 60), "MacBook Air 图片识别测试", fill="black", font=font_cn)
    draw.text((60, 170), "Hello Vision OCR 12345", fill="black", font=font_en)
    draw.text((60, 280), "日期: 2026-08-04 13:30", fill="black", font=font_cn)

    # 简单图形：红色圆形（用于测试图像分类）
    draw.ellipse((1000, 80, 1120, 200), fill="red")

    img.save(out, "PNG")
    print(f"测试图片已生成: {out} ({img.width}x{img.height})")


if __name__ == "__main__":
    main()
