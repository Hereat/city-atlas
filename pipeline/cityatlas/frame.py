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

from .geometry import meters_per_degree, point_in_polygon

# 详情图的比例：稿 1a 的图 462 pt 高、版心 353 pt 宽，4:5。列表缩略图是它中央的一段裁切。
ASPECT = 1.25

# GHSL 没覆盖到的地方（山里的小城，或并进了邻城的城市中心）用的最小画框。
MIN_SHORT_SIDE = 2400

# 短边下限。GHSL 的城区下限是 5 万人，对应的面积再小也有几平方公里，
# 这条实际只在数据缺失时兜底。
SHORT_MIN = 2400

# 短边取整到这个刻度，免得出现 8137 m 这种没人能复述的数。
SHORT_STEP = 500

# GHSL 没覆盖到城区、但 OSM 说这儿有座城时，以市中心为心给的画框（米，短边）。
#
# 中国有 81 座这样的城（2026-09-07 实测）：涪陵城区几十万人，GHSL R2024A 里却没有
# 任何建成区覆盖它，最近的一片在二十二公里外。这时候不能退回「这座城辖内最大的那片
# 建成区」——那会把画框放到二十公里外的农田上，图上一条街都没有（用户在图上指出的
# 就是涪陵与崇明这两张）。GHSL 说「这里不算城市中心」，OSM 说「这儿有座城」，
# 后者更可信：这个数按县级市城区的常见尺度取，宁可略大也不要指错地方。
UNCOVERED_SHORT = 10000

# 默认取景占「等面积短边」的比例，以及它的下限（米）。
#
# 默认那一眼不能是整个数据范围（上海 75 km，散步缩成中间一小簇），也不能按用户自己的
# 路径框（只在小区周围走过的人会得到一张空白纸，第一眼要先让人认出「这是我的城市」，
# 拍板 24）。所以按城市自身的尺度取一个比例。
#
# 下限是给小城的：GHSL 的面积中位数只有 23 km²（等面积短边 4.3 km），90% 分位 8.9 km，
# 绝大多数城市本来就小，再乘 0.35 会缩到看不见。夹住之后小城默认就是它的全貌。
DEFAULT_FRACTION = 0.35
# 下限从 4 km 提到 6 km：小城的画框本来就只有十几公里，0.35 一乘再被 4 km 夹住，
# 打开第一眼只剩市中心几条街，认不出是哪儿（2026-09-07 用户看万州、永川时指出）。
DEFAULT_MIN = 6000

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


# 量「这片建成区与这座城重叠在哪」时，一副轮廓上取样多少个点。
# 画框的短边最后要取整到 500 m，取样密到毫米没有意义；这个数是「够定出外接框」的量。
OVERLAP_SAMPLES = 300


