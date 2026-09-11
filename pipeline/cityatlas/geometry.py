"""纯几何：投影、简化、裁剪、点在多边形内。零第三方依赖。

为什么不引 shapely/geopandas：管线只需要五个操作——局部米制投影、Douglas–Peucker、
按矩形裁线、按矩形裁面、点在环内。每个都是二三十行的教科书算法，引一套 GEOS
换来的是三百兆的构建依赖和一份「这台机器上装没装」的运维负担。
坐标一律用 `(x, y)` 二元组的列表；经纬一律 `(lon, lat)`，与 GeoJSON 同序。
"""

from __future__ import annotations

import math

# WGS-84 椭球。米/度按当地曲率半径算，与 CoreLocation 的 `CLLocation.distance`
# 在城市尺度（几十公里）上相对误差小于 1e-5——App 侧 `CityProjection` 必须用同一套常数，
# 否则同一条路径在管线里和手机上会落在不同像素上。
_A = 6378137.0
_F = 1.0 / 298.257223563
_E2 = _F * (2.0 - _F)


def meters_per_degree(lat: float) -> tuple[float, float]:
    """返回 `(每度经度的米数, 每度纬度的米数)`，在纬度 `lat` 处取值。"""
    s = math.sin(math.radians(lat))
    w = math.sqrt(1.0 - _E2 * s * s)
    prime_vertical = _A / w              # 卯酉圈曲率半径
    meridian = _A * (1.0 - _E2) / w ** 3  # 子午圈曲率半径
    rad = math.pi / 180.0
    return prime_vertical * math.cos(math.radians(lat)) * rad, meridian * rad


class Projection:
    """以一点为原点的局部米制平面，x 朝东、y 朝北。

    这是 `WalkRoute.Planar` 的同一套算法：城市画框最大不过几十公里，
    在这个尺度上切平面与椭球的差异远小于一米的量化步长。
    """

    def __init__(self, lon: float, lat: float) -> None:
        self.lon = lon
        self.lat = lat
        self.mx, self.my = meters_per_degree(lat)

    def forward(self, lon: float, lat: float) -> tuple[float, float]:
        return (lon - self.lon) * self.mx, (lat - self.lat) * self.my

    def inverse(self, x: float, y: float) -> tuple[float, float]:
        return self.lon + x / self.mx, self.lat + y / self.my


# ---------- Mollweide（ESRI:54009）逆投影：GHSL 的几何存在这个坐标系里 ----------

_MOLLWEIDE_R = _A


def mollweide_forward(lon: float, lat: float) -> tuple[float, float]:
    """`(lon, lat)` 度 → Mollweide 平面米。辅助角要牛顿迭代，五轮到双精度。"""
    phi = math.radians(lat)
    theta = phi
    for _ in range(5):
        denominator = 2.0 + 2.0 * math.cos(2.0 * theta)
        if abs(denominator) < 1e-12:
            break
        theta -= (2.0 * theta + math.sin(2.0 * theta) - math.pi * math.sin(phi)) / denominator
    root2 = math.sqrt(2.0)
    return (2.0 * root2 / math.pi) * _MOLLWEIDE_R * math.radians(lon) * math.cos(theta), \
        root2 * _MOLLWEIDE_R * math.sin(theta)


def mollweide_inverse(x: float, y: float) -> tuple[float, float]:
    """Mollweide 平面米 → `(lon, lat)` 度。正投影要迭代，逆投影是闭式的。"""
    root2 = math.sqrt(2.0)
    theta = math.asin(max(-1.0, min(1.0, y / (root2 * _MOLLWEIDE_R))))
    lat = math.degrees(math.asin(max(-1.0, min(1.0, (2.0 * theta + math.sin(2.0 * theta)) / math.pi))))
    cos_theta = math.cos(theta)
    if abs(cos_theta) < 1e-12:
        return 0.0, lat
    lon = math.degrees(math.pi * x / (2.0 * root2 * _MOLLWEIDE_R * cos_theta))
    return lon, lat


# ---------- 简化 ----------

def simplify(points: list[tuple[float, float]], epsilon: float) -> list[tuple[float, float]]:
    """Douglas–Peucker。迭代实现——一条洲际河流的环有上万个点，递归会爆栈。"""
    if len(points) < 3 or epsilon <= 0:
        return points
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        lo, hi = stack.pop()
        if hi - lo < 2:
            continue
        ax, ay = points[lo]
        bx, by = points[hi]
        dx, dy = bx - ax, by - ay
        span = math.hypot(dx, dy)
        best, best_d = -1, epsilon
        for i in range(lo + 1, hi):
            px, py = points[i]
            if span == 0:
                d = math.hypot(px - ax, py - ay)
            else:
                d = abs(dy * px - dx * py + bx * ay - by * ax) / span
            if d > best_d:
                best, best_d = i, d
        if best >= 0:
            keep[best] = True
            stack.append((lo, best))
            stack.append((best, hi))
    return [p for p, k in zip(points, keep) if k]


# ---------- 裁剪 ----------

Box = tuple[float, float, float, float]  # (min_x, min_y, max_x, max_y)
Ring = list[tuple[float, float]]


