"""地图包：把画框内的 OSM 要素压成稿那套格式。

产出的字典可以**直接喂给 `reference/citymap.js` 的 `makeScene`**——这是有意的：
「用参考实现把生成的包画出来与稿肉眼一致」是 S0 的验收项，两边的字段名因此必须一样。
manifest 的几列（`cityID`、版本、内容 hash、来源）并排放在同一层，不另起一层嵌套。
"""

from __future__ import annotations

import hashlib
import json

from . import codec
from .geometry import (Projection, assemble_polygons, clip_polyline, clip_ring,
                       ring_area, simplify, stitch_rings)

# 四档道路。分档看的是「这条路在一张城市图上该有多粗」，不是 OSM 的功能分类本身：
# 一档是穿城的骨架，二档是区与区之间，三档是住得进人的街，四档是前三档之外还画得出来的
# 细纹——辅路、小径、步道、台阶都在这一档。稿的 `roadW` / `minPx` 四个数一一对应这四档。
#
# 两条留给产品拍（S0 只把它们摆出来，见 README）：`pedestrian`（步行街）落在第四档，
# 而参考实现里第四档是可以整层关掉的 `paths` 图层——一个记录散步的产品把南京东路放进
# 「可关」的那层，方向是反的。另外 `footway=sidewalk` 会给每条街配一对平行线，
# 在 5.7 米一像素下人行道离车道只有半个像素，等于把每条路描粗一倍；
# Amsterdam 第四档 18931 条、波特兰 16475 条，多半有相当比例是人行道。
ROAD_TIERS = {
    "motorway": 1, "trunk": 1, "primary": 1,
    "motorway_link": 1, "trunk_link": 1, "primary_link": 1,
    "secondary": 2, "tertiary": 2, "secondary_link": 2, "tertiary_link": 2,
    "residential": 3, "unclassified": 3, "living_street": 3,
    "service": 4, "footway": 4, "path": 4, "steps": 4, "cycleway": 4, "track": 4, "pedestrian": 4,
}

# 线状水系的画宽（米）。稿的 `waterLines` 里 `w` 就是这个数。
WATER_WIDTHS = {"river": 8, "canal": 6, "stream": 3}

WATER_AREA_TAGS = (("natural", "water"), ("landuse", "reservoir"), ("landuse", "basin"))
GREEN_AREA_TAGS = (
    ("leisure", "park"), ("leisure", "garden"), ("leisure", "nature_reserve"),
    ("landuse", "forest"), ("landuse", "grass"), ("landuse", "meadow"),
    ("landuse", "recreation_ground"), ("landuse", "village_green"),
    ("landuse", "cemetery"), ("landuse", "allotments"),
    ("natural", "wood"), ("natural", "scrub"), ("natural", "grassland"), ("natural", "heath"),
)

# 简化容差（米）。一米是量化步长，容差取它的两到四倍：
# 骨架路留得细，末梢和面状要素可以粗——它们在图上本来就只占一两个像素。
TOLERANCE = {1: 2.0, 2: 2.5, 3: 3.0, 4: 4.0, "line": 3.0, "area": 3.0}
MIN_AREA = 400.0  # 平方米。比这更小的绿地水面在图上不到一个像素，留着只是体积。


def covers(snapshot: dict, bounds) -> bool:
    """快照是否盖住这个经纬画框。这是 `build` 的前置：拿按旧画框取的要素去裁新画框，
    结果是一张边上缺一条的图，不报错、不好看出来，所以在这里拦。"""
    lons: list[float] = []
    lats: list[float] = []
    for element in snapshot.get("elements", ()):
        for node in element.get("geometry", ()) or ():
            if node:
                lons.append(node["lon"])
                lats.append(node["lat"])
    if not lons:
        return False
    return min(lons) <= bounds[0] and min(lats) <= bounds[1] and max(lons) >= bounds[2] and max(lats) >= bounds[3]


