#!/usr/bin/env python3
"""macOS 本地视觉识别引擎 —— 基于 Apple Vision 框架 (pyobjc)。

提供三大本地能力（全程本机推理、无网络请求、数据不出设备）：
  1. OCR 文字提取          —— VNRecognizeTextRequest（中英混排、手写体）
  2. 图像主体/场景分类      —— VNClassifyImageRequest
  3. 综合识别 / 屏幕截图    —— analyze_image / screen_capture

图片来源统一解析：本地路径 | data URI(base64) | 纯 base64（PNG 魔数校验）。

依赖：pyobjc-framework-Vision, pyobjc-framework-Quartz
"""

from __future__ import annotations

import base64
import os
import re
import subprocess
import tempfile
import time
from typing import Optional

import Quartz
import Vision
from Foundation import NSURL

# ---------------------------------------------------------------------------
# 常量与配置
# ---------------------------------------------------------------------------

# 默认识别语言（顺序即优先级），中文简体 + 英文
DEFAULT_LANGUAGES = ["zh-Hans", "en-US"]

# OCR 精度档位：accurate=高精度（稍慢）/ fast=快速
OCR_LEVELS = {
    "accurate": Vision.VNRequestTextRecognitionLevelAccurate,
    "fast": Vision.VNRequestTextRecognitionLevelFast,
}

# 超大图自动缩放到该像素上限，控制内存与耗时（Vision 内部同样会处理，
# 但对 8000px+ 的截图先缩略能显著提速）
MAX_PIXEL_DEFAULT = 4096

# 支持的图片格式（用于快速校验提示；最终以 CGImageSource 能否解码为准）
SUPPORTED_EXTS = {
    ".jpg", ".jpeg", ".png", ".heic", ".heif",
    ".tif", ".tiff", ".gif", ".bmp", ".webp",
}

DATA_URI_RE = re.compile(r"^data:image/[a-zA-Z0-9.+-]+;base64,(.+)$", re.S)

# 解码 data URI 后落盘的临时文件。调用方用完即可清理，避免长期堆积。
_TEMP_FILES: set[str] = set()


def is_temp_file(path: str) -> bool:
    """该路径是否是本模块解码 data URI 时创建的临时文件。"""
    return path in _TEMP_FILES


def cleanup_temp_files(keep: Optional[set] = None) -> int:
    """删除本模块创建的全部临时图片文件，返回删除数量。

    `keep` 中的路径会被保留（例如截图，用户可能想留着看）。
    并发场景请用 `remove_temp_file` 只删本次调用自己的文件。
    """
    keep = keep or set()
    removed = 0
    for path in list(_TEMP_FILES):
        if path in keep:
            continue
        if remove_temp_file(path):
            removed += 1
    return removed


def remove_temp_file(path: str) -> bool:
    """只删除本模块创建的某一个临时文件；不是本模块创建的则不动。"""
    if path not in _TEMP_FILES:
        return False
    _TEMP_FILES.discard(path)
    try:
        os.remove(path)
        return True
    except OSError:
        return False


def normalize_level(level: Optional[str]) -> str:
    """把 OCR 档位归一到已知值；未知值回退 accurate，而不是静默用默认常量。"""
    value = (level or "").strip().lower()
    return value if value in OCR_LEVELS else "accurate"


# ---------------------------------------------------------------------------
# 噪声过滤（图标/符号误识启发式）
# ---------------------------------------------------------------------------

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_ALNUM_RE = re.compile(r"[A-Za-z0-9]")
# 常见"图标/符号"类 Unicode 块：带圈数字/字母、©®™、箭头、几何形状、dingbats、emoji
_SYMBOL_BLOCKS_RE = re.compile(
    r"[\u2460-\u24ff"                 # ①-⑳ ㉑-㉟ 带圈数字/括号字母
    r"\u00a9\u00ae\u2122"             # © ® ™
    r"\u2190-\u21ff"                  # 箭头
    r"\u25a0-\u25ff"                  # 几何形状 ■□◆等
    r"\u2700-\u27bf"                  # dingbats ✀-➿
    r"\u2b00-\u2bff"                  # 补充箭头/形状
    r"\U0001f000-\U0001faff"          # emoji 🀀-🿿
    r"]"
)


