# 本机对 image-recognition-mcp 的修改

上游：https://github.com/carlleilzj/image-recognition-mcp
本地路径：`~/.local/share/image-recognition-mcp`
修改日期：2026-10-27

`git diff` 即本文件所列内容。上游更新时若冲突，按此清单逐项重做。

---

## 1. 多栏阅读顺序（`vision_engine.py` → `order_lines`）

**问题**：原实现只按 `(-y, x)` 排序，两栏/侧边栏/表格布局会左右交错：

```
输入             原输出
LEFT-1 RIGHT-1 → RIGHT-1
LEFT-2 RIGHT-2 → LEFT-1
               → RIGHT-2
```

拼接出的 `text` 不可读，而且不报错 —— 静默产出错误内容。

**修法**：先按纵向重叠聚成「行带」，再找一条所有行带都不跨越的纵向空白带；
若两侧各自成栏（宽度比 ≥ 0.25、栏间距 ≥ 4%）则改为「先左栏读完再右栏」，
否则退回单栏的按行带顺序。

**验证**：两栏 4+4 行 → 左栏 4 行全在前；单栏 4 行顺序不变；
IDE 侧边栏+正文 → 侧栏 6 行在前；表格 → 按列读取不交错。

## 2. 返回体积（`mcp_server.py`）

**问题**：每行都带 `confidence` 和 `bbox`，且 `json.dumps(indent=2)`。
1080p 截图一次调用 ≈ 8,800 token，其中约九成是坐标。

**修法**：新增 `include_bbox`（默认 `false`），默认只返回 `{text}`；`_ok()` 改为紧凑 JSON。

**验证**：同一张截图文本完全一致（105 行），体积 21,992 → 4,941 字符（**-78%**）。

## 3. OCR 档位参数（`mcp_server.py` + `vision_engine.py`）

**问题**：`ocr_image()` 内部有 `level` 参数，README 也在推荐 `level="fast"`，
但 4 个 MCP 工具都没有暴露它。

**修法**：`ocr_image` / `recognize_image` / `screenshot_and_recognize` 增加 `level` 参数，
取 `accurate`（默认）或 `fast`，非法值报错。

**实测代价（docstring 与 README 均已写明）**：

| 图片 | accurate | fast | 准确率 |
|---|---|---|---|
| 测试卡片小字 | 73 ms | 16 ms | 中文变乱码（`图片识别测试` → `a ki/iyilJ jJf,Iil`） |
| 1080p 截图 | 464 ms | 112 ms | 字符重合率仅 36.7%，漏 22 行 |

结论：速度确实约 4 倍，但中文基本不可用。默认值保持 `accurate` 是对的。
**原 README 说“代价是准确率略降”明显低估了**，已一并更正。

## 4. mcp 版本（`requirements.txt` + `mcp_server.py`）

**问题**：README 称支持 mcp 1.x 和 2.0，但代码 `from mcp.server.mcpserver import MCPServer`
只在 2.x 存在（核对过 1.9.0 的 wheel：只有 `mcp/server/fastmcp/`，没有 `mcpserver.py`）。

**修法**：`requirements.txt` 锁 `mcp>=2.3.0`；代码加 `try/except` 双路径导入。

## 5. 临时文件泄漏（`vision_engine.py`）

**问题**：data URI 解码写到 `/tmp/wb_mcp_img_*.png` 后从不清理。

**修法**：登记到 `_TEMP_FILES`，每次工具调用结束用 `remove_temp_file(path)` 只删本次调用
产生的文件（并发安全）。返回的 `image` 字段在临时文件已删时显示 `"data-uri"`，
不再报告一个不存在的路径。

**验证**：data URI 调用后临时目录新增文件数 = 0。

## 6. 噪声误删（`vision_engine.py` → `is_noise_line`）

**问题**：规则「单字符 + 置信度 < 0.4 → 噪声」会误删真实内容，且行为不可预测
（同一张图里 `A` 因置信度 0.5 保留、`B` 因 0.3 被删）。

**修法**：规则 4 增加条件 —— 只有该字符**既不是字母数字也不是汉字**时才判噪声。
真内容单字符（答案表、坐标、编号）不再被删。

**验证**：`A B C 7 9 X` 全部保留，只剩 `。` 判为噪声。

## 7. 参数校验 + 分类去重

- `min_confidence` 校验 0~1，越界/非数字直接报错（原来 `1.5` 和 `-1` 都被静默接受）
- `describe_image` 新增 `dedupe`（默认 true）：置信度相同/近似的标签视为同一判定结果只留一个。
  Vision 对纯文字图会返回 `recreation` / `sport` / `ping_pong` 三个 0.686 的同分标签，
  去重后只留 `recreation`，而 `document(0.53)`、`screenshot(0.07)` 这类真实差异仍保留

## 顺带修的

- data URI 落盘按魔数选后缀（JPEG 不再写成 `.png`）
- 裸 base64 兜底支持 PNG/JPEG/GIF/BMP/TIFF/WebP（原来只认 PNG）
- `analyze_image` 透传 `level`
- README：更正 opencode 配置为 **V2 格式**（`mcp.servers` + `disabled`，原来的
  `mcp.<name>` + `enabled` 在 V2 下不生效）、更正 `level` 的真实代价、补充新参数说明、
  修掉重复的 `### WorkBuddy` 标题
- `configs/opencode.example.json`：同步为 V2 格式，路径改成占位符

---

## 兼容性

新增参数全部有默认值，**旧调用方式行为不变**，除了以下两处刻意的行为修正：

1. 默认不再返回 `bbox` / `confidence` —— 需要时传 `include_bbox: true`
2. `describe_image` 默认合并同分标签 —— 需要原始全集时传 `dedupe: false`

---

## 回归测试

```sh
cd ~/.local/share/image-recognition-mcp
./.venv/bin/python -m py_compile vision_engine.py mcp_server.py
./.venv/bin/python scripts/test_engine.py sample/test_card.png   # 上游自带，全部通过
```

注意：**改完必须重启 OpenCode 后台服务**（`opencode service restart`），
MCP 服务器是常驻进程，不会热重载。
