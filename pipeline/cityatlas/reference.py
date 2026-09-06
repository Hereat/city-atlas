"""从设计稿 HTML 解包出 `CityMap` 参考实现。

稿（`docs/散步/demo/2026-09-04-散步城市图 · 优化稿（离线）.html`）是 bundler 打包件：
正文 JSON 字串化在 `<script type="__bundler/template">` 里，资源在 `__bundler/manifest` 里，
每个资源是 gzip + base64。引擎与两座城的矢量数据是其中最大的那份 JS。

解出来的东西是**格式与渲染的参考实现**，不是产品代码：S1 的 Swift 渲染器逐行对着它写，
S0 用 `reference/preview.html` 把管线新生成的包画出来跟稿比。
"""

from __future__ import annotations

import base64
import gzip
import json
import re
from pathlib import Path

_MARKER = '<script type="__bundler/%s"'


def _segment(html: str, kind: str) -> str:
    start = html.index(_MARKER % kind)
    start = html.index(">", start) + 1
    return html[start:html.index("</script>", start)]


def _largest_javascript(html: str) -> str:
    manifest = json.loads(_segment(html, "manifest"))
    best = ""
    for asset in manifest.values():
        if "javascript" not in asset["mime"]:
            continue
        raw = base64.b64decode(asset["data"])
        if asset.get("compressed"):
            raw = gzip.decompress(raw)
        text = raw.decode("utf-8")
        if "window.CityMap" in text and len(text) > len(best):
            best = text
    if not best:
        raise SystemExit("稿里没有找到 CityMap 引擎")
    return best


def _object_after(source: str, needle: str) -> tuple[int, int]:
    """返回 `needle` 之后第一个 `{…}` 的起止下标（含）。按引号状态配平花括号。"""
    start = source.index("{", source.index(needle))
    depth, i, in_string = 0, start, False
    while True:
        ch = source[i]
        if in_string:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return start, i
        i += 1


def unpack(html_path: Path, out_dir: Path) -> dict[str, Path]:
    source = _largest_javascript(html_path.read_text(encoding="utf-8"))
    (out_dir / "cities").mkdir(parents=True, exist_ok=True)

    written: dict[str, Path] = {}
    cuts: list[tuple[int, int]] = []
    for const, slug in (("CITY_SH", "shanghai"), ("CITY_HZ", "hangzhou")):
        decl = source.index("const %s = " % const)
        head, tail = _object_after(source, "const %s = " % const)
        city = json.loads(source[head:tail + 1])
        path = out_dir / "cities" / f"{slug}.json"
        path.write_text(json.dumps(city, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
        written[slug] = path
        cuts.append((decl, source.index(";", tail) + 1))

    # 引擎里把两整句 `const CITY_XX = {…};` 抽走，数据改由调用方注入
    engine = source
    for start, end in sorted(cuts, reverse=True):
        engine = engine[:start] + engine[end:]

    # 末尾的导出表引用了刚抽走的两个常量，换成空表：数据从 `preview.html` 注入
    engine = engine.replace("CITIES: { sh: CITY_SH, hz: CITY_HZ },", "CITIES: {},")
    engine = engine.replace("SCENES: { sh: makeScene(CITY_SH), hz: makeScene(CITY_HZ) },", "SCENES: {},")
    engine = re.sub(r"\n{3,}", "\n\n", engine.lstrip("\n"))

    header = (
        "// 稿的 `CityMap` 引擎，解包自 docs/散步/demo/2026-09-04-散步城市图 · 优化稿（离线）.html。\n"
        "// 由 `python3 -m cityatlas reference` 生成，除以下两处外逐字未改：\n"
        "//   1. 抽掉内联的 `const CITY_SH` / `const CITY_HZ`，数据改存 reference/cities/*.json；\n"
        "//   2. 导出表里的 `CITIES` / `SCENES` 因此变成空对象，由调用方 `CityMap.makeScene(json)` 现造。\n"
        "// 这是格式与渲染的参考实现：S1 的 Swift 渲染器对着 `render` / `drawInk` 写，\n"
        "// S0 用 reference/preview.html 把新生成的包画出来与稿比对。别在这里改产品行为。\n\n"
    )
    engine_path = out_dir / "citymap.js"
    engine_path.write_text(header + engine, encoding="utf-8")
    written["engine"] = engine_path

    styles_head, styles_tail = source.index("const STYLES = ["), source.index("\n];", source.index("const STYLES = ["))
    styles_path = out_dir / "styles.js"
    styles_path.write_text(
        "// 稿的八套样式表，原样摘出（产品取其中四套，见实施文档拍板 13）。\n"
        + source[styles_head:styles_tail + 3] + "\n",
        encoding="utf-8",
    )
    written["styles"] = styles_path
    return written
