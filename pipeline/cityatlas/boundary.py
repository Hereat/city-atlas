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
        name_zh=chinese_name(tags) or name,
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


def chinese_name(tags: dict) -> str:
    """PRD 说 `name:zh` 优先、没有退回 `name`。实际数据里还有两件事：
    `name:zh` 可能把几个写法塞在一个值里，而 `name:zh-Hans` 存在时是明确的简体。
    所以顺序是 zh-Hans → zh 的变体里挑一个 → name。

    塞多个写法有两种写法，各按各的约定挑：**分号是「繁;简」**（波特兰是
    `波特蘭;波特兰`），取最后一段；**斜杠是「简 / 繁」**（横浜市的 place 节点写
    `横滨市 / 橫濱市`），取第一段。两条都是约定不是判据——目录里的名字最终要人过一遍。

    `name:zh-Hans` 那一条在日本尤其顶用：1896 座里有 590 座的 `name:zh` 是繁体
    （瀧澤市、會津坂下町、澀谷區），只有它给出简体（2026-09-07 实测）。
    这条规则**全管线只此一份**——边界、place 节点、名册都问它，
    不然同一座城的边界与它的市中心点会各叫各的名字，`centre_for` 当场认不出人。"""
    simplified_tag = tags.get("name:zh-Hans")
    if simplified_tag:
        return simplified_tag
    zh = tags.get("name:zh")
    return zh.split(";")[-1].split("/")[0].strip() if zh else ""


def spellings(tags: dict) -> set[str]:
    """这条数据可能被写成的名字，**认人时几种写法都要试一遍**。

    `chinese_name` 答的是「显示成什么」，这个答的是「可能被写成什么」，两件事。
    OSM 里 `name` / `name:zh` / `name:zh-Hans` 各填各的：一座城的边界填了简体、
    它的 place 节点只填了繁体是常事。只认一种写法就会认不出人——日本这一轮把两边
    统一成 `chinese_name` 之后，`centre_for` 的命中率从 1702 掉到 1443
    （2026-09-07 实测），244 座小城的画框因此退回行政区外接框的中心。
    """
    names = {tags.get("name"), tags.get("name:zh"), tags.get("name:zh-Hans"), chinese_name(tags)}
    return {name.strip() for name in names if name and name.strip()}


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
