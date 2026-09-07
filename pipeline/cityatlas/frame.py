"""城市画框：这张路径图看哪里。

**与 PRD 第 6 节的原文有出入，三轮之后改成现在这样，理由逐条记在这里。**

PRD 原文：「城市行政区与 GHSL 城市中心相交，取人口最大的中心，对其**外接框**按详情比例
补足方向并加 15% 留白」。照这条跑，上海拿到的是 90 × 110 km——外接框量的是这片连绵建成区
最远两点的距离，形状一歪就虚胖一大圈。

S0 第一版改成写死的 6000 m 短边。它错在另一头（2026-09-06 用户在真机上指出）：
**对每座城给同一个数**。成都三环内约 20 km 宽，6 km 只框住天府广场周边一小块。
上海那 6 × 7.5 是设计稿为上海挑的，好看不代表它是普适常数。

中间试过「按城市级路网的密度掉档收边」，实测否掉了：主次干道在市区与郊区都是每公里
一条，密度从核心到三十公里外只从 9 掉到 3 m/km²，**没有可辨的边界**（上海/成都/杭州
三城的剖面一致）。要拿路网判就得把 residential 一起算进来，那又正是用户担心的
「郊区路径把范围撑大」。路网是间接证据，换掉。

**现在用的是直接证据：GHSL UCDB 的 `GC_UCA_KM2_2025`——城区面积，单位平方公里。**
「城区」在这里有确切定义（每平方公里 1500 人以上或建成率 50% 以上的连片格网，
总人口 5 万以上），全球一套口径、逐城可查，而且这份数据我们本来就依赖。
画框 = 与这个面积等面积的 4:5 框。上海 3128 km² → 50 km，成都 867 km² → 26 km，
阿姆斯特丹 308 km² → 16 km；小城自然小，不需要另一条规则。

用面积而不是外接框，是因为**面积不受形状影响**：沿江铺开的城、被山切开的城，
外接框会把大片非城区圈进来，面积不会。

没有上限。上限原先存在的唯一理由是「只有一张静态图，它必须在一个尺寸下就好看」；
一旦支持缩放，这个前提就没了（2026-09-06 拍板）。眼下还没有缩放，所以这里算出来的是
**默认取景**——缩放落地后它是打开城市的第一眼，不是能看到的全部。
"""

from __future__ import annotations

from dataclasses import dataclass

import math

from .geometry import meters_per_degree

# 详情图的比例：稿 1a 的图 462 pt 高、版心 353 pt 宽，4:5。列表缩略图是它中央的一段裁切。
ASPECT = 1.25

# GHSL 没覆盖到的地方（山里的小城，或并进了邻城的城市中心）用的最小画框。
MIN_SHORT_SIDE = 2400

# 短边下限。GHSL 的城区下限是 5 万人，对应的面积再小也有几平方公里，
# 这条实际只在数据缺失时兜底。
SHORT_MIN = 2400

# 短边取整到这个刻度，免得出现 8137 m 这种没人能复述的数。
SHORT_STEP = 500

# 默认取景占「等面积短边」的比例，以及它的下限（米）。
#
# 默认那一眼不能是整个数据范围（上海 75 km，散步缩成中间一小簇），也不能按用户自己的
# 路径框（只在小区周围走过的人会得到一张空白纸，第一眼要先让人认出「这是我的城市」，
# 拍板 24）。所以按城市自身的尺度取一个比例。
#
# 下限是给小城的：GHSL 的面积中位数只有 23 km²（等面积短边 4.3 km），90% 分位 8.9 km，
# 绝大多数城市本来就小，再乘 0.35 会缩到看不见。夹住之后小城默认就是它的全貌。
DEFAULT_FRACTION = 0.35
DEFAULT_MIN = 4000

@dataclass(frozen=True)
class Frame:
    lon: float
    lat: float
    width: int
    height: int
    anchor: str          # 中心是怎么来的：admin_centre / ucdb / bbox / ucdb-bbox
    ucdb_covered: bool
    # 默认取景：打开城市的第一眼。短边（米）与相对画框中心的偏移（米，y 朝北）。
    # 它与画框是两个数：画框是「装了多少、缩到最远能看多远」，这个是「先看哪一块」。
    default_short: int = 0
    default_cx: float = 0.0
    default_cy: float = 0.0

    @property
    def default_view(self) -> dict:
        short = self.default_short or self.width
        return {"cx": round(self.default_cx), "cy": round(self.default_cy),
                "w": int(short), "h": int(short * ASPECT)}

    @property
    def center(self):
        return self.lon, self.lat

    @property
    def size(self):
        return self.width, self.height

    def bounds(self):
        """WGS-84 外接框 `(min_lon, min_lat, max_lon, max_lat)`，进 manifest。"""
        return bounds_of(self.lon, self.lat, self.width, self.height)