def is_noise_line(text: str, confidence: float, min_conf_single: float = 0.4) -> bool:
    """启发式判断一行 OCR 结果是否为噪声（图标/符号误识）。

    满足任一规则即视为噪声：
      1. 空行
      2. 纯符号行：不含任何 CJK/字母/数字，且长度 >= 2（如 "•••"、"***"）
      3. 特殊符号字符（带圈数字/箭头/几何形状/emoji 等）占整行字符数 >= 60%
      4. 单字符行、置信度 < min_conf_single，且该字符既不是字母数字也不是汉字

    规则 4 故意只覆盖“非文字”的单字符：图标常被误识成一个标点或怪符号，
    而单独一个字母/数字/汉字更可能是真内容（答案表、坐标、编号列表）。早期版本
    对所有低置信度单字符一律判噪声，会把 "B"、"7" 这类真内容误删，且同一张图里
    "A" 因置信度略高而被保留 —— 行为不可预测。
    """
    t = text.strip()
    if not t:
        return True

    # 规则 2：纯符号行
    if len(t) >= 2 and not _CJK_RE.search(t) and not _ALNUM_RE.search(t):
        return True

    # 规则 3：特殊符号占比过高
    sym_chars = len(_SYMBOL_BLOCKS_RE.findall(t))
    if sym_chars > 0 and sym_chars / len(t) >= 0.6:
        return True

    # 规则 4：单字符 + 非文字字符 + 低置信度
    if len(t) == 1 and confidence < min_conf_single:
        if not _ALNUM_RE.search(t) and not _CJK_RE.search(t):
            return True

    return False


def split_noise_lines(lines: list[dict]) -> tuple[list[dict], list[dict]]:
    """把 OCR 行分为 (保留行, 噪声行)。"""
    clean, noise = [], []
    for l in lines:
        if is_noise_line(l["text"], l["confidence"]):
            noise.append(l)
        else:
            clean.append(l)
    return clean, noise


# ---------------------------------------------------------------------------
# 图片加载与信息
# ---------------------------------------------------------------------------

def _load_cg_image(path: str, max_pixel: Optional[int] = MAX_PIXEL_DEFAULT):
    """加载图片为 CGImage；超大图按 max_pixel 生成缩略图以提升识别速度。"""
    if not os.path.isfile(path):
        raise FileNotFoundError(f"图片文件不存在: {path}")

    url = NSURL.fileURLWithPath_(path)
    source = Quartz.CGImageSourceCreateWithURL(url, None)
    if source is None:
        raise ValueError(f"无法打开图片（可能不是有效图像文件）: {path}")

    if max_pixel and max_pixel > 0:
        opts = {
            Quartz.kCGImageSourceCreateThumbnailFromImageAlways: True,
            Quartz.kCGImageSourceThumbnailMaxPixelSize: max_pixel,
            Quartz.kCGImageSourceCreateThumbnailWithTransform: True,
        }
        cg = Quartz.CGImageSourceCreateThumbnailAtIndex(source, 0, opts)
    else:
        cg = Quartz.CGImageSourceCreateImageAtIndex(source, 0, None)

    if cg is None:
        raise ValueError(f"图片解码失败（格式不受支持或文件损坏）: {path}")
    return cg


def image_info(path: str) -> dict:
    """返回图片基本元信息：尺寸 / 大小 / UTI 格式。"""
    url = NSURL.fileURLWithPath_(path)
    source = Quartz.CGImageSourceCreateWithURL(url, None)
    info: dict = {"path": path, "size_bytes": os.path.getsize(path)}
    if source is not None:
        props = Quartz.CGImageSourceCopyPropertiesAtIndex(source, 0, None)
        if props:
            info["pixel_width"] = int(props.get(Quartz.kCGImagePropertyPixelWidth, 0) or 0)
            info["pixel_height"] = int(props.get(Quartz.kCGImagePropertyPixelHeight, 0) or 0)
        info["uti"] = Quartz.CGImageSourceGetType(source) or "unknown"
    return info


def _write_temp(data: bytes, prefix: str = "wb_mcp_img_", suffix: str = ".png") -> str:
    """把二进制数据写入临时文件，返回路径；登记以便后续清理。"""
    fd, path = tempfile.mkstemp(prefix=prefix, suffix=suffix)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    _TEMP_FILES.add(path)
    return path


