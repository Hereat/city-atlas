"""出包前的自动闸门。**红了硬停**，`--force` 可以放行，但会打印跳过了哪几条并写进索引页。

硬停而不是只报警，是因为这批产物的失败模式是**静默的**：产物照常生成、体积正常、
hash 算得出，只是名单少一半，或者东京被安给了六十公里外的古河市。只报警会淹在
1739 行进度里，而发布之后才发现就晚了——`cityID` 派出去不能改。

每一条都对着一次真踩过的坑，没有一条是「顺手也检查一下」：

| # | 闸门 | 拦的是哪次 |
|---|---|---|
| 1 | 覆盖层不是空集 | 日本出 127 座、东京大阪京都一座都不在那次 |
| 2 | 同级单位的边界不重叠 | 157 个平成大合并前的旧町村那次 |
| 3 | 每座城的画框盖得住它自己那片建成区 | 大阪市被量成 2×7 km 那次 |
| 4 | 人口最多的那些建成区，质心落在某座城的行政区里 | 同 1，但这条不需要任何该国知识 |
| 5 | 分片的平局无歧义：按面积升序，同一座城在各格里的面积一致 | 义乌被算成金华那次 |
| 6 | 画框锚点退回「行政区外接框中心」的不超过阈值 | 293 座退回外接框、小笠原村的框横跨 1827 km 那次 |
| 7 | 包里一条线都没有的城不超过阈值 | 7 座自治州的包是空白那次 |

**3 和 4 是最值钱的两条**，因为它们不需要任何该国知识，只拿 GHSL 那份全球数据当真值，
而最惨的两次失败都会被当场拦下。两条不能合并：4 管「这座城在不在名单里」，
3 管「它的画框对不对」——大阪那次是**在名单里但画框错**，只有 3 拦得住。

**原方案的第 7、8 条合成了这里的 7。** 原文是「默认取景里画得出的路不低于下限」加
「切片不得小于 1 KB」，两条判的是同一件事的不同强度，而那个「下限」定不出来：
御藏岛村 42 条路是对的（离岛本来就稀），甘孜州 0 条是错的，中间没有一刀切得下去。
所以只留「一条线都没有」这个不需要拍脑袋的极值，座数超过基线就停。

**两组、两处调用。** 1/2/3/4/6 在名单与画框算完、抓要素之前——那时拦下来只花了几分钟，
往下走就是几十分钟的抓取与出包；5/7 要有产物才问得了，放在出包之后。
"""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from pathlib import Path

from . import DIRECTORY_VERSION, MAP_DATA_VERSION
from . import review

# 闸门 3：画框面积至少要有城区面积的这么多倍。正常城在 1 上下（画框按等面积算），
# 大阪被量成 2×7 km 那次是 0.06——中间的间隔足够宽，这个数不必精调。
FRAME_AREA_RATIO = 0.25

# 闸门 3 只查城区最大的这些城：小城有 6 km 的画框下限兜着，而「画框被量成窄缝」
# 只发生在连片都市区里的大城上（大阪、町田、横滨都是）。
URBAN_SAMPLE = 100

# 估城区面积用的网格边长（`URBAN_GRID²` 个采样点）
URBAN_GRID = 24

# 闸门 4：人口最多的这些片建成区，质心必须落在某座城的行政区里。
# 取前 50 而不是全部：GHSL 的下限是 5 万人的连片建成区，尾巴上全是我们本来就不打算
# 单独立城的小镇；而名单塌掉的失败（日本那次）在前几名上就露馅了。
POPULOUS_SAMPLE = 50

# 闸门 6：画框锚点退回「行政区外接框中心」的比例上限。
# 那条退路本身是对的（名字认不到市中心、关系上也没写驻地时总得有个心），
# 但它一多就说明这一国的 `PLACE_KINDS` 或名字判据不对——日本加 village 之前是 293 座。
BBOX_ANCHOR_RATIO = 0.02


@dataclass(frozen=True)
class Result:
    number: int
    title: str
    ok: bool
    detail: str

    def line(self) -> str:
        return f"      [{'过' if self.ok else '停'}] {self.number} · {self.title}：{self.detail}"