def bounds_of(lon: float, lat: float, width: float, height: float):
    """以 `(lon, lat)` 为中心、`width × height` 米的画框的 WGS-84 外接框。"""
    mx, my = meters_per_degree(lat)
    return (round(lon - width / 2 / mx, 6), round(lat - height / 2 / my, 6),
            round(lon + width / 2 / mx, 6), round(lat + height / 2 / my, 6))


def compute(boundary, centres) -> Frame:
    """`centres` 是 UCDB 里外接框与这座城相交的城市中心列表（可以为空）。"""
    covered = bool(centres)
    lon, lat, anchor = centre_of(boundary, centres)
    short = short_side_from(boundary, centres)
    return Frame(round(lon, 6), round(lat, 6), short, int(short * ASPECT), anchor, covered)


def short_side_from(boundary, centres) -> int:
    """与这座城的城区面积等面积的 4:5 框，短边取整到 `SHORT_STEP`。

    面积取**质心落在这座城边界内**的那些城市中心里最大的一个。只认落在界内的：
    与边界相交的城市中心可能是隔壁那座大城（京都之于大阪、旧金山之于湾区），
    拿它的面积会给这座城一个几倍大的画框。一个都没有就退回 `MIN_SHORT_SIDE`。
    """
    inside = [centre for centre in centres if boundary.contains((centre.lon, centre.lat))]
    area = max((centre.area_km2 for centre in inside), default=0.0)
    if area <= 0:
        return MIN_SHORT_SIDE
    short = math.sqrt(area / ASPECT) * 1000
    return max(SHORT_MIN, int(round(short / SHORT_STEP) * SHORT_STEP))


def from_urban_centre(centre, polygons) -> Frame:
    """GHSL 那条路的画框：**数据范围取城区多边形的外接框**，不是等面积框。

    等面积框会切掉城区的一部分——杭州建成区铺开 73 × 60 km，等面积框只有 35 km，
    灵隐寺差两百米落在框外，绕西湖走一圈会判成「不属于任何城市」（2026-09-06 实测）。
    数据范围要装得下整片建成区，「先看哪一块」由默认取景回答，两件事不能用一个数。

    中心取外接框的几何中心：画框要盖住整片城区，就该以它为心。
    默认取景另以 GHSL 的人口加权质心为心——那才是「城市的重心」在哪
    （实测五座城里人口质心盖住的城区比几何中心多，赢四座）。
    """
    points = [point for polygon in polygons for point in polygon["o"]]
    if not points:
        short = MIN_SHORT_SIDE
        return Frame(round(centre.lon, 6), round(centre.lat, 6), short, int(short * ASPECT),
                     "ucdb", False, short, 0.0, 0.0)

    lons = [lon for lon, _ in points]
    lats = [lat for _, lat in points]
    lon = (min(lons) + max(lons)) / 2
    lat = (min(lats) + max(lats)) / 2
    mx, my = meters_per_degree(lat)
    # **数据范围不补成 4:5**：4:5 是取景的比例，不是这片数据该有的形状。上海的城区是
    # 74.8 × 68.3，补成 4:5 要撑到 75 × 93.8——多出来的二十五公里几乎全是海和农田，
    # 白装进包里。取景的比例由视口保证，画框只负责「装得下」。
    width = max(SHORT_MIN, int(math.ceil((max(lons) - min(lons)) * mx / SHORT_STEP) * SHORT_STEP))
    height = max(SHORT_MIN, int(math.ceil((max(lats) - min(lats)) * my / SHORT_STEP) * SHORT_STEP))

    default = short_side_from_area(centre.area_km2)
    default = max(DEFAULT_MIN, int(round(default * DEFAULT_FRACTION / SHORT_STEP) * SHORT_STEP))
    default = min(default, width, int(height / ASPECT))

    return Frame(round(lon, 6), round(lat, 6), width, height, "ucdb-bbox", True,
                 default, (centre.lon - lon) * mx, (centre.lat - lat) * my)


