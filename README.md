# image-recognition-mcp

[![Powered by RustChain](https://img.shields.io/badge/Powered%20by-RustChain-orange)](https://rustchain.org)

> 基于 macOS 本地 Vision 框架的图片识别 MCP 服务器 —— 让无视觉 AI 模型也能"看见"截图与图片。

为 AI 客户端（opencode / Claude Desktop / Cursor / Cline 等）提供 4 个 MCP 工具：  
**OCR 文字识别 / 图像主体分类 / 综合识别 / 屏幕截图并识别**。全程本地推理，数据不出本机。

---

## 目录

- [特性](#特性)
- [架构](#架构)
- [快速开始](#快速开始)
- [MCP 工具说明](#mcp-工具说明)
- [输入与输出格式](#输入与输出格式)
- [触发机制说明](#触发机制说明)
- [客户端接入配置](#客户端接入配置)
- [性能与资源](#性能与资源)
- [权限与隐私](#权限与隐私)
- [故障排查](#故障排查)
- [扩展建议](#扩展建议)

---

## 特性

- **100% 本地推理**：基于 Apple Vision 框架（`VNRecognizeTextRequest` + `VNClassifyImageRequest`），零网络请求，零外部 API 调用。
- **中英文混排 OCR**：支持中文（zh-Hans）、英文及 20+ 种语言，含手写体识别，可选精度档位（accurate / fast）。
- **图像主体/场景分类**：返回类别标签与置信度，模型可基于此生成自然语言描述。
- **三种图片来源**：本地路径、`data:image/png;base64,...` URI、纯 base64（PNG 魔数校验）。
- **超大图自动缩略**：默认将大于 4096px 的图片自动生成缩略图后再识别，速度更快、内存更省。
- **结构化 JSON 输出**：所有工具返回统一的 `{status, ...}` JSON，包含置信度与归一化包围框，便于模型解析与引用。
- **可选屏幕截图**：直接调用 `screencapture` 命令截屏并识别（需屏幕录制权限）。

---

## 架构

```
┌────────────────────────────────────────────────────────────┐
│  AI 会话客户端（opencode / Claude Desktop / Cursor / ...）   │
│  无视觉模型看到图片路径 → 调用工具                            │
└──────────────────────────┬─────────────────────────────────┘
                           │  MCP 协议 (stdio JSON-RPC)
┌──────────────────────────▼─────────────────────────────────┐
│  image-recognition MCP 服务器 (Python + MCPServer)          │
│  ┌──────────────┬──────────────┬──────────────┐            │
│  │  ocr_image   │recognize_image│describe_image│            │
│  │screenshot_…  │              │              │            │
│  └──────────────┴──────────────┴──────────────┘            │
└──────────────────────────┬─────────────────────────────────┘
                           │  Vision 框架调用 (pyobjc)
┌──────────────────────────▼─────────────────────────────────┐
│  macOS 本地视觉引擎                                          │
│  VNRecognizeTextRequest   —— OCR（中英+多语言）              │
│  VNClassifyImageRequest   —— 图像主体/场景分类                │
│  全程本机推理，无网络请求，数据不出设备                        │
└────────────────────────────────────────────────────────────┘
```

---

## 快速开始

### 环境要求

- macOS 13+（推荐 14+，Vision 框架中文识别效果最佳）
- Python 3.10+（已测试 3.13.12）
- 已安装 Xcode Command Line Tools（`xcode-select --install`）

### 安装

```bash
# 克隆/进入项目目录
cd /path/to/image-recognition-mcp

# 创建 venv 并安装依赖
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
```

### 自测

```bash
# 生成一张含中英文的测试图片
.venv/bin/python scripts/make_test_image.py

# 直接测试 Vision 引擎（不走 MCP）
.venv/bin/python scripts/test_engine.py sample/test_card.png

# 端到端测试 MCP 服务器（启动 stdio，列出工具，调用 OCR）
.venv/bin/python scripts/test_mcp.py sample/test_card.png
```

预期输出：3 行文字（`MacBook Air 图片识别测试` / `Hello Vision OCR 12345` / `日期：2026-08-04 13:30`）被完整识别，且图像分类结果合理（document/printed_page/screenshot 等）。

### 直接命令行调用引擎（可选）

```bash
# OCR
.venv/bin/python vision_engine.py /path/to/image.png --mode ocr

# 主体分类
.venv/bin/python vision_engine.py /path/to/image.png --mode classify

# 综合识别
.venv/bin/python vision_engine.py /path/to/image.png --mode analyze

# 截屏到 ~/Pictures
.venv/bin/python vision_engine.py --mode shot
```

---

## MCP 工具说明

服务器启动后向客户端暴露 4 个工具：

### 1. `ocr_image` — 提取图片中的文字（OCR）

```json
{
  "image": "/Users/me/Pictures/shot.png",      // 必填，路径 / data URI / 纯 base64
  "languages": "zh-Hans,en-US",                // 可选，逗号分隔，顺序即优先级
  "min_confidence": 0.2,                       // 可选，0~1，过滤低置信度结果
  "filter_noise": true                         // 可选，默认 true，过滤图标/符号误识噪声
}
```

> **filter_noise 说明**：自动过滤截图中的图标误识噪声（如 `•••`、`③`、单独一个 `8`/`凸` 等），
> 但**保留可能有业务含义的数字串**（金额、卡号、交易编号、时间等）。被过滤的行会单独放在
> 返回的 `noise` 字段中，不丢失信息；如需原始全量结果，设 `filter_noise: false`。

**返回**：

```json
{
  "status": "ok",
  "image": "/Users/me/Pictures/shot.png",
  "text": "完整拼接的全文",
  "count": 3,
  "lines": [
    {
      "text": "MacBook Air 图片识别测试",
      "confidence": 0.5,
      "bbox": {"x": 0.052, "y": 0.695, "width": 0.555, "height": 0.133}
    }
  ]
}
```

### 2. `recognize_image` — 综合识别

```json
{
  "image": "/path/to/img.png",
  "languages": "zh-Hans,en-US"
}
```

**返回**：

```json
{
  "status": "ok",
  "image": "/path/to/img.png",
  "info": {"path": "...", "size_bytes": 12345, "pixel_width": 1200, "pixel_height": 420, "uti": "public.png"},
  "ocr": [...],
  "classification": [{"label": "document", "confidence": 0.529}, ...],
  "summary": "图中文字(OCR):\n... \n图像主体/场景: document(0.53)",
  "elapsed_ms": 98
}
```

### 3. `describe_image` — 主体/场景分类

```json
{
  "image": "/path/to/img.png",
  "top_k": 8,                    // 1~20
  "min_confidence": 0.05
}
```

**返回**：

```json
{
  "status": "ok",
  "image": "/path/to/img.png",
  "labels": [
    {"label": "Animal", "confidence": 0.812},
    {"label": "Cat", "confidence": 0.703}
  ]
}
```

> label 为英文（如 Animal / Landscape / Food / Vehicle），由调用方模型自行理解并翻译。

### 4. `screenshot_and_recognize` — 截屏并识别

```json
{
  "languages": "zh-Hans,en-US"
}
```

截取整个屏幕 → OCR。**需要屏幕录制权限**，详见 [权限与隐私](#权限与隐私)。

---

## 输入与输出格式

### 输入格式（`image` 参数）

| 形式       | 示例                                   | 说明              |
| -------- | ------------------------------------ | --------------- |
| 本地绝对路径   | `/Users/me/Pictures/x.png`           | 最常用             |
| 相对路径     | `shot.png` / `./imgs/x.png`          | 基于客户端工作目录       |
| data URI | `data:image/png;base64,iVBORw0KG...` | 用户直接粘贴图片时常见     |
| 纯 base64 | `iVBORw0KG...`                       | 兜底（自动校验 PNG 魔数） |

**实测**：桌面截图 256KB → base64 data URI（约 34 万字符）→ MCP 工具调用，识别 42 行有效文字 + 4 行噪声，耗时约 0.6s，与直接传路径结果一致。

服务器会自动：

- 路径存在性校验
- data URI / base64 解码后写入临时文件
- 格式支持校验（基于 CGImageSource，兼容 JPEG/PNG/HEIC/TIFF/GIF/BMP/WebP）

### 输出格式

- 所有工具返回字符串（JSON），便于模型直接解析。
- 成功：`{"status": "ok", ...}`
- 失败：`{"status": "error", "error": "..."}`
- 包围框坐标 (bbox) 为归一化值（原点在左下角，0~1），与 Vision 框架一致。

---

## 触发机制说明

MCP 采用「工具由模型按需调用」的协议设计，**服务器无法主动感知用户上传了图片**。要实现"自动触发"，需要客户端/模型侧配合：

### 触发路径

| 用户行为                    | 客户端上下文              | 模型行为                                   | 工具调用 |
| ----------------------- | ------------------- | -------------------------------------- | ---- |
| 在 opencode 中 `@引用` 一张图片 | 图片绝对路径注入上下文         | 无视觉模型看到路径 → 调用 `ocr_image(path)`       | ✅ 自动 |
| 拖拽图片到会话 / 粘贴截图          | 部分客户端会以 data URI 注入 | 无视觉模型看到 data URI → 调用 `ocr_image(uri)` | ✅ 自动 |
| 用户口述"这是我的截图"并粘贴         | 路径 / data URI 进入上下文 | 同上                                     | ✅ 自动 |

### 推荐的提示词约定（关键）

为确保 100% 触发，在项目根目录的 `AGENTS.md` 或模型系统提示中加入：

```markdown
## 图片处理约定

当用户消息中出现图片路径或 base64 data URI 时：
- 若当前模型具备视觉能力 → 直接读取图片（不要调用本 MCP 服务器）。
- 若当前模型不具备视觉能力 → **必须**调用 `image-recognition` MCP 服务器的 `ocr_image` 或
  `recognize_image` 工具，将识别结果作为图片内容继续处理。
- 工具调用结果已经包含识别出的文字与图像描述，无需再要求用户提供说明。
```

将此约定写进 `AGENTS.md` 后，opencode / Claude Desktop 等客户端会把该指令一并作为系统提示发给模型，实现真正的"自动触发"。

---

## 客户端接入配置

将以下配置中的 **绝对路径** 替换为你本机的项目位置，然后写入对应客户端的配置文件。

### opencode

写入 `opencode.json`（项目级）或 `~/.config/opencode/opencode.json`（用户级）：

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "image-recognition": {
      "type": "local",
      "command": [
        "/path/to/image-recognition-mcp/.venv/bin/python",
        "/path/to/image-recognition-mcp/mcp_server.py"
      ],
      "enabled": true
    }
  }
}
```

重启 opencode 即可在工具列表中看到 `image-recognition` 的 4 个工具。

### Claude Desktop

写入 `~/Library/Application Support/Claude/claude_desktop_config.json`：

```json
{
  "mcpServers": {
    "image-recognition": {
      "command": "/path/to/image-recognition-mcp/.venv/bin/python",
      "args": ["/path/to/image-recognition-mcp/mcp_server.py"]
    }
  }
}
```

### Cursor / Cline / 通用 stdio MCP 客户端

```json
{
  "mcpServers": {
    "image-recognition": {
      "command": "/path/to/image-recognition-mcp/.venv/bin/python",
      "args": ["/path/to/image-recognition-mcp/mcp_server.py"]
    }
  }
}
```

### WorkBuddy

编辑 `~/.workbuddy/mcp.json`，将 `image-recognition` 加入 `mcpServers`，重启后生效：

### WorkBuddy

参考配置示例见 `configs/` 目录：

- `configs/opencode.example.json`
- `configs/claude-desktop.example.json`
- `configs/generic-stdio.example.json`

---

## 性能与资源

| 图片大小            | OCR 耗时（实测 M4 Air）         | 内存峰值    |
| --------------- | ------------------------- | ------- |
| 1200×420 (测试图)  | ~100 ms                   | < 50 MB |
| 1920×1080 (截图)  | 150–300 ms                | ~80 MB  |
| 4096×4096 (4K)  | 400–800 ms                | ~150 MB |
| 8000×8000 (超大图) | 自动缩到 4096px，约 500–1200 ms | ~200 MB |

**优化建议**：

- 已在 `_load_cg_image` 内置 4096px 自动缩略，对绝大多数截图已足够。
- 若识别大量批量图片，可在客户端对多次 `ocr_image` 调用合并为一次 `recognize_image`，减少上下文 token 消耗。
- OCR 选 `level="fast"` 可提速 30–50%，代价是准确率略降（小字、手写体）。

---

## 权限与隐私

- **完全本地**：所有识别在 macOS Vision 框架内完成，**数据完全不出本机**，无需任何 API Key 或网络。
- **屏幕录制权限**（仅 `screenshot_and_recognize` 工具需要）：
  - 首次调用时，macOS 会弹窗或在「系统设置 > 隐私与安全性 > 屏幕录制」中要求授权。
  - 请为 **运行该 MCP 服务器的宿主进程**（如终端、Claude Desktop、opencode）授权。
  - 未授权时工具会返回明确错误信息，不会静默失败。

---

## 故障排查

| 问题                                                               | 原因与解决                                                                     |
| ---------------------------------------------------------------- | ------------------------------------------------------------------------- |
| `ModuleNotFoundError: No module named 'pyobjc.framework.Vision'` | 依赖未安装。在 venv 中执行 `pip install -r requirements.txt`。                       |
| `ModuleNotFoundError: No module named 'mcp.server.fastmcp'`      | mcp<2.0 才使用 `fastmcp`；本项目支持 1.x 和 2.0。如需降级：`pip install 'mcp>=1.2,<2.0'`。 |
| OCR 中文识别为空/乱码                                                    | 检查图片是否清晰；中文图片缩放过小（< 16px 字号）会导致识别失败。可尝试 `level="accurate"` 并增大字号。         |
| 分类结果异常（如对纯文字图返回 "sport"）                                         | Vision 分类对部分场景边界模糊属正常行为；将 `min_confidence` 调高（0.2~0.5）过滤噪声。               |
| `screenshot_and_recognize` 报错"截图失败"                              | 未授权屏幕录制。请到「系统设置 > 隐私与安全性 > 屏幕录制」为宿主 App 授权后重试。                            |
| MCP 客户端连接后工具列表为空                                                 | 检查 `command` 路径是否正确；确认 venv 中的 python 解释器能 `import vision_engine` 成功。     |

---

## 扩展建议

如需添加更多 Vision 能力，可参考 `vision_engine.py` 中现有函数添加对应的 Vision 请求，例如：

- `VNDetectFaceRectanglesRequest` — 人脸检测
- `VNGenerateAttentionBasedSaliencyImageRequest` — 显著性区域
- `VNDetectDocumentSegmentationRequest` — 文档区域分割（扫描类应用）
- `VNRecognizeAnimalsRequest` — 动物品种识别（iOS 15+，macOS 12+）

实现后只需在 `mcp_server.py` 中新增一个 `@mcp.tool()` 即可暴露给模型。

---

## 文件结构

```
image-recognition-mcp/
├── README.md                       # 本文档
├── requirements.txt                # Python 依赖
├── vision_engine.py                # Vision 框架封装（OCR + 分类 + 截图）
├── mcp_server.py                   # MCP 服务器主程序
├── scripts/
│   ├── make_test_image.py          # 生成含中英文的测试图片
│   ├── test_engine.py              # Vision 引擎自测
│   └── test_mcp.py                 # MCP 服务器端到端冒烟测试
├── configs/                        # 客户端配置示例
│   ├── opencode.example.json
│   ├── claude-desktop.example.json
│   └── generic-stdio.example.json
├── sample/
│   └── test_card.png               # 测试图片（含中文/英文/数字/红色圆形）
└── .venv/                          # Python 虚拟环境（运行后生成）
```

## 许可

本项目代码 MIT 协议。Vision 框架调用受 Apple SDK 许可约束，仅可在 macOS 上运行。
