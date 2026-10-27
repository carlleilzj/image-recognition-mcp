#!/usr/bin/env python3
"""图片识别 MCP 服务器 —— 基于 macOS 本地 Vision 框架。

通过 MCP(Model Context Protocol) 标准协议，以 stdio 方式为任意 MCP 客户端
(opencode / Claude Desktop / Cursor / Cline 等) 提供图片识别工具：

  • ocr_image                提取图中全部文字(OCR)
  • recognize_image          综合识别(OCR + 主体分类 + 图像信息 + 摘要)
  • describe_image           识别图像主体/场景(视觉分类)
  • screenshot_and_recognize 截取当前屏幕并识别(需屏幕录制权限)

image 参数支持三种来源：
  1. 本地绝对路径          /Users/me/Pictures/x.png
  2. data URI             data:image/png;base64,....
  3. 相对路径(基于客户端工作目录)

运行方式(stdio)：python mcp_server.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

# 允许从同目录 import vision_engine（脚本方式 / 被 MCP 客户端调用均适用）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import vision_engine as ve

# mcp 2.x 提供 MCPServer；1.x 只有 FastMCP。requirements.txt 锁 2.x，
# 这里仍做双路径导入，避免旧环境直接崩溃。
try:
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # pragma: no cover - 仅 mcp<2 才会走到
    from mcp.server.fastmcp import FastMCP as _Server

FastMCP = _Server

SERVER_INSTRUCTIONS = """图片识别服务器（基于 macOS 本地 Vision 框架，全程本地推理、无网络请求）。

适用场景：当会话中出现截图/图片（本地路径或 base64 data URI），且当前模型不具备视觉能力时，
调用本服务器工具完成识别：
  - ocr_image: 提取图中全部文字（OCR，支持中英混排）
  - recognize_image: 综合识别（OCR + 主体/场景分类 + 图像元信息 + 可读摘要）
  - describe_image: 识别图像主体/场景类别
  - screenshot_and_recognize: 截取当前屏幕并识别（需要屏幕录制权限）

image 参数接受：
  1. 本地绝对路径，如 /Users/me/Pictures/x.png
  2. data URI，如 data:image/png;base64,....（粘贴的图片常以该形式出现）
  3. 相对路径（基于客户端工作目录）