def from_admin_and_ghsl(boundary, centres, ucdb, city_centre=None) -> Frame:
    """行政边界 + GHSL：**画框取「GHSL 城区落在这座城行政区内的那部分」的外接框**。

    两份数据各答各的题（拍板 27，见实施三.2）：行政边界说「哪一片属于这座城」，
    GHSL 说「建成区在哪」，交出来正好是「这座城的建成区」。深圳因此拿到深圳那一块，
    而不是整个珠三角——GHSL 把珠三角并成一座 6454 km² 的「广州」，直接拿它当边界，
    深圳用户的城市图会写着广州。

    GHSL 覆盖不到的地方（山里的小城、低于五万人的镇）没有建成区可交，退回
    `MIN_SHORT_SIDE`：那种地方本来就不需要几十公里的画框。
    """
    # **只取占这座城最多的那一片建成区**，不是所有相交城区的并集。
    #
    # 地级市的辖区里散着许多互不相连的镇，各自都是 GHSL 的城市中心：取并集，渭南市会拿到
    # 139 × 116 km 的画框（2026-09-06 实测），画的是整个渭南地区而不是渭南这座城。
    # 城市图要画的是这座城。
    #
    # 判据是「这片建成区有多少落在这座城里」，估法是**总面积 × 落在界内的点的比例**。
    #
    # 不能按整片的面积挑：连片都市区里，深圳境内那块属于 GHSL 的「广州」（6454 km²），
    # 按整片面积挑会挑中它，按质心在不在界内又一个都挑不着。
    #
    # 也不能按「裁完剩多少点」挑：GHSL 的多边形是网格生成的，**点数跟周长走、不跟面积走**，
    # 一条沿海的细长带周长大、点多，会赢过紧凑的主城——宁波就是这么拿到 58 × 17.5 km 的
    # 细长画框的（2026-09-07 实测）。乘上比例之后，宁波主城 523 km² 胜过慈溪 339 km²。
    best, inside = 0.0, []
    for centre in centres:
        points = [point for polygon in ucdb.polygons(centre.uc_id) for point in polygon["o"]]
        if not points:
            continue
        within = [point for point in points if boundary.contains(point)]
        if not within:
            continue
        share = centre.area_km2 * len(within) / len(points)
        if share > best:
            best, inside = share, within

    if not inside:
        box = boundary.bbox
        lon, lat = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        short = MIN_SHORT_SIDE
        return Frame(round(lon, 6), round(lat, 6), short, int(short * ASPECT),
                     "admin-bbox", False, short, 0.0, 0.0)

    lons = [lon for lon, _ in inside]
    lats = [lat for _, lat in inside]
    lon = (min(lons) + max(lons)) / 2
    lat = (min(lats) + max(lats)) / 2
    mx, my = meters_per_degree(lat)
    width = max(SHORT_MIN, int(math.ceil((max(lons) - min(lons)) * mx / SHORT_STEP) * SHORT_STEP))
    height = max(SHORT_MIN, int(math.ceil((max(lats) - min(lats)) * my / SHORT_STEP) * SHORT_STEP))

    default = max(DEFAULT_MIN, int(round(min(width, height / ASPECT)
                                         * DEFAULT_FRACTION / SHORT_STEP) * SHORT_STEP))
    default = min(default, width, int(height / ASPECT))

    # **默认取景以市中心为心，不是画框的几何中心。** 建成区的外接框中心不等于城市中心：
    # 成都往南铺得远（天府新区）、重庆主城被江切成几块，几何中心都会偏出去——
    # 渝中半岛因此跑到了图的右下角（2026-09-07 用户在图上指出）。
    # 市中心取 OSM 的 `place=city` 节点（`pbf.centre_for`），逐城可查，不需要手调。
    # 数据范围仍以外接框为准：那一层要保证装得下整片城区，两件事各归各的。
    dx = dy = 0.0
    if city_centre is not None:
        dx = (city_centre[0] - lon) * mx
        dy = (city_centre[1] - lat) * my
        # 夹回画框之内：市中心可能离画框中心很远（广州的建成区往东南铺，两者差 28 km），
        # 不夹的话默认取景会有一半落在画框外，开城第一眼是半张空纸。
        limit_x = max(0.0, (width - default) / 2)
        limit_y = max(0.0, (height - default * ASPECT) / 2)
        dx = max(-limit_x, min(limit_x, dx))
        dy = max(-limit_y, min(limit_y, dy))
    return Frame(round(lon, 6), round(lat, 6), width, height, "ghsl-in-admin", True,
                 default, dx, dy)


def short_side_from_area(area_km2: float) -> float:
    """与城区面积等面积的 4:5 框的短边（米）。用面积而不是外接框，是因为**面积不受形状
    影响**：沿江铺开的城、被山切开的城，外接框会把大片非城区圈进来，面积不会。"""
    if area_km2 <= 0:
        return MIN_SHORT_SIDE
    return math.sqrt(area_km2 / ASPECT) * 1000


def centre_of(boundary, centres) -> tuple[float, float, str]:
    """画框中心，以及它是怎么来的。"""
    # 退回 UCDB 拿中心时，只认质心**落在这座城边界内**的那些：
    # 相交的城市中心可能是隔壁那座大城（京都之于大阪），它的质心不是这里的中心。
    inside = [centre for centre in centres if boundary.contains((centre.lon, centre.lat))]
    if boundary.centre is not None:
        return boundary.centre[0], boundary.centre[1], "admin_centre"
    if inside:
        biggest = max(inside, key=lambda centre: centre.population)
        return biggest.lon, biggest.lat, "ucdb"
    box = boundary.bbox
    return (box[0] + box[2]) / 2, (box[1] + box[3]) / 2, "bbox"


def covering_centres(boundary, ucdb) -> list:
    """几何与这座城相交的 GHSL 城市中心。

    用相交、不用「质心落在边界内」：京都、旧金山这类被并进邻城城市中心的城，
    质心判据会答「没有城市中心」，而它们显然是城市。
    """
    return ucdb.centres_intersecting(boundary.bbox)
