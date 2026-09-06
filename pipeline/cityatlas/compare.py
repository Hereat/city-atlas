"""并排比对页：稿的那张图 vs 管线生成的包，同一个渲染器、同一个像素尺寸。

S0 的验收里有一条「用参考实现把生成的包画出来与稿的上海图肉眼一致」。
肉眼一致是人来判的，所以这里产出一个自带数据、双击就能看的 HTML，交给拍板的人。
稿那份带 201 条合成散步，管线的包没有散步——比的是底图，墨迹层在两边都关掉。
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

from . import MAP_DATA_VERSION

TEMPLATE = """<!doctype html>
<meta charset="utf-8">
<title>CityAtlas · %(title)s</title>
<style>
 body { font: 14px/1.6 -apple-system, "PingFang SC", sans-serif; margin: 0; padding: 28px;
        background: #F3F1EC; color: #2E3A4D; }
 h1 { font-size: 16px; font-weight: 600; margin: 0 0 4px; }
 p  { color: #6B7280; margin: 0 0 22px; }
 .row { display: flex; gap: 24px; flex-wrap: wrap; }
 figure { margin: 0; }
 figcaption { text-align: center; color: #6B7280; padding-top: 8px; }
 canvas { border-radius: 12px; box-shadow: 0 1px 3px rgba(0,0,0,.12); display: block; }
</style>
<h1>%(title)s</h1>
<p>同一个渲染器、同一个像素尺寸（353 × 462 @3x）。墨迹层两边都关掉，比的是底图。</p>
<div class="row" id="row"></div>
<script>%(engine)s</script>
<script>
const CITIES = %(cities)s;
const style = CityMap.styleById[%(style)s];
const row = document.getElementById('row');
for (const [label, city] of CITIES) {
  const scene = CityMap.makeScene({ ...city, walks: [], snapped: [] });
  const canvas = document.createElement('canvas');
  canvas.style.width = '353px'; canvas.style.height = '462px';
  CityMap.render(canvas, 353 * 3, 462 * 3, style,
                 { green: true, rail: true, paths: true, band: false, track: 'free', count: 0 }, scene);
  const figure = document.createElement('figure');
  const caption = document.createElement('figcaption');
  caption.textContent = `${label} · ${city.frame[0]} × ${city.frame[1]} m · ${city.center.join(', ')}`;
  figure.append(canvas, caption);
  row.append(figure);
}
</script>
"""


def write(root: Path, slug: str, city_id: str, style: str = "plain") -> Path:
    engine = (root / "reference" / "citymap.js").read_text(encoding="utf-8")
    drafted = json.loads((root / "reference" / "cities" / f"{slug}.json").read_text(encoding="utf-8"))
    package_path = root / "out" / "package" / city_id / f"{MAP_DATA_VERSION}.json.gz"
    with gzip.open(package_path, "rt", encoding="utf-8") as handle:
        built = json.load(handle)
    pairs = [["稿", _thin(drafted)], ["管线", _thin(built)]]
    page = TEMPLATE % {
        "title": f"{built['name']} · 稿与管线并排",
        "engine": engine,
        "cities": json.dumps(pairs, ensure_ascii=False, separators=(",", ":")),
        "style": json.dumps(style),
    }
    out = root / "out" / f"compare-{slug}.html"
    out.write_text(page, encoding="utf-8")
    return out


def _thin(city: dict) -> dict:
    """只留渲染要用的层，扔掉合成散步与 manifest——比对页不该有五十万字的无关数据。"""
    keep = ("name", "center", "frame", "roads", "rail", "water", "waterLines", "green")
    return {key: city[key] for key in keep if key in city}
