"""地图包：把画框内的 OSM 要素压成稿那套格式。

产出的字典可以**直接喂给 `reference/citymap.js` 的 `makeScene`**——这是有意的：
「用参考实现把生成的包画出来与稿肉眼一致」是 S0 的验收项，两边的字段名因此必须一样。
manifest 的几列（`cityID`、版本、内容 hash、来源）并排放在同一层，不另起一层嵌套。
"""

from __future__ import annotations

import hashlib
import json

from . import codec
from .geometry import (Projection, assemble_polygons, bounds, boxes_overlap,
                       clip_polyline, clip_polyline_outside, clip_ring,
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

# 画进图里的轨道类型。**白名单而不是黑名单**，与 `ROAD_TIERS` 同一个形状：
# 判据是「这条轨道今天有车在跑」，能穷举的是跑车的那几类，不是不跑车的那些。
#
# 先前收的是「任何带 railway 标签的 way」，于是废线、停用线、在建线全画进了图：
# 上海画框内这一批占轨道总长的两成（1230 km / 6169 km，未裁边口径；1991 年就停用的
# 沪杭线、废弃货线都在里面），关东 925 条里也是绝大多数。黑名单挡不干净——OSM 里
# 表达「不再使用」的写法有 `railway=abandoned|disused|razed|proposed|construction`
# 和 `abandoned:railway=rail` 两套，后者的 `railway` 键根本不存在。
#
# `platform`（站台）、`station`（站房）也随黑名单一起挡掉了：它们是面或点，
# 描成线会在站场那里糊成一片。
RAILWAY_KINDS = frozenset({"rail", "subway", "light_rail", "tram", "monorail",
                           "narrow_gauge", "funicular"})

# 线状水系的画宽（米）。稿的 `waterLines` 里 `w` 就是这个数。
WATER_WIDTHS = {"river": 8, "canal": 6, "stream": 3}

WATER_AREA_TAGS = (("natural", "water"), ("landuse", "reservoir"), ("landuse", "basin"))

# 静水面：水线画进这几类面里的那一段裁掉（`_trim_lines_in_still_water`）。
# 判据是「这块水面里再画一条中心线是多余的」——湖、塘、水库是静的，一条线穿过去
# 说不出任何水在哪里之外的事；河与运河**不参与**，宽河中间那道线是现在的画法
# （`WATER_WIDTHS`），色表冻结着，不顺手改。
# `landuse` 那两个值不是补全，是那两个标签本身的意思就是一潭静水。
STILL_WATER_TAGS = (("water", "lake"), ("water", "pond"), ("water", "reservoir"),
                    ("landuse", "reservoir"), ("landuse", "basin"))
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

    # 编码之后同一块面只留一份，见 `_keep_area`。键是**外环**，不含内环。
    areas_by_outer: dict[bytes, tuple[list, int]] = {}

    roads: dict[str, list[list[int]]] = {"1": [], "2": [], "3": [], "4": []}
    rail: list[list[int]] = []
    water_lines: list[dict] = []
    water_areas: list[dict] = []
    still_outers: set[bytes] = set()       # 湖 / 塘 / 水库的外环，水线按它们裁
    green_areas: list[dict] = []
    coastlines: list[list[int]] = []       # 海面的环，一环一条，绕向即角色

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
        if tags.get("railway") in RAILWAY_KINDS and points:
            _add_lines(rail, points, box, TOLERANCE["line"])
            continue
        if tags.get("natural") == "coastline" and points:
            # 海面是**面**，不是线：一环一份进 `coastline`，**绕向就是这一环的角色**
            # （顺时针是海、逆时针是岛，摆正在 `overture._sea_rings`）。渲染器按 nonzero
            # 填，岛在海里自然成洞、岛中的湖又自然填回水，App 侧不必再接链、去重、
            # 沿画框围。裁剪是保向的（Sutherland–Hodgman 按原顺序走），所以裁到画框
            # 之后绕向仍然作数——`selftest` 有一条盯着这件事。
            encoded = _encode_polygon({"o": points, "i": []}, box)
            if encoded:
                coastlines.append(encoded["o"])
            continue
        waterway = tags.get("waterway")
        if waterway in WATER_WIDTHS and points:
            segments: list[list[int]] = []
            _add_lines(segments, points, box, TOLERANCE["line"])
            water_lines += [{"w": WATER_WIDTHS[waterway], "p": segment} for segment in segments]
            continue

        target = None
        if _matches(tags, WATER_AREA_TAGS):
            target = water_areas
        elif _matches(tags, GREEN_AREA_TAGS):
            target = green_areas
        if target is None:
            continue
        still = target is water_areas and _matches(tags, STILL_WATER_TAGS)
        for polygon in _polygons(element, projection):
            encoded = _encode_polygon(polygon, box)
            if encoded:
                _keep_area(areas_by_outer, target, encoded)
                if still:
                    # 记外环、不记这一份面：同一个湖画两遍（一遍带湖心岛、一遍没画）时，
                    # `_keep_area` 留的是带岛那份，而裁线必须用**画出来的那份**——
                    # 拿没岛那份去裁，岛上那截河道会跟着湖面一起被裁掉。
                    still_outers.add(_outer_key(encoded))

    # 水面要等整趟遍历跑完才齐全，所以这一步不能放进循环里
    still_water = [area for area in water_areas if _outer_key(area) in still_outers]
    water_lines = _trim_lines_in_still_water(water_lines, still_water)

    # **落盘前把每一层排序。** 「同一输入重跑逐字节相同」是 S0 的验收项，而
    # `content_hash` 是顺序敏感的：上游按城取要素那一趟没有稳定行序（DuckDB 并行扫描，
    # 关掉行序保持才排得动几千万行），同一份 `cut.parquet` 重出上海，`roads` 与 `rail`
    # 会是「顺序不同、内容逐条相同」，hash 于是每次都变。排一次就把上游的顺序问题
    # 挡在产物之外，图上没有区别——同一层里线与线、面与面同色同宽，画的先后不改变结果。
    package = {
        "name": name,
        "nameLocal": name_local,
        "center": [round(center[0], 6), round(center[1], 6)],
        "frame": [int(frame[0]), int(frame[1])],
        "roads": {tier: sorted(lines) for tier, lines in roads.items()},
        "rail": sorted(rail),
        "water": sorted(water_areas, key=_area_order),
        "waterLines": sorted(water_lines, key=lambda line: (line["p"], line["w"])),
        "green": sorted(green_areas, key=_area_order),
    }
    if coastlines:
        package["coastline"] = sorted(coastlines)
    return package


def _matches(tags: dict, table) -> bool:
    """这一份 tags 落在那张表里吗。三张表（水面、绿地、静水面）同一个形状、同一个问法。"""
    return any(tags.get(key) == value for key, value in table)


def _outer_key(area: dict) -> bytes:
    """一块面的身份就是它的**外环**，不含内环。`_keep_area` 的去重与裁线时「这块面是不是
    静水」都按它认，所以只有这一处定义。"""
    return hashlib.sha1(repr(area["o"]).encode()).digest()


def _area_order(area: dict):
    return area["o"], area["i"]


def _trim_lines_in_still_water(water_lines: list[dict], still_water: list[dict]) -> list[dict]:
    """水线落在静水面里的那一段裁掉，岸上的那段留着。

    河流的中心线在 OSM 里常常一路画进湖里，而湖本身另有一块水面。画出来就是湖面上
    一把放射状细线——用户在大津市那张图上看到的就是它。日本尤其密集：`waterway` 来自
    2006 年 KSJ2（国土数値情報 河川）那次导入，一条河跨度过一公里半却只有两三个点，
    十条汇到湖面上同一个节点（高岛市那边一个节点汇了十四条），全国 110 座城 244 条。

    **判据先前是「两端都在水面里」，在大津 0 比 95 一条都没挡住。** 真实的失败形状是
    一端在岸上、一端伸进湖里，而按端点判只有两种写法，都不对：要求两端都在水里等于
    要求整条线泡在湖里（一条都挡不住），改成「有一端在水里就整条删」又会把河口那段
    正常的岸上河道一起删掉。按面裁没有这个取舍——**该断的地方就是湖岸**，湖盖住的
    那段消失，岸上那段一米不动。

    裁的空间是编码之后的整数米，与水面同一套坐标：湖岸是包里那个简化过的环，
    裁点落在它上面，而不是落在一条盘上没有的原始岸线上。
    """
    if not water_lines or not still_water:
        return water_lines
    lakes = []
    for area in still_water:
        polygon = {"o": codec.decode(area["o"]), "i": [codec.decode(hole) for hole in area.get("i", ())]}
        lakes.append((bounds(polygon["o"]), polygon))

    kept = []
    for line in water_lines:
        points = codec.decode(line["p"])
        # 外接框先筛一遍。全国大多数水线离任何湖都很远，这一刀让它们连一次射线法都不用跑，
        # 也不必重新编码（原样留着的那条线字节不变）。这不是过早优化：不筛就是
        # 「线数 × 湖数」对里每一对都跑一遍求交与射线法，从一分钟变成几小时。
        line_box = bounds(points)
        near = [polygon for box, polygon in lakes if boxes_overlap(box, line_box)]
        if not near:
            kept.append(line)
            continue
        for piece in clip_polyline_outside(points, near):
            flat = codec.encode(piece)
            if flat:
                kept.append({"w": line["w"], "p": flat})
    return kept


def _keep_area(seen: dict, target: list, encoded: dict) -> None:
    """同一块面只留一份，**按外环认**；同一个外环出现两次时留内环多的那份。

    去重必须在编码之后，不能在原始坐标上做：OSM 里同一段河岸常被画成两条几乎重合、
    顶点却不完全相同的线，原始坐标上一个重复都没有；经过简化（容差几米）与整数量化
    之后，两者塌成完全相同的环（2026-09-07 重庆实测：原始 475 个水面要素零重复，
    出包后 22 个外环两两相同）。

    为什么非去不可：渲染器按 even-odd 填充，两个完全重叠的多边形互相抵消，
    那段河于是被填成纸色——长江在图上是一条白带（用户在截图里指出来的就是它）。

    只认外环、不认内环：同一个水塘常被画两遍，一遍画了里面的小岛一遍没画
    （广州、上海各有几处）。两份的外环相同，内环不同——按整份去重抓不到，
    而它们在 even-odd 下照样互相抵消。留内环多的那份，岛才不会丢。

    记的是「这个外环放进了**哪个 target 的第几位**」，不能只记对象本身：水面与绿地是
    两个列表，同一个外环可能先作为绿地进来、再作为水面出现，那时拿着绿地那份去水面
    列表里找位置会直接抛错（2026-09-07 全国出包跑到第 577 座炸在这儿）。
    """
    key = _outer_key(encoded)
    holes = len(encoded.get("i", []))
    previous = seen.get(key)
    if previous is None:
        seen[key] = (target, len(target), holes)
        target.append(encoded)
        return
    kept_target, index, kept_holes = previous
    if holes > kept_holes:
        kept_target[index] = encoded
        seen[key] = (kept_target, index, holes)


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