def tolerances(rule: dict) -> dict:
    """这一国的容忍值。写在 `pbf.CITY_LEVELS` 那一行里——那里已经是「这个国家的特例
    都在这一处」，容忍值也是特例的一种（中国有 2 座空包已拍板不动，别国没有）。
    """
    return {"empty_packages": 0, "bbox_anchor_ratio": BBOX_ANCHOR_RATIO, **rule.get("gates", {})}


# --- 第一组：名单与画框（抓要素之前） ---------------------------------------

def check_selection(cities: list[dict], *, ucdb, country: str,
                    cover_levels: tuple[int, ...], rule: dict) -> list[Result]:
    """`cities` 是 `_select_cities` 之后、算完画框的那批，每座带 `boundary` 与 `frame`。"""
    limits = tolerances(rule)
    return [
        _cover_not_empty(cities, cover_levels),
        _no_same_level_overlap(cities),
        _frames_cover_their_urban_area(cities, ucdb),
        _populous_centres_have_a_city(cities, ucdb, country),
        _anchors_not_mostly_bbox(cities, limits["bbox_anchor_ratio"]),
    ]


def _cover_not_empty(cities, cover_levels) -> Result:
    covering = [city for city in cities if city["boundary"].admin_level in cover_levels]
    return Result(1, "覆盖层不是空集", bool(covering),
                  f"{len(covering)} 座落在覆盖层 {'/'.join(map(str, cover_levels))}"
                  if covering else
                  f"覆盖层 {'/'.join(map(str, cover_levels))} 上一座城都没有——"
                  f"`CITY_LEVELS` 的 `cover` 多半指错了层")


def _no_same_level_overlap(cities) -> Result:
    """同一层里两座城的地盘重叠 = 其中一个是已经撤销的历史单位。

    **只查同级**：地级市把下辖的县级市整个包住是设计内的（分片的平局规则就是为它写的），
    跨级重叠一概不算。日本的旧町村与现役市同在 7 级，正好落进这一条。

    探针取画框的心（市中心或驻地）。它在自己边界外的城跳过——那种城由闸门 6 管，
    在这里误报只会淹掉真问题。
    """
    offenders = []
    for city in cities:
        point = (city["frame"].lon, city["frame"].lat)
        level = city["boundary"].admin_level
        if not city["boundary"].contains(point):
            continue
        others = [other["name"] for other in cities
                  if other is not city and other["boundary"].admin_level == level
                  and _in_bbox(point, other["boundary"].bbox)
                  and other["boundary"].contains(point)]
        if others:
            offenders.append(f"{city['name']} ⊂ {'、'.join(others[:2])}")
    return Result(2, "同级单位的边界不重叠", not offenders,
                  "没有重叠" if not offenders else
                  f"{len(offenders)} 座的心落在同级的另一座里（多半是已撤销的旧单位，"
                  f"给 `CITY_LEVELS` 加一条 `require` 挡掉）：{'；'.join(offenders[:5])}")


def _frames_cover_their_urban_area(cities, ucdb) -> Result:
    """一座城的画框，面积不该远小于它自己那片城区。

    **判据必须用独立真值。** 第一版写的是「画框盖住 `frame.overlap_points` 的一半以上」，
    在中国 1483 座上误报 281 座（19%）：画框是「与城区**等面积**的 4:5 框」，
    而那些点按城区的**形状**散布——狭长的江门、多岛的香港、连片的苏州，等面积的框天然
    盖不住一半，那是设计，不是病。更要命的是那样问等于拿画框自己的输入再算一遍：
    大阪那次恰恰是 `overlap_points` 出的错，同一个错输入算两遍必然同号，闸门变成假绿。

    所以改成：在这座城的地盘上撒网格，数「既在 GHSL 建成区里、又在行政区里」的点，
    乘上单格面积就是城区面积——这条路一步都不经过画框。大阪那次画框 2×7 km = 14 km²
    而市区 220 km²，比值 0.06，当场红。

    只查城区最大的那些城：小城有 6 km 的画框下限兜着，而「画框被量成窄缝」只发生在
    连片都市区里的大城上。顺带把这条闸门的耗时从几分钟压到十几秒。
    """
    from .frame import covering_centres

    ranked = []
    for city in cities:
        if not city["frame"].ucdb_covered:
            continue          # 没有建成区的城（自治州、离岛的村）无从谈起，交给闸门 7
        centres = covering_centres(city["boundary"], ucdb)
        if centres:
            ranked.append((max(centre.area_km2 for centre in centres), city, centres))
    ranked.sort(key=lambda item: -item[0])

    offenders = []
    for _, city, centres in ranked[:URBAN_SAMPLE]:
        urban = _urban_area_km2(city["boundary"], centres, ucdb)
        if urban <= 0:
            continue
        frame = city["frame"]
        ratio = (frame.width / 1000) * (frame.height / 1000) / urban
        if ratio < FRAME_AREA_RATIO:
            offenders.append(f"{city['name']} 画框 {frame.width // 1000}×{frame.height // 1000} km "
                             f"对城区 {urban:.0f} km²（{ratio:.2f}×）")
    return Result(3, "画框装得下自己那片城区", not offenders,
                  f"量了 {min(len(ranked), URBAN_SAMPLE)} 座城区最大的城，"
                  f"画框都不小于城区的 {FRAME_AREA_RATIO:.0%}" if not offenders else
                  f"{len(offenders)} 座的画框远小于自己的城区："
                  f"{'；'.join(offenders[:5])}")