# 常见图片魔数 → 扩展名，用于给 data URI 落盘时选对后缀
_MAGIC_SUFFIX = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"BM", ".bmp"),
    (b"II*\x00", ".tiff"),
    (b"MM\x00*", ".tiff"),
)


def _suffix_for(data: bytes) -> str:
    for magic, suffix in _MAGIC_SUFFIX:
        if data.startswith(magic):
            return suffix
    # HEIC/HEIF/WebP: 看容器内标识
    if len(data) > 12 and data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in (b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1"):
            return ".heic"
        if brand == b"avif":
            return ".avif"
    if len(data) > 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ".png"


def resolve_image_source(source: str, workdir: Optional[str] = None) -> str:
    """把「本地路径 / data URI / 纯 base64」统一解析为本地文件路径。

    - data:image/...;base64,xxxx  → 解码写入临时文件（按魔数选后缀）
    - 极长裸 base64               → 按魔数兜底解码（PNG/JPEG/GIF/BMP/TIFF/WebP）
    - 相对路径 → 基于 workdir（默认当前目录）解析
    - 绝对路径 → 直接校验存在性
    """
    source = source.strip()
    if not source:
        raise ValueError("image 参数为空：请提供图片路径或 base64 data URI")

    m = DATA_URI_RE.match(source)
    if m:
        try:
            raw = base64.b64decode(m.group(1))
        except Exception as exc:
            raise ValueError(f"data URI base64 解码失败: {exc}") from exc
        if not raw:
            raise ValueError("data URI 解码结果为空")
        return _write_temp(raw, suffix=_suffix_for(raw))

    # 纯 base64 兜底：长度足够且魔数可识别，避免误伤普通路径
    if len(source) > 200 and not source.startswith("/"):
        try:
            raw = base64.b64decode(source, validate=True)
        except Exception:
            raw = None
        if raw:
            suffix = _suffix_for(raw)
            known = any(raw.startswith(magic) for magic, _ in _MAGIC_SUFFIX) or suffix != ".png"
            if known:
                return _write_temp(raw, suffix=suffix)

    if not source.startswith("/"):
        base = workdir or os.getcwd()
        cand = os.path.abspath(os.path.join(base, source))
        if os.path.isfile(cand):
            return cand
        raise FileNotFoundError(f"图片路径不存在: {source}")

    if not os.path.isfile(source):
        raise FileNotFoundError(f"图片文件不存在: {source}")
    return source


# ---------------------------------------------------------------------------
# OCR：文字识别
# ---------------------------------------------------------------------------

def order_lines(lines: list[dict], column_gap: float = 0.04) -> list[dict]:
    """把 OCR 行排成人类阅读顺序，并处理多栏排版。

    单纯的 `(-y, x)` 排序只解决单栏：两栏布局会左右交错（LEFT-1, RIGHT-1,
    LEFT-2, ...），拼出的全文不可读且不报错。这里先按纵向重叠把行分成「行带」，
    再判断整页是否是多栏：

    1. 估算行高，把 y 相近的行归入同一行带；
    2. 找一条纵向空白带：所有行带的 x 区间都在该处断开，且两侧都有内容；
    3. 若存在这样的分割（并满足最小栏宽），就按「先左栏到底，再右栏到底」输出；
    4. 否则退回单栏的按行带顺序。

    `column_gap` 是判定栏间空白的最小宽度（归一化坐标），默认 4%。
    """
    if not lines:
        return []

    ordered = sorted(lines, key=lambda l: (-l["bbox"]["y"], l["bbox"]["x"]))
    heights = [l["bbox"]["height"] for l in ordered if l["bbox"]["height"] > 0]
    line_h = max(sum(heights) / len(heights), 0.008) if heights else 0.02

    # 1. 归入行带：y 中心落在同一带内视为一行
    bands: list[list[dict]] = []
    for line in ordered:
        cy = line["bbox"]["y"] + line["bbox"]["height"] / 2
        if bands:
            last = bands[-1]
            last_cy = sum(
                b["bbox"]["y"] + b["bbox"]["height"] / 2 for b in last
            ) / len(last)
            if abs(cy - last_cy) <= line_h * 0.6:
                last.append(line)
                continue
        bands.append([line])

    for band in bands:
        band.sort(key=lambda l: l["bbox"]["x"])

    # 2. 找纵向空白带：候选点来自各行带的左右边界
    def find_split() -> Optional[float]:
        edges = set()
        for band in bands:
            for line in band:
                b = line["bbox"]
                edges.add(round(b["x"], 4))
                edges.add(round(b["x"] + b["width"], 4))
        candidates = sorted(edges)
        best = None
        for cut in candidates:
            left = [l for band in bands for l in band if l["bbox"]["x"] + l["bbox"]["width"] <= cut + 1e-6]
            right = [l for band in bands for l in band if l["bbox"]["x"] >= cut - 1e-6]
            spanning = [l for band in bands for l in band
                        if l["bbox"]["x"] < cut - 1e-6 and l["bbox"]["x"] + l["bbox"]["width"] > cut + 1e-6]
            # 没有横跨分割线的行，且两侧都有内容
            if spanning or not left or not right:
                continue
            left_edge = min(l["bbox"]["x"] for l in left)
            right_edge = max(l["bbox"]["x"] + l["bbox"]["width"] for l in left)
            right_start = min(l["bbox"]["x"] for l in right)
            right_far = max(l["bbox"]["x"] + l["bbox"]["width"] for l in right)
            # 栏间空白足够宽，且两侧各自成栏（不是同一行的两段）
            if right_start - right_edge >= column_gap:
                # 要求两侧宽度相近，避免把「标题 + 右对齐页码」误判成两栏
                lw = right_edge - left_edge
                rw = right_far - right_start
                if lw > 0 and rw > 0 and min(lw, rw) / max(lw, rw) >= 0.25:
                    best = cut
        return best

    cut = find_split()
    if cut is None:
        return [l for band in bands for l in band]

    # 3. 多栏：先左栏按行带输出，再右栏
    left_lines = [l for band in bands for l in band if l["bbox"]["x"] + l["bbox"]["width"] <= cut + 1e-6]
    right_lines = [l for band in bands for l in band if l["bbox"]["x"] >= cut - 1e-6]
    return left_lines + right_lines


def ocr_image(
    path: str,
    languages: Optional[list] = None,
    level: str = "accurate",
    min_confidence: float = 0.2,
    max_pixel: Optional[int] = MAX_PIXEL_DEFAULT,
) -> list[dict]:
    """提取图片中的全部可见文字。

    返回按阅读顺序排列的文本行，每行含：
      text       识别出的字符串
      confidence 置信度 0-1
      bbox       归一化包围框 (x, y, w, h)，坐标原点在左下角
    """
    cg = _load_cg_image(path, max_pixel)

    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(OCR_LEVELS.get(normalize_level(level), Vision.VNRequestTextRecognitionLevelAccurate))
    request.setUsesLanguageCorrection_(True)
    request.setRecognitionLanguages_(languages or DEFAULT_LANGUAGES)

    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cg, None)
    ok, err = handler.performRequests_error_([request], None)
    if not ok:
        raise RuntimeError(f"Vision OCR 请求失败: {err}")

    lines: list[dict] = []
    for obs in request.results() or []:
        candidates = obs.topCandidates_(1)
        if not candidates or len(candidates) == 0:
            continue
        cand = candidates[0]
        text = str(cand.string())
        conf = float(cand.confidence())
        if conf < min_confidence:
            continue
        box = obs.boundingBox()
        lines.append({
            "text": text,
            "confidence": round(conf, 3),
            "bbox": {
                "x": round(float(box.origin.x), 4),
                "y": round(float(box.origin.y), 4),
                "width": round(float(box.size.width), 4),
                "height": round(float(box.size.height), 4),
            },
        })

    return order_lines(lines)


