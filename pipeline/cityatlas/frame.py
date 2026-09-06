"""城市画框：这张路径图看哪里。

**这里与 PRD 第 6 节的原文有实质出入，S0 实测之后改的，需要产品拍板**（理由见 README「画框」一节）。
PRD 写的是「城市行政区与 GHSL 城市中心相交，取人口最大的中心，对其外接框按详情比例补足方向并加
15% 留白」。照这条跑出来的结果：

  * 上海的 GHSL 城市中心外接框 75 × 68 km，杭州 73 × 60 km，成都 41 × 48 km。
    补足比例加留白之后画框接近 90 × 110 km，落到 353 pt 宽的版心上是 250 米一个点——
    主干道细过一根发丝，一次散步是几粒尘。稿的上海画框是 6 × 7.5 km。
  * 京都在 GHSL 里没有自己的城市中心，它并进了「大阪」那一个；「取人口最大的相交中心」
    会把画框中心放到四十公里外的大阪湾。旧金山同理，中心会落到湾区中部的 Hayward。

所以这里改成两条：**中心取 OSM 行政边界关系上的 `admin_centre` 节点**（市政府所在地，
这正是「城市中心」的行政定义，而且逐城可查、可审计）；**尺寸由可读性定**，见 `SHORT_SIDE`。
GHSL 因此只剩一个二值判断：这座城有没有被城市中心覆盖——覆盖的用可读画框，
没覆盖的用 PRD 说的「最小画框」兜底。
"""

from __future__ import annotations

from dataclasses import dataclass

from .geometry import meters_per_degree

# 详情图的比例：稿 1a 的图 462 pt 高、版心 353 pt 宽，4:5。列表缩略图是它中央的一段裁切。
ASPECT = 1.25

# 可读上限（米，短边）。版心 353 pt 在 3 倍屏上是 1059 px，
# 6000 / 1059 = 5.7 米一个像素——一条五米宽的支路正好占一个像素，这是「细路不消失」的下界。
# 稿的上海画框就是 6000 × 7500。画框再大，底图先糊掉，散步墨迹跟着一起糊。
SHORT_SIDE = 6000

# 没有被 GHSL 城市中心覆盖的地方（小城、或并进了邻城的城市中心）用的最小画框。
MIN_SHORT_SIDE = 2400

# 这条二值判据自己有个弱点，S0 没解决，记在这里：一个离大都市二十公里的小镇，
# 它的行政区多半会与那座大都市连片的城市中心相交，于是判成「覆盖」、拿到 6000 m 的画框，
# 画出来是一片近乎空白的田野——而这正是 MIN_SHORT_SIDE 想接住的情形。
# Bad Ischl 判成 False 是因为它在阿尔卑斯山里离谁都远，不是因为判据准。
# 真正想问的是「这里的路网密到值得 6000 m 的画框吗」，那个问题用手上已有的 OSM 数据
# 就能答（画框内一到三档道路的总长度 / 面积），不需要 GHSL。见 README「画框」。


@dataclass(frozen=True)
class Frame:
    lon: float
    lat: float
    width: int
    height: int
    anchor: str          # 中心是怎么来的：admin_centre / ucdb / bbox
    ucdb_covered: bool

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
    # 退回 UCDB 拿中心时，只认质心**落在这座城边界内**的那些：
    # 相交的城市中心可能是隔壁那座大城（京都之于大阪），它的质心不是这里的中心。
    inside = [centre for centre in centres if boundary.contains((centre.lon, centre.lat))]
    if boundary.centre is not None:
        lon, lat, anchor = boundary.centre[0], boundary.centre[1], "admin_centre"
    elif inside:
        biggest = max(inside, key=lambda centre: centre.population)
        lon, lat, anchor = biggest.lon, biggest.lat, "ucdb"
    else:
        box = boundary.bbox
        lon, lat, anchor = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2, "bbox"

    short = SHORT_SIDE if covered else MIN_SHORT_SIDE
    return Frame(round(lon, 6), round(lat, 6), short, int(short * ASPECT), anchor, covered)


def covering_centres(boundary, ucdb) -> list:
    """几何与这座城相交的 GHSL 城市中心。

    用相交、不用「质心落在边界内」：京都、旧金山这类被并进邻城城市中心的城，
    质心判据会答「没有城市中心」，而它们显然是城市。
    """
    return ucdb.centres_intersecting(boundary.bbox)
