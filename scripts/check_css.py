#!/usr/bin/env python3
"""CSS 结构校验：括号平衡 + 紧邻重复选择器检测。

背景：脚本化修改 style.css 时曾多次出现「X {.X {」紧邻重复选择器，
导致浏览器解析错乱、面板样式整体失效（如 .history-item / .panel-head）。
本脚本在提交前/修改后运行，快速拦截这类结构错误。

用法：python3 scripts/check_css.py [文件...]（缺省检查 webapp/static/style.css）
"""

import pathlib
import re
import sys

# 同一行内紧邻重复选择器：`.xxx {.xxx {`（媒体查询内嵌是换行缩进，不会误报）
_DUP_SELECTOR = re.compile(r"\{(\.[a-zA-Z][\w-]*)\s*\{")


def check_css(path: str) -> list:
    src = pathlib.Path(path).read_text(encoding="utf-8")
    errors = []

    # 1) 花括号平衡（粗检）
    opens = src.count("{")
    closes = src.count("}")
    if opens != closes:
        errors.append(f"花括号不平衡：{{ x{opens} vs }} x{closes}")

    # 2) 同一行紧邻重复选择器
    for m in _DUP_SELECTOR.finditer(src):
        line_no = src[: m.start()].count("\n") + 1
        errors.append(f"第 {line_no} 行疑似选择器重复「{m.group(1)}」（前一个块未闭合）")

    # 3) 重复选择器块：选择器名在同一文件出现多次（helper 提示，容忍动画/媒体复用）
    selectors = re.findall(r"^(\.[a-zA-Z][\w-]*)\s*\{", src, re.M)
    seen = {}
    for s in selectors:
        seen[s] = seen.get(s, 0) + 1
    for s, n in sorted(seen.items()):
        if n > 1:
            errors.append(f"选择器「{s}」出现 {n} 次（可能为覆盖/动画，请确认是否有意）")

    return errors


def main() -> int:
    files = sys.argv[1:] or ["webapp/static/style.css"]
    failed = False
    for f in files:
        errs = check_css(f)
        if errs:
            failed = True
            print(f"❌ {f}")
            for e in errs:
                print("  -", e)
        else:
            print(f"✅ {f} 结构检查通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