def clip_polyline(points: list[tuple[float, float]], box: Box) -> list[list[tuple[float, float]]]:
    """把折线裁进矩形，返回若干段。逐段 Liang–Barsky，相邻的完整段接回同一条。"""
    return _stitch(_clip_segment(a, b, box) for a, b in zip(points, points[1:]))


def clip_polyline_outside(points: list[tuple[float, float]], polygons: list[dict]) -> list[list[tuple[float, float]]]:
    """把折线**落在这些多边形之内**的部分裁掉，外面的留着，返回若干段。

    与 `clip_polyline` 是同一层级的操作，但内核不同、不该合并成一个「对任意区域裁剪」：
    矩形能解析求交（Liang–Barsky），任意多边形只能「求出所有交点、切开、逐段判在不在里面」。
    两者共用的是接段那一步（`_stitch`）。

    **段的归属按中点判，不按端点。** 端点常常正落在边界上（一条河的最后一个节点就画在
    岸线上），那时射线法答什么都是对的；中点在哪一侧没有歧义。
    """
    def pieces():
        for a, b in zip(points, points[1:]):
            cuts = _shore_cuts(a, b, polygons)
            for start, end in zip(cuts, cuts[1:]):
                middle = ((start[0] + end[0]) / 2.0, (start[1] + end[1]) / 2.0)
                yield None if any(point_in_polygon(middle, poly) for poly in polygons) else (start, end)

    return _stitch(pieces())


def _stitch(segments) -> list[list[tuple[float, float]]]:
    """一串 `(起点, 终点)`（`None` 表示这一段没了）接成若干条折线：相邻的接回同一条，
    不足两点的丢掉。裁剪只有「怎么切一段」不同，接段这一步是共用的。"""
    out: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []
    for segment in segments:
        if segment is None:
            if len(current) > 1:
                out.append(current)
            current = []
            continue
        a, b = segment
        if current and _close(current[-1], a):
            current.append(b)
        else:
            if len(current) > 1:
                out.append(current)
            current = [a, b]
    if len(current) > 1:
        out.append(current)
    return out


def _shore_cuts(a, b, polygons: list[dict]) -> list:
    """线段 ab 上的切点，连头带尾按 a→b 排好。一个多边形都没穿过时就是 `[a, b]`。"""
    parameters = {0.0, 1.0}
    for polygon in polygons:
        for ring in (polygon["o"], *polygon.get("i", ())):
            parameters.update(_crossings(a, b, ring))
    if len(parameters) == 2:
        return [a, b]
    return [a if t == 0.0 else b if t == 1.0 else
            (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))
            for t in sorted(parameters)]


def _crossings(a, b, ring: list):
    """线段 ab 穿过一个环的那些交点，返回在 ab 上的参数（0 与 1 不算：贴着端点
    擦过边界不是穿过，那一段的归属交给中点判）。平行与共线不产生交点，同理。"""
    rx, ry = b[0] - a[0], b[1] - a[1]
    for i, c in enumerate(ring):
        d = ring[(i + 1) % len(ring)]
        sx, sy = d[0] - c[0], d[1] - c[1]
        denominator = rx * sy - ry * sx
        if denominator == 0.0:
            continue
        cx, cy = c[0] - a[0], c[1] - a[1]
        t = (cx * sy - cy * sx) / denominator
        u = (cx * ry - cy * rx) / denominator
        if 0.0 < t < 1.0 and 0.0 <= u <= 1.0:
            yield t


def boxes_overlap(a: Box, b: Box) -> bool:
    return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])


def _close(a: tuple[float, float], b: tuple[float, float]) -> bool:
    return abs(a[0] - b[0]) < 1e-6 and abs(a[1] - b[1]) < 1e-6


def _clip_segment(a, b, box: Box):
    x0, y0 = a
    x1, y1 = b
    dx, dy = x1 - x0, y1 - y0
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, x0 - box[0]), (dx, box[2] - x0), (-dy, y0 - box[1]), (dy, box[3] - y0)):
        if p == 0:
            if q < 0:
                return None
            continue
        r = q / p
        if p < 0:
            if r > t1:
                return None
            t0 = max(t0, r)
        else:
            if r < t0:
                return None
            t1 = min(t1, r)
    return (x0 + t0 * dx, y0 + t0 * dy), (x0 + t1 * dx, y0 + t1 * dy)


def clip_ring(ring: list[tuple[float, float]], box: Box) -> list[tuple[float, float]]:
    """Sutherland–Hodgman：把一个环裁进矩形。裁空返回空列表。"""
    poly = list(ring)
    if poly and _close(poly[0], poly[-1]):
        poly = poly[:-1]
    for edge in range(4):
        if not poly:
            return []
        out: list[tuple[float, float]] = []
        for i, cur in enumerate(poly):
            prev = poly[i - 1]
            cur_in = _inside(cur, box, edge)
            prev_in = _inside(prev, box, edge)
            if cur_in:
                if not prev_in:
                    out.append(_edge_cross(prev, cur, box, edge))
                out.append(cur)
            elif prev_in:
                out.append(_edge_cross(prev, cur, box, edge))
        poly = out
    return poly


