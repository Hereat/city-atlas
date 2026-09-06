"""行政边界：从 Overpass 的关系里拼出环，取名字与城市中心点。

边界回答的是「这次散步算哪座城」——App 拿到分片后在本地做点在多边形内的判断，
所以这里产出的必须是**闭合的经纬环**，而不是一堆线段。
OSM 的边界关系是若干条 way 的集合，顺序与方向都不保证，得自己接。

名字按 PRD：`name:zh` 优先，没有退回 `name`；本地名单独留一列（列表里中文名下面那行）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .geometry import Ring, assemble_polygons, bounds, point_in_polygon, simplify, stitch_rings


@dataclass
class Boundary:
    osm_relation: int
    admin_level: int
    name_zh: str
    name_local: str
    polygons: list[dict] = field(default_factory=list)   # {"o": Ring, "i": [Ring…]}
    centre: tuple[float, float] | None = None            # admin_centre 节点

    @property
    def bbox(self):
        return bounds([point for polygon in self.polygons for point in polygon["o"]])

    def contains(self, point) -> bool:
        return any(point_in_polygon(point, polygon) for polygon in self.polygons)


def parse(element: dict) -> Boundary:
    tags = element.get("tags", {})
    name = tags.get("name", "")
    boundary = Boundary(
        osm_relation=element["id"],
        admin_level=int(tags.get("admin_level", 0)),
        name_zh=_chinese_name(tags) or name,
        name_local=name,
    )
    outer_ways: list[Ring] = []
    inner_ways: list[Ring] = []
    for member in element.get("members", ()):
        if member["type"] == "node" and member.get("role") == "admin_centre":
            boundary.centre = (member["lon"], member["lat"])
        elif member["type"] == "way" and "geometry" in member:
            way = [(point["lon"], point["lat"]) for point in member["geometry"]]
            (inner_ways if member.get("role") == "inner" else outer_ways).append(way)

    outer, dropped = stitch_rings(outer_ways)
    inner, dropped_inner = stitch_rings(inner_ways)
    if dropped or dropped_inner:
        # 边界少一块 = 落在那里的散步归属不到这座城，而产物看起来一切正常。要吵。
        print(f"{boundary.name_zh}（关系 {boundary.osm_relation}）：{dropped + dropped_inner} 段边界接不成环，已丢弃")
    if not outer:
        raise SystemExit(f"{boundary.name_zh}（关系 {boundary.osm_relation}）：一个边界环都没接上，"
                         "多半是这个关系只有 subarea 成员——换一层 admin_level 或换一个关系")
    boundary.polygons = assemble_polygons(outer, inner)
    return boundary


def _chinese_name(tags: dict) -> str:
    """PRD 说 `name:zh` 优先、没有退回 `name`。实际数据里还有两件事：
    `name:zh` 可能是分号分隔的多个变体（波特兰是 `波特蘭;波特兰`），而 `name:zh-Hans`
    存在时是明确的简体。所以顺序是 zh-Hans → zh 的最后一段 → name。
    分号里挑最后一段是约定，不是判据——目录里的名字最终要人过一遍。"""
    simplified_tag = tags.get("name:zh-Hans")
    if simplified_tag:
        return simplified_tag
    zh = tags.get("name:zh")
    return zh.split(";")[-1].strip() if zh else ""


def simplified(boundary: Boundary, tolerance_degrees: float) -> list[dict]:
    """给分片用的简化边界。容差是度——1e-4 度约 10 米，够判断散步起点在哪座城，
    比原始边界小一个数量级。简化后少于四个点的环丢掉。"""
    out: list[dict] = []
    for polygon in boundary.polygons:
        ring = simplify(polygon["o"] + polygon["o"][:1], tolerance_degrees)[:-1]
        if len(ring) < 4:
            continue
        holes = []
        for hole in polygon["i"]:
            reduced = simplify(hole + hole[:1], tolerance_degrees)[:-1]
            if len(reduced) >= 4:
                holes.append(reduced)
        out.append({"o": ring, "i": holes})
    return out