def overlap_points(polygons, boundary) -> list:
    """这片建成区与这座城重叠处的取样点。**两副轮廓各取落在对方里的那些。**

    只取建成区那一副是不够的：一座整个躺在连片建成区**内部**的城，建成区的轮廓
    一个点都不落在它界内，量出来只有边界擦过的那一小段——东京圈里的町田市因此
    拿到 2 × 2 km、大阪市 2 × 7 km、横滨市 6 × 13 km（2026-09-07 日本这一轮实测）。
    中国没露出这个毛病，是因为地级市的地盘普遍比一片建成区大，轮廓总会穿过去。

    只取行政边界那一副也不行：深圳会拿到整个市域，包括东边那片山。
    两边都取，外接框正是「这座城的建成区」——这也正是拍板 27 那句「GHSL 说建成区
    在哪、行政边界说哪一片属于这座城」的字面意思。
    """
    points = [point for polygon in polygons for point in polygon["o"]
              if boundary.contains(point)]
    for ring in (polygon["o"] for polygon in boundary.polygons):
        step = max(1, len(ring) // OVERLAP_SAMPLES)
        points += [point for point in ring[::step]
                   if any(point_in_polygon(point, polygon) for polygon in polygons)]
    return points


def from_admin_and_ghsl(boundary, centres, ucdb, city_centre=None) -> Frame:
    """行政边界 + GHSL：**画框取「GHSL 城区落在这座城行政区内的那部分」的外接框**。

    两份数据各答各的题（拍板 27，见实施三.2）：行政边界说「哪一片属于这座城」，
    GHSL 说「建成区在哪」，交出来正好是「这座城的建成区」。深圳因此拿到深圳那一块，
    而不是整个珠三角——GHSL 把珠三角并成一座 6454 km² 的「广州」，直接拿它当边界，
    深圳用户的城市图会写着广州。

    GHSL 覆盖不到的地方（山里的小城、低于五万人的镇）没有建成区可交，退回
    `MIN_SHORT_SIDE`：那种地方本来就不需要几十公里的画框。
    """
    # **只取一片建成区**，不是所有相交城区的并集：地级市的辖区里散着许多互不相连的镇，
    # 各自都是 GHSL 的城市中心，取并集会让渭南市拿到 139 × 116 km 的画框
    # （2026-09-06 实测），画的是整个渭南地区而不是渭南这座城。
    #
    # 取哪一片，**优先看市中心落在哪一片里**。只按大小挑会挑错：崇明区辖内最大的一片
    # 建成区在长兴岛（造船厂那带），涪陵区辖内最大的一片在惠民一带的乡镇，两座城的画框
    # 因此落在了离城区十几二十公里的农田上（2026-09-07 用户在图上指出）。
    #
    # 没有市中心可依时（自治州与盟的名字对不上 OSM 的 place 节点，中国有 69 座）退回
    # 「占这座城最多的那一片」。那一步的判据是**总面积 × 落在界内的点的比例**：
    # 不能按整片面积挑——深圳境内那块属于 GHSL 的「广州」（6454 km²），按面积挑会挑中
    # 整个珠三角；也不能按裁完剩多少点挑——GHSL 的多边形是网格生成的、点数跟周长走，
    # 一条沿海细长带会赢过紧凑的主城（宁波实测拿到 58 × 17.5 km 的细长画框）。
    inside = []
    if city_centre is not None:
        for centre in centres:
            polygons = ucdb.polygons(centre.uc_id)
            if not any(point_in_polygon(city_centre, polygon) for polygon in polygons):
                continue
            within = overlap_points(polygons, boundary)
            if within:
                inside = within
                break

    # 市中心在、但没有任何建成区覆盖它：以市中心为心给一个默认框。
    # 退回「辖内最大的那片」会把画框放到几十公里外（见 `UNCOVERED_SHORT` 的注释）。
    if not inside and city_centre is not None:
        short = UNCOVERED_SHORT
        height = int(short * ASPECT)
        default = max(DEFAULT_MIN, int(round(short * DEFAULT_FRACTION / SHORT_STEP) * SHORT_STEP))
        return Frame(round(city_centre[0], 6), round(city_centre[1], 6), short, height,
                     "osm-centre", False, min(default, short), 0.0, 0.0)

    if not inside:
        best = 0.0
        for centre in centres:
            polygons = ucdb.polygons(centre.uc_id)
            points = [point for polygon in polygons for point in polygon["o"]]
            if not points:
                continue
            within = [point for point in points if boundary.contains(point)]
            if not within:
                continue
            share = centre.area_km2 * len(within) / len(points)
            if share > best:
                best, inside = share, overlap_points(polygons, boundary)

    # 整座城躺在一片建成区**内部**：那片建成区的轮廓一个点都不落在界内，上面那一轮
    # 挑不出任何一片，而这座城明明整个都是建成区。东京圈与大阪圈里没有 `place` 节点
    # 的特别区就是这样（新宿区、涩谷区，2026-09-07 日本这一轮实测：不接这条会退到
    # `admin-bbox`，18 km² 的新宿区拿到 2.4 km 的画框）。
    if not inside:
        centre_point = ((boundary.bbox[0] + boundary.bbox[2]) / 2,
                        (boundary.bbox[1] + boundary.bbox[3]) / 2)
        for centre in sorted(centres, key=lambda c: -c.area_km2):
            polygons = ucdb.polygons(centre.uc_id)
            if any(point_in_polygon(centre_point, polygon) for polygon in polygons):
                inside = overlap_points(polygons, boundary)
                break

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


# ---------- 城市名单：一片建成区 = 一座城 ----------
#
# 先前一版的名单是「市级行政单位」（中国 289 座地级市与直辖市）。它漏掉了辖区里那些
# **与主城不相连**的建成区：289 座里有 269 座辖区内还有别的成片城镇，一共 1512 片
# （2026-09-07 实测）。在涪陵、慈溪、常熟散步会归到重庆、宁波、苏州，而那些地方落在
# 主城画框之外，图上什么都没有。
#
# 名单由两部分合成：
#
#   1. **每个有建成区的地级市与直辖市**（289 座，就是上一版的名单）。这一半保证覆盖
#      不丢，也保证连片都市区不会塌成一个名字——珠三角跨广州、深圳、佛山、东莞四个市，
#      没有任何单一单位包得住它，只靠「最小包含单位」会把整片安到其中一家头上
#      （实测安给了东莞，6454 km²）。
#   2. **完整包住一片独立建成区、且不属于任何主城的区县**。这一半把远郊捞出来：
#      涪陵、万州、慈溪、常熟那些与主城不相连的城镇。
#
# 于是重庆主城仍叫「重庆市」（横跨多个区，没有区包得住它），而涪陵那片整个落在涪陵区
# 之内、且涪陵区的地盘上没有重庆主城，所以另立一座「涪陵区」。

def smallest_containing(probe, units, *, coverage: float = 0.95):
    """完整包住这些点的最小行政单位；没有就退回覆盖比例最大的那个。

    「完整包住」而不是「质心落在里面」：按质心判，重庆主城的质心落在某个区里，
    整片主城就会被命名成那个区。
    """
    full = None
    best_share, best = 0.0, None
    for unit in units:
        box = unit["bbox"]
        if not any(box[0] <= x <= box[2] and box[1] <= y <= box[3] for x, y in probe):
            continue
        share = sum(1 for point in probe if unit["boundary"].contains(point)) / len(probe)
        if share >= coverage and (full is None or unit["area"] < full["area"]):
            full = unit
        if share > best_share:
            best_share, best = share, unit
    return full or best


def drop_swallowed(entries, cover_levels):
    """剔掉「已经是别座城主城的一部分」的区县。

    浦东新区里有临港这种与主城不相连的建成区，于是整个浦东被当成一座城——可浦东同时
    又是上海主城的一部分，图上会出现两座重叠的「城」。

    两条约束，都是被反例逼出来的：

    * **只有候补层（不在 `cover_levels` 里的那些）会被剔**。不加这条，珠三角那片连绵建成区会
      把地盘与它重叠的**广州市、苏州市、嘉兴市**一并剔掉——它们是正经的地级市，
      不是谁的一部分（2026-09-07 实测，误剔 161 座里有一批是这种）。
    * **判据是地理事实，不是行政级别**。按「区一律不独立」会误伤涪陵、万州、永川、
      江津——它们全是市辖区，而辖区里没有任何一片属于重庆主城。

    `entries` 按建成区面积从大到小给，大的先占。
    """
    kept = []
    for entry in entries:
        unit = entry["unit"]
        if unit["level"] in cover_levels:
            kept.append(entry)          # 覆盖层（中国的地级市与直辖市）不被吞并
            continue
        box = unit["bbox"]
        swallowed = False
        for bigger in kept:
            other = bigger["box"]
            # 外接框不相交就不可能吞并，先筛一道：不筛是 1500 座两两判断，跑十八分钟
            if other[2] < box[0] or other[0] > box[2] or other[3] < box[1] or other[1] > box[3]:
                continue
            if bigger["unit"]["osm_id"] == unit["osm_id"]:
                continue
            if any(unit["boundary"].contains(point) for point in bigger["probe"][::4]):
                swallowed = True
                break
        if not swallowed:
            kept.append(entry)
    return kept
