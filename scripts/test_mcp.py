#!/usr/bin/env python3
"""MCP 服务器端到端冒烟测试：连接 stdio 服务器，列出工具并实际调用。

用法: python scripts/test_mcp.py [图片路径]
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


async def main() -> None:
    img = sys.argv[1] if len(sys.argv) > 1 else "sample/test_card.png"
    img = os.path.abspath(os.path.join(ROOT, img))

    params = StdioServerParameters(
        command=sys.executable,
        args=[os.path.join(ROOT, "mcp_server.py")],
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            print("== MCP 工具列表 ==")
            tools = await session.list_tools()
            for t in tools.tools:
                print(f"  - {t.name}: {t.description.splitlines()[0]}")
            assert any(t.name == "ocr_image" for t in tools.tools), "缺少 ocr_image 工具"

            print(f"\n== 调用 ocr_image({img}) ==")
            res = await session.call_tool("ocr_image", {"image": img})
            text = res.content[0].text if res.content else "（无输出）"
            parsed = json.loads(text)
            print(f"  status={parsed['status']} count={parsed.get('count')}")
            for line in parsed.get("lines", []):
                print(f"    - {line['text']!r}  (conf={line['confidence']})")
            assert parsed["status"] == "ok", f"OCR 调用失败: {text}"

            print("\n== 调用 recognize_image ==")
            res = await session.call_tool("recognize_image", {"image": img})
            text = res.content[0].text if res.content else "（无输出）"
            parsed = json.loads(text)
            print(f"  status={parsed['status']} summary={parsed['summary'][:120]!r}")

            print("\n== 调用 describe_image ==")
            res = await session.call_tool("describe_image", {"image": img})
            text = res.content[0].text if res.content else "（无输出）"
            parsed = json.loads(text)
            print(f"  status={parsed['status']} labels={parsed.get('labels')}")

            print("\n== 错误处理：传入不存在的路径 ==")
            res = await session.call_tool("ocr_image", {"image": "/no/such/file.png"})
            text = res.content[0].text if res.content else "（无输出）"
            parsed = json.loads(text)
            print(f"  status={parsed['status']} error={parsed.get('error')}")
            assert parsed["status"] == "error", "错误场景应返回 status=error"

            print("\n端到端冒烟测试全部通过 ✅")


if __name__ == "__main__":
    asyncio.run(main())