def _urban_area_km2(boundary, centres, ucdb) -> float:
    """这座城行政区内的建成区面积，网格采样估计（平方公里）。

    采样而不是求精确交集：几何那一路零依赖（不引 shapely），而闸门要的是量级——
    「画框比城区小一个数量级」与「小 3%」是两回事，采样的误差远够不着这个分辨率。
    """
    from .geometry import meters_per_degree, point_in_polygon

    box = boundary.bbox
    patches = [polygon for centre in centres for polygon in ucdb.polygons(centre.uc_id)]
    if not patches:
        return 0.0
    inside = 0
    total = URBAN_GRID * URBAN_GRID
    for row in range(URBAN_GRID):
        lat = box[1] + (box[3] - box[1]) * (row + 0.5) / URBAN_GRID
        for column in range(URBAN_GRID):
            lon = box[0] + (box[2] - box[0]) * (column + 0.5) / URBAN_GRID
            point = (lon, lat)
            if boundary.contains(point) and any(point_in_polygon(point, polygon) for polygon in patches):
                inside += 1
    mx, my = meters_per_degree((box[1] + box[3]) / 2)
    box_km2 = (box[2] - box[0]) * mx / 1000 * ((box[3] - box[1]) * my / 1000)
    return box_km2 * inside / total


def _populous_centres_have_a_city(cities, ucdb, country: str) -> Result:
    """人口最多的那几片建成区，人口加权质心必须落在某座城的行政区里。

    **这条不用名字匹配，用几何。** GHSL 给的是拉丁名（Osaka / 大阪市、Hong Kong / 香港），
    跨语言匹配一旦有 bug，闸门就变成假绿——而假绿的闸门比没有闸门更糟。
    """
    centres = sorted(ucdb.centres_in(country), key=lambda c: c.population, reverse=True)
    if not centres:
        return Result(4, "人口最多的建成区都有城", True,
                      f"GHSL 里没有 {country} 的建成区（国名对不上？这一条跳过）")
    sample = centres[:POPULOUS_SAMPLE]
    missing = [f"{centre.name or centre.uc_id}（{centre.population / 1e4:.0f} 万）"
               for centre in sample
               if not any(_in_bbox((centre.lon, centre.lat), city["boundary"].bbox)
                          and city["boundary"].contains((centre.lon, centre.lat))
                          for city in cities)]
    return Result(4, "人口最多的建成区都有城", not missing,
                  f"前 {len(sample)} 片都落在某座城里" if not missing else
                  f"{len(missing)} 片建成区的质心不在任何一座城的行政区内："
                  f"{'、'.join(missing[:5])}")


def _anchors_not_mostly_bbox(cities, ratio_limit: float) -> Result:
    fallback = [city["name"] for city in cities if city["frame"].anchor == "bbox"]
    ratio = len(fallback) / max(1, len(cities))
    return Result(6, "画框锚点很少退到外接框中心", ratio <= ratio_limit,
                  f"{len(fallback)}/{len(cities)} = {ratio:.1%}（上限 {ratio_limit:.0%}）"
                  + ("" if ratio <= ratio_limit else
                     f"；多半是 `PLACE_KINDS` 少认了一类，或名字判据认不到市中心："
                     f"{'、'.join(fallback[:5])}"))