# ---------------------------------------------------------------------------
# 图像分类：主体 / 场景
# ---------------------------------------------------------------------------

def classify_image(
    path: str,
    top_k: int = 8,
    min_confidence: float = 0.05,
    max_pixel: Optional[int] = MAX_PIXEL_DEFAULT,
    dedupe: bool = True,
) -> list[dict]:
    """识别图像主体/场景（VNClassifyImageRequest）。

    返回按置信度降序的标签列表：[{label, confidence}, ...]
    label 形如 "Animal", "Food", "Landscape", "Vehicle" 等（英文，由模型自行理解）。
    自动过滤 negative 类目。

    `dedupe` 为真时，把置信度几乎相同的标签视为同一个判定结果，只保留最具信息量
    的那一个：Vision 对同一条视觉线索常同时给出多个同分标签（如
    recreation / sport / ping_pong 全是 0.686），逐个列出会误导模型以为有多个主体。
    """
    cg = _load_cg_image(path, max_pixel)

    request = Vision.VNClassifyImageRequest.alloc().init()
    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cg, None)
    ok, err = handler.performRequests_error_([request], None)
    if not ok:
        raise RuntimeError(f"Vision 分类请求失败: {err}")

    items: list[dict] = []
    for obs in request.results() or []:
        ident = str(obs.identifier() or "")
        conf = float(obs.confidence())
        if ident.startswith("negative"):
            continue
        if conf < min_confidence:
            continue
        items.append({"label": ident, "confidence": round(conf, 3)})

    items.sort(key=lambda r: -r["confidence"])

    if dedupe:
        kept: list[dict] = []
        for item in items:
            # 与已保留项置信度几乎相同 → 同一判定结果的不同叫法
            if any(abs(item["confidence"] - k["confidence"]) < 1e-3 for k in kept):
                continue
            kept.append(item)
        items = kept

    return items[:top_k]