def build(snapshot: dict, *, name: str, name_local: str, center, frame) -> dict:
    """`center` 是 `(lon, lat)`，`frame` 是 `(宽米, 高米)`。返回可直接 JSON 序列化的包。"""
    projection = Projection(*center)
    half_w, half_h = frame[0] / 2.0, frame[1] / 2.0
    box = (-half_w, -half_h, half_w, half_h)

    roads: dict[str, list[list[int]]] = {"1": [], "2": [], "3": [], "4": []}
    rail: list[list[int]] = []
    water_lines: list[dict] = []
    water_areas: list[dict] = []
    green_areas: list[dict] = []
    coastlines: list[list[int]] = []

    for element in snapshot.get("elements", ()):
        if element.get("type") not in ("way", "relation"):
            continue
        tags = element.get("tags", {})
        if element["type"] == "way" and "geometry" in element:
            points = [projection.forward(node["lon"], node["lat"]) for node in element["geometry"]]
        else:
            points = []

        highway = tags.get("highway")
        if highway in ROAD_TIERS and points:
            tier = ROAD_TIERS[highway]
            _add_lines(roads[str(tier)], points, box, TOLERANCE[tier])
            continue
        if tags.get("railway") and points:
            _add_lines(rail, points, box, TOLERANCE["line"])
            continue
        if tags.get("natural") == "coastline" and points:
            _add_lines(coastlines, points, box, TOLERANCE["line"])
            continue
        waterway = tags.get("waterway")
        if waterway in WATER_WIDTHS and points:
            segments: list[list[int]] = []
            _add_lines(segments, points, box, TOLERANCE["line"])
            water_lines += [{"w": WATER_WIDTHS[waterway], "p": segment} for segment in segments]
            continue

        target = None
        if any(tags.get(key) == value for key, value in WATER_AREA_TAGS):
            target = water_areas
        elif any(tags.get(key) == value for key, value in GREEN_AREA_TAGS):
            target = green_areas
        if target is None:
            continue
        for polygon in _polygons(element, projection):
            encoded = _encode_polygon(polygon, box)
            if encoded:
                target.append(encoded)

    package = {
        "name": name,
        "nameLocal": name_local,
        "center": [round(center[0], 6), round(center[1], 6)],
        "frame": [int(frame[0]), int(frame[1])],
        "roads": roads,
        "rail": rail,
        "water": water_areas,
        "waterLines": water_lines,
        "green": green_areas,
    }
    if coastlines:
        package["coastline"] = coastlines
    return package


def _add_lines(sink: list[list[int]], points, box, tolerance: float) -> None:
    for piece in clip_polyline(points, box):
        flat = codec.encode(simplify(piece, tolerance))
        if flat:
            sink.append(flat)


def _polygons(element: dict, projection: Projection) -> list[dict]:
    """way 的闭合环、或 relation 的 outer/inner 成员，统一成 `{"o": 外环, "i": [内环…]}`。

    关系的成员是一段一段的弧，接环与内环归属走 `geometry` 里那一份实现——
    和行政边界同一件事，不该有第二份。两件事都不能省：一段没接上的弧当成环画出去，
    渲染时被 `closePath()` 用一根弦封口，图上是一大块实心色；一个内环挂到两个外环上，
    even-odd 填充会把洞填回去。
    """
    if element["type"] == "way":
        ring = [projection.forward(node["lon"], node["lat"]) for node in element.get("geometry", ())]
        if len(ring) < 4 or ring[0] != ring[-1]:
            return []
        return [{"o": ring[:-1], "i": []}]

    outer_ways, inner_ways = [], []
    for member in element.get("members", ()):
        if member.get("type") != "way" or "geometry" not in member:
            continue
        way = [projection.forward(node["lon"], node["lat"]) for node in member["geometry"]]
        (inner_ways if member.get("role") == "inner" else outer_ways).append(way)
    outer, _ = stitch_rings(outer_ways)
    inner, _ = stitch_rings(inner_ways)
    # 底图上接不上的弧段丢了就丢了——它在图上只占一两个像素，不像行政边界那样会改答案，
    # 所以这里不吵。行政边界那边会吵（`boundary.parse`）。
    return assemble_polygons(outer, inner)


def _encode_polygon(polygon: dict, box) -> dict | None:
    outer = clip_ring(polygon["o"], box)
    if len(outer) < 4:
        return None
    outer = simplify(outer + outer[:1], TOLERANCE["area"])[:-1]
    if len(outer) < 4 or ring_area(outer) < MIN_AREA:
        return None
    holes = []
    for hole in polygon["i"]:
        clipped = clip_ring(hole, box)
        if len(clipped) < 4:
            continue
        reduced = simplify(clipped + clipped[:1], TOLERANCE["area"])[:-1]
        if len(reduced) >= 4 and ring_area(reduced) >= MIN_AREA:
            encoded = codec.encode_ring(reduced)
            if encoded:
                holes.append(encoded)
    encoded_outer = codec.encode_ring(outer)
    return {"o": encoded_outer, "i": holes} if encoded_outer else None


def content_hash(package: dict) -> str:
    """内容 hash 只覆盖矢量与画框，不覆盖版本号与生成时间——
    否则每次重跑都得到新 hash，「同一输入产出同一个包」这句话就没法验。"""
    payload = {key: package[key] for key in
               ("name", "nameLocal", "center", "frame", "roads", "rail", "water", "waterLines", "green")
               if key in package}
    if "coastline" in package:
        payload["coastline"] = package["coastline"]
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def stats(package: dict) -> dict:
    def points(lines):
        return sum(len(line) // 2 for line in lines)

    def polygon_points(polygons):
        return sum(len(polygon["o"]) // 2 + sum(len(hole) // 2 for hole in polygon["i"]) for polygon in polygons)

    return {
        "roads": {tier: {"lines": len(lines), "points": points(lines)} for tier, lines in package["roads"].items()},
        "rail": {"lines": len(package["rail"]), "points": points(package["rail"])},
        "water": {"polygons": len(package["water"]), "points": polygon_points(package["water"])},
        "waterLines": {"lines": len(package["waterLines"]),
                       "points": points([line["p"] for line in package["waterLines"]])},
        "green": {"polygons": len(package["green"]), "points": polygon_points(package["green"])},
    }