def _in_bbox(point, box) -> bool:
    """点在经纬矩形内。闸门里所有的精确判定都先过这一关——1739 座城两两问「包不包含」
    是 O(N²) 次点在多边形内，而外接框一刀能去掉其中的绝大多数。
    """
    return box[0] <= point[0] <= box[2] and box[1] <= point[1] <= box[3]


# --- 第二组：产物（出包之后） -----------------------------------------------

def check_products(out: Path, *, country: str, registry: dict, rule: dict) -> list[Result]:
    limits = tolerances(rule)
    return [
        _shard_order_is_unambiguous(out),
        _packages_not_empty(out, country, registry, limits["empty_packages"]),
    ]


def _shard_order_is_unambiguous(out: Path) -> Result:
    """分片的平局规则：按面积升序，而且同一座城在各格里报的面积必须一样。

    第一条是义乌那次——App 取第一个包含起点的城，谁排前面就是答案，先前按 `cityID` 排
    等于按发号顺序随机选。第二条拦的是同一个 bug 的另一种写法：`areaKm2` 若写成
    「裁到这一格内那块」的面积，排序照样是升序，答案却会随格子变——同一座城在不同格里
    大小不同，归属就会在跨格的两次散步上给出两个城。
    """
    directory = out / "directory" / f"v{DIRECTORY_VERSION}"
    areas: dict[str, set] = {}
    unsorted = []
    for path in sorted(directory.glob("*.json.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            entries = json.load(handle)["cities"]
        keys = [(entry["areaKm2"], entry["cityID"]) for entry in entries]
        if keys != sorted(keys):
            unsorted.append(path.name)
        for entry in entries:
            areas.setdefault(entry["cityID"], set()).add(entry["areaKm2"])
    inconsistent = [city_id for city_id, values in areas.items() if len(values) > 1]
    ok = not unsorted and not inconsistent
    detail = f"{len(areas)} 座城、{len(list(directory.glob('*.json.gz')))} 个格，顺序与面积都一致"
    if unsorted:
        detail = f"{len(unsorted)} 个格没按面积升序排：{'、'.join(unsorted[:5])}"
    elif inconsistent:
        detail = (f"{len(inconsistent)} 座城在不同格里报了不同的面积（`areaKm2` 该是整个行政区"
                  f"的面积，不是裁到格内那块）：{'、'.join(inconsistent[:5])}")
    return Result(5, "分片的平局无歧义", ok, detail)


def _packages_not_empty(out: Path, country: str, registry: dict, limit: int) -> Result:
    """包里一条线都没有的城。画框落在农田、海面或山里就会这样——切出来不是零字节，
    图上却什么都没有。基线逐国声明（中国的阿坝州与草湖项目区已拍板留着）。
    """
    empty = []
    for entry in registry.values():
        if entry.get("country") != country:
            continue
        path = out / "package" / entry["cityID"] / f"{MAP_DATA_VERSION}.json.gz"
        if not path.exists():
            continue
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            package = json.load(handle)
        if review.road_count(package) == 0:
            empty.append(entry["name"])
    return Result(7, "包里有东西可画", len(empty) <= limit,
                  f"{len(empty)} 座空包（基线 {limit}）"
                  + (f"：{'、'.join(empty[:8])}" if empty else ""))


# --- 汇报 -------------------------------------------------------------------

def report(results: list[Result], *, force: bool, stage: str) -> list[Result]:
    """打印这一组的结果；有红的就停，除非 `--force`。返回被放行的那些红灯，
    调用方要把它们写进索引页——**不然「force 过一次」这件事会随会话结束而消失**。
    """
    print(f"      闸门（{stage}）", flush=True)
    for result in results:
        print(result.line(), flush=True)
    failed = [result for result in results if not result.ok]
    if failed and not force:
        raise SystemExit(
            f"\n{len(failed)} 道闸门红了，停在这里。每一条都对着一次真踩过的坑（`gates.py`），\n"
            f"先看红的那几条说了什么；确认这一国就该是这样，用 --force 放行"
            f"（会记进索引页，别让它随手一按）。")
    return failed