# ---------------------------------------------------------------------------
# 综合识别
# ---------------------------------------------------------------------------

def analyze_image(
    path: str,
    languages: Optional[list] = None,
    max_pixel: Optional[int] = MAX_PIXEL_DEFAULT,
    level: str = "accurate",
) -> dict:
    """一站式识别：图像信息 + OCR 全文 + 主体/场景分类 + 可读摘要。"""
    started = time.time()
    ocr_lines = ocr_image(path, languages=languages, level=level, max_pixel=max_pixel)
    classes = classify_image(path, max_pixel=max_pixel)

    full_text = "\n".join(l["text"] for l in ocr_lines)
    summary_parts: list[str] = []
    if full_text.strip():
        summary_parts.append(f"图中文字(OCR):\n{full_text}")
    if classes:
        top = ", ".join(f"{c['label']}({c['confidence']:.2f})" for c in classes[:3])
        summary_parts.append(f"图像主体/场景: {top}")

    return {
        "image": path,
        "info": image_info(path),
        "ocr": ocr_lines,
        "classification": classes,
        "summary": "\n".join(summary_parts) or "未能识别出文字或类别。",
        "elapsed_ms": int((time.time() - started) * 1000),
    }


# ---------------------------------------------------------------------------
# 屏幕截图（可选能力）
# ---------------------------------------------------------------------------

def screen_capture(out_dir: Optional[str] = None) -> str:
    """截取当前整个屏幕，返回图片路径。

    注意：需要「屏幕录制」权限。首次调用时 macOS 会弹出授权提示，
    需在 系统设置 > 隐私与安全性 > 屏幕录制 中为宿主终端/App 开启。
    """
    out_dir = out_dir or tempfile.gettempdir()
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"wb_screenshot_{int(time.time() * 1000)}.png")
    proc = subprocess.run(
        ["/usr/sbin/screencapture", "-x", "-t", "png", path],
        capture_output=True, text=True, timeout=15,
    )
    if proc.returncode != 0 or not os.path.isfile(path):
        raise RuntimeError(
            "截图失败: "
            + (proc.stderr.strip() or f"返回码 {proc.returncode}")
            + "。请在 系统设置 > 隐私与安全性 > 屏幕录制 中授权。"
        )
    return path


# ---------------------------------------------------------------------------
# 便捷入口（供命令行自测）
# ---------------------------------------------------------------------------

def _cli() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="macOS Vision 本地图片识别（自测用）")
    parser.add_argument("image", help="图片路径或 data URI")
    parser.add_argument("--mode", choices=["ocr", "classify", "analyze", "shot"],
                        default="analyze", help="识别模式")
    parser.add_argument("--langs", default=",".join(DEFAULT_LANGUAGES), help="OCR 语言，逗号分隔")
    args = parser.parse_args()

    if args.mode == "shot":
        p = screen_capture()
        print(f"截图已保存: {p}")
        return

    path = resolve_image_source(args.image)
    langs = [l.strip() for l in args.langs.split(",") if l.strip()]
    if args.mode == "ocr":
        import json
        print(json.dumps(ocr_image(path, languages=langs), ensure_ascii=False, indent=2))
    elif args.mode == "classify":
        import json
        print(json.dumps(classify_image(path), ensure_ascii=False, indent=2))
    else:
        import json
        print(json.dumps(analyze_image(path, languages=langs), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _cli()