def _inside(p, box: Box, edge: int) -> bool:
    return (p[0] >= box[0], p[0] <= box[2], p[1] >= box[1], p[1] <= box[3])[edge]


def _edge_cross(a, b, box: Box, edge: int):
    if edge < 2:
        x = box[0] if edge == 0 else box[2]
        t = (x - a[0]) / (b[0] - a[0])
        return x, a[1] + t * (b[1] - a[1])
    y = box[1] if edge == 2 else box[3]
    t = (y - a[1]) / (b[1] - a[1])
    return a[0] + t * (b[0] - a[0]), y


def point_in_ring(point: tuple[float, float], ring: list[tuple[float, float]]) -> bool:
    """射线法。边界上的点算在内还是在外没有定义——归属判定不该踩到这个精度。"""
    x, y = point
    inside = False
    n = len(ring)
    for i in range(n):
        x0, y0 = ring[i]
        x1, y1 = ring[(i + 1) % n]
        if (y0 > y) != (y1 > y):
            xc = x0 + (y - y0) * (x1 - x0) / (y1 - y0)
            if x < xc:
                inside = not inside
    return inside


def point_in_polygon(point: tuple[float, float], polygon: dict) -> bool:
    """`polygon` 形如 `{"o": 外环, "i": [内环…]}`：在外环内且不在任何内环内。"""
    if not point_in_ring(point, polygon["o"]):
        return False
    return not any(point_in_ring(point, hole) for hole in polygon.get("i", ()))


# ---------- OSM 的关系成面 ----------
#
# 行政边界、大湖、大公园在 OSM 里都是「一个关系 + 若干条 way」，way 的顺序与方向都不保证，
# 环得自己接。行政边界与底图面用的是同一件事，所以只有这一份实现。

def stitch_rings(ways: list[list[tuple[float, float]]]) -> tuple[list[Ring], int]:
    """把首尾相接的 way 接成闭合环。返回 `(环, 接不上而丢弃的 way 数)`。

    丢弃是有代价的：边界少一块，落在那里的散步就归属不到这座城；底图少一块，
    图上就少一片水或一块绿地。所以丢了多少要报出来，调用方决定是警告还是当场停。
    """
    remaining = [way for way in ways if len(way) >= 2]
    rings: list[Ring] = []
    dropped = 0
    while remaining:
        ring = remaining.pop()
        while ring[0] != ring[-1]:
            for index, candidate in enumerate(remaining):
                if candidate[0] == ring[-1]:
                    ring += candidate[1:]
                elif candidate[-1] == ring[-1]:
                    ring += candidate[-2::-1]
                elif candidate[-1] == ring[0]:
                    ring = candidate[:-1] + ring
                elif candidate[0] == ring[0]:
                    ring = candidate[:0:-1] + ring
                else:
                    continue
                remaining.pop(index)
                break
            else:
                dropped += 1
                ring = []
                break
        if len(ring) > 3:
            rings.append(ring[:-1])
    return rings, dropped


def assemble_polygons(outer: list[Ring], inner: list[Ring]) -> list[dict]:
    """内环归给包住它的那个外环，一个内环只挂一次。

    「只挂一次」是硬要求：渲染用的是 even-odd，同一条环在同一层出现两次等于没出现，
    洞会被填回去。飞地各成一个多边形，不合并。
    """
    # 从小到大试：一个洞如果落在两个嵌套的外环里，它属于小的那个
    polygons = [{"o": ring, "i": []} for ring in sorted(outer, key=ring_area)]
    for hole in inner:
        for polygon in polygons:
            if point_in_ring(hole[0], polygon["o"]):
                polygon["i"].append(hole)
                break
    return sorted(polygons, key=lambda polygon: ring_area(polygon["o"]), reverse=True)


def bounds(points) -> Box:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def ring_area(ring: list[tuple[float, float]]) -> float:
    """有符号面积的两倍取绝对值再折半；只用来比大小，不做单位换算。"""
    total = 0.0
    n = len(ring)
    for i in range(n):
        x0, y0 = ring[i]
        x1, y1 = ring[(i + 1) % n]
        total += x0 * y1 - x1 * y0
    return abs(total) / 2.0


def polygon_area_km2(polygons: list[dict]) -> float:
    """一组 `{"o": 外环, "i": [内环…]}` 的总面积，平方公里。

    逐块按自己那块的纬度换算度→米：中国南北跨三十五个纬度，一度经度在漠河与三亚
    差着近一倍，用一个全局比例尺会把北方的城算小。分片的平局规则要拿它比大小
    （见 `directory.shard`），所以算的是真实面积而不是 `ring_area` 那个度²的量。
    """
    total = 0.0
    for polygon in polygons:
        outer = polygon["o"]
        if len(outer) < 3:
            continue
        lon_meters, lat_meters = meters_per_degree(sum(y for _, y in outer) / len(outer))
        square_km = lon_meters * lat_meters / 1e6
        total += (ring_area(outer) - sum(ring_area(hole) for hole in polygon["i"])) * square_km
    return total