所有工具返回 JSON 文本；出错时返回 {"status": "error", "error": "..."}。
只需要文字时优先用 ocr_image 且保持 include_bbox=false（默认）：带坐标的返回体积约为纯文本的 10 倍。
"""

mcp = FastMCP("image-recognition", instructions=SERVER_INSTRUCTIONS)


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------

def _resolve(image: str) -> tuple[str, bool]:
    """解析图片来源，返回 (本地路径, 是否为本次解码出的临时文件)。"""
    try:
        path = ve.resolve_image_source(image)
    except Exception as exc:
        raise ValueError(f"无法解析图片来源: {exc}") from exc
    return path, ve.is_temp_file(path)


def _release(path: str) -> None:
    """删除本次调用解码出的临时文件；用户自己的图片路径不受影响。"""
    ve.remove_temp_file(path)


def _label(path: str, temporary: bool) -> str:
    """返回给调用方的图片来源标识：临时文件已删除，就不再谎报一个不存在的路径。"""
    return "data-uri" if temporary else path


def _ok(**payload) -> str:
    # 紧凑输出：indent=2 会让同样的内容多占约 30% 字符
    return json.dumps({"status": "ok", **payload}, ensure_ascii=False)


def _err(message: str) -> str:
    return json.dumps({"status": "error", "error": message}, ensure_ascii=False)


def _langs(languages: str) -> list[str]:
    return [l.strip() for l in (languages or "").split(",") if l.strip()] or ve.DEFAULT_LANGUAGES


def _confidence(value, name: str, default: float) -> float:
    """校验置信度阈值：必须是 0~1 的数字，越界直接报错而不是静默失效。"""
    if value is None:
        return default
    try:
        f = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} 必须是 0~1 之间的数字，收到 {value!r}")
    if not 0.0 <= f <= 1.0:
        raise ValueError(f"{name} 必须在 0~1 之间，收到 {f}")
    return f


def _level(value) -> str:
    """校验 OCR 档位。"""
    if value is None or value == "":
        return "accurate"
    normalized = ve.normalize_level(value)
    if normalized != str(value).strip().lower():
        raise ValueError(f"level 只能是 accurate 或 fast，收到 {value!r}")
    return normalized


def _lines_payload(lines: list[dict], include_bbox: bool) -> list[dict]:
    """按需裁剪每行结果；bbox 不是必需信息时省掉，能省下约九成体积。"""
    if include_bbox:
        return lines
    return [{"text": l["text"]} for l in lines]


def _ocr_payload(path: str, languages, level: str, min_confidence: float,
                 filter_noise: bool, include_bbox: bool, temporary: bool = False) -> dict:
    lines = ve.ocr_image(path, languages=_langs(languages), level=level,
                         min_confidence=min_confidence)
    clean, noise = ve.split_noise_lines(lines) if filter_noise else (lines, [])
    payload = {
        "image": _label(path, temporary),
        "text": "\n".join(l["text"] for l in clean),
        "count": len(clean),
        "lines": _lines_payload(clean, include_bbox),
    }
    if noise:
        payload["noise"] = _lines_payload(noise, include_bbox)
    return payload


# ---------------------------------------------------------------------------
# MCP 工具
# ---------------------------------------------------------------------------

@mcp.tool()
def ocr_image(image: str, languages: str = "zh-Hans,en-US", min_confidence: float = 0.2,
              filter_noise: bool = True, level: str = "accurate",
              include_bbox: bool = False) -> str:
    """从图片中提取所有可见文字（OCR），支持中文与英文混排。

    Args:
        image: 图片来源 —— 本地文件路径 或 data:image/...;base64,.... 形式的 base64 数据
        languages: 识别语言（逗号分隔，顺序即优先级），默认 "zh-Hans,en-US"
        min_confidence: 最低置信度(0~1)，低于该值的识别结果将被过滤
        filter_noise: 是否过滤图标/符号误识噪声（默认 true）。被过滤项会放在
            "noise" 字段中返回，不丢失信息；金额、卡号、交易编号等数字串会被保留
        level: OCR 档位。accurate(默认) 或 fast(约快 4 倍)。注意 fast 会明显损失中文准确率
            （实测中文小字会变成乱码、截图整行遗漏），只建议用于大号清晰英文或快速预览
        include_bbox: 是否返回每行的坐标包围框（默认 false）。开启后返回体积约增大 10 倍

    Returns:
        JSON：{status, image, text, count, lines, noise}，text 为拼接全文，
        lines 为过滤后的逐行结果（含文本，include_bbox 时另含置信度与归一化位置 bbox）
    """
    path = None
    try:
        path, temporary = _resolve(image)
        payload = _ocr_payload(path, languages, _level(level),
                               _confidence(min_confidence, "min_confidence", 0.2),
                               bool(filter_noise), bool(include_bbox), temporary)
        return _ok(**payload)
    except Exception as exc:
        return _err(str(exc))
    finally:
        if path:
            _release(path)


@mcp.tool()
def recognize_image(image: str, languages: str = "zh-Hans,en-US", filter_noise: bool = True,
                    level: str = "accurate", include_bbox: bool = False) -> str:
    """对图片做综合识别：OCR 文字 + 图像主体/场景分类 + 图像元信息 + 可读摘要。

    Args:
        image: 图片来源 —— 本地文件路径 或 base64 data URI
        languages: OCR 识别语言（逗号分隔），默认 "zh-Hans,en-US"
        filter_noise: 是否过滤图标/符号误识噪声（默认 true），被过滤项放 "noise" 字段
        level: OCR 档位，accurate(默认) 或 fast(快 4 倍但明显损失中文准确率)
        include_bbox: 是否返回每行坐标（默认 false）

    Returns:
        JSON：{status, image, info, ocr, classification, summary, noise, elapsed_ms}
    """
    path = None
    try:
        path, temporary = _resolve(image)
        result = ve.analyze_image(path, languages=_langs(languages), level=_level(level))
        clean, noise = ve.split_noise_lines(result["ocr"]) if filter_noise else (result["ocr"], [])
        full_text = "\n".join(l["text"] for l in clean)
        summary_parts = []
        if full_text.strip():
            summary_parts.append(f"图中文字(OCR):\n{full_text}")
        if result["classification"]:
            top = ", ".join(f"{c['label']}({c['confidence']:.2f})" for c in result["classification"][:3])
            summary_parts.append(f"图像主体/场景: {top}")
        result["image"] = _label(path, temporary)
        result["info"]["path"] = _label(path, temporary)
        result["ocr"] = _lines_payload(clean, bool(include_bbox))
        result["summary"] = "\n".join(summary_parts) or "未能识别出文字或类别。"
        if noise:
            result["noise"] = _lines_payload(noise, bool(include_bbox))
        return _ok(**result)
    except Exception as exc:
        return _err(str(exc))
    finally:
        if path:
            _release(path)


@mcp.tool()
def describe_image(image: str, top_k: int = 8, min_confidence: float = 0.05,
                   dedupe: bool = True) -> str:
    """识别图像主体/场景（视觉分类），返回按置信度降序的类别标签。

    Args:
        image: 图片来源 —— 本地文件路径 或 base64 data URI
        top_k: 最多返回的类别数(1~20)，默认 8
        min_confidence: 最低置信度(0~1)，默认 0.05
        dedupe: 合并置信度相同/近似的标签（默认 true）。Vision 对同一视觉线索常给出多个
            同分标签（如 recreation / sport / ping_pong 都是 0.686），去重后只保留一个

    Returns:
        JSON：{status, image, labels: [{label, confidence}]}
        label 为英文类别（如 Animal / Landscape / Food / Vehicle / Scene），由调用方模型自行理解
    """
    path = None
    try:
        path, temporary = _resolve(image)
        top_k = max(1, min(20, int(top_k)))
        labels = ve.classify_image(
            path,
            top_k=top_k,
            min_confidence=_confidence(min_confidence, "min_confidence", 0.05),
            dedupe=bool(dedupe),
        )
        return _ok(image=_label(path, temporary), labels=labels)
    except Exception as exc:
        return _err(str(exc))
    finally:
        if path:
            _release(path)


@mcp.tool()
def screenshot_and_recognize(languages: str = "zh-Hans,en-US", filter_noise: bool = True,
                             level: str = "accurate", include_bbox: bool = False) -> str:
    """截取当前整个屏幕并识别图中文字（OCR）。

    需要「屏幕录制」权限：首次调用请在 系统设置 > 隐私与安全性 > 屏幕录制
    中为宿主终端/App 开启授权。

    Args:
        languages: OCR 识别语言（逗号分隔），默认 "zh-Hans,en-US"
        filter_noise: 是否过滤图标/符号误识噪声（默认 true），被过滤项放 "noise" 字段
        level: OCR 档位，accurate(默认) 或 fast(快 4 倍但明显损失中文准确率)
        include_bbox: 是否返回每行坐标（默认 false）

    Returns:
        JSON：{status, image, text, count, lines, noise}，与 ocr_image 相同
    """
    try:
        path = ve.screen_capture(out_dir=tempfile.gettempdir())
        payload = _ocr_payload(path, languages, _level(level), 0.2,
                               bool(filter_noise), bool(include_bbox))
        return _ok(**payload)
    except Exception as exc:
        return _err(str(exc))


if __name__ == "__main__":
    mcp.run(transport="stdio")
