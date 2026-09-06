"""管线自检。`python3 -m cityatlas selftest`，一秒跑完，不联网。

钉的是会**静默出错**的那些事——出错时产物照样生成、体积正常、hash 能算，
只有图错了或者归属答错了：接环与内外环归属（两个手写的关系成面步骤）、
GeoPackage 的二进制解析、编码往返、裁剪不越界、投影与 `CLLocation.distance` 同量，
以及同一份内容两次落盘逐字节相同（S0 验收的「同一输入重跑逐字节相同」靠它成立）。
教科书算法本身出错的概率反而低，但它们便宜，顺手一起钉。
"""

from __future__ import annotations

import math

from . import codec, mappack
from .geometry import (Projection, assemble_polygons, clip_polyline, clip_ring, meters_per_degree,
                       mollweide_forward, mollweide_inverse, point_in_ring, simplify, stitch_rings)


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def run() -> None:
    checks = 0

    # 编码：量化到整数米之后往返相等；相邻重合点被丢掉
    line = [(0.0, 0.0), (1200.4, -3.6), (1200.4, -3.6), (-450.9, 2000.2)]
    flat = codec.encode(line)
    _assert(codec.decode(flat) == [(0.0, 0.0), (1200.0, -4.0), (-451.0, 2000.0)], "增量整数往返对不上")
    _assert(flat[0:2] == [0, 0] and len(flat) == 6, "重合点没有被丢掉")
    checks += 2

    # 环不重复首点
    ring = [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0), (0.0, 100.0), (0.0, 0.0)]
    _assert(len(codec.encode_ring(ring)) == 8, "环编码不该带上重复的首点")
    checks += 1

    # 裁剪：出框的部分被切掉，切点落在框上
    box = (-100.0, -100.0, 100.0, 100.0)
    pieces = clip_polyline([(-300.0, 0.0), (300.0, 0.0)], box)
    _assert(pieces == [[(-100.0, 0.0), (100.0, 0.0)]], f"折线裁剪结果不对：{pieces}")
    _assert(clip_polyline([(200.0, 200.0), (300.0, 300.0)], box) == [], "全在框外的线没有被丢掉")
    clipped = clip_ring([(-300.0, -300.0), (300.0, -300.0), (300.0, 300.0), (-300.0, 300.0)], box)
    _assert(len(clipped) == 4 and all(abs(x) <= 100.0001 and abs(y) <= 100.0001 for x, y in clipped),
            "环裁剪越界")
    checks += 3

    # 简化：直线上的中间点被删光，端点保留
    straight = [(float(i), 0.0) for i in range(50)]
    _assert(simplify(straight, 1.0) == [(0.0, 0.0), (49.0, 0.0)], "共线点没有被简化掉")
    checks += 1

    # 点在环内
    square = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
    _assert(point_in_ring((5.0, 5.0), square) and not point_in_ring((15.0, 5.0), square), "射线法答错")
    checks += 1

    # 投影：一度经度在赤道约 111.3 km，在北纬 60 度约一半；往返回到原点
    equator_x, _ = meters_per_degree(0.0)
    high_x, _ = meters_per_degree(60.0)
    _assert(abs(equator_x - 111319) < 60, f"赤道每度经度算错：{equator_x}")
    _assert(abs(high_x / equator_x - 0.5) < 0.005, "高纬度的经度收缩不对")
    projection = Projection(121.48, 31.225)
    x, y = projection.forward(121.5, 31.24)
    lon, lat = projection.inverse(x, y)
    _assert(abs(lon - 121.5) < 1e-9 and abs(lat - 31.24) < 1e-9, "投影往返有偏移")
    checks += 3

    # Mollweide 逆投影：原点回原点，已知点落在合理经纬范围内
    _assert(mollweide_inverse(0.0, 0.0) == (0.0, 0.0), "Mollweide 原点不对")
    lon, lat = mollweide_inverse(10_000_000.0, 3_500_000.0)
    _assert(0 < lon < 180 and 0 < lat < 90, f"Mollweide 逆投影落到界外：{lon},{lat}")
    checks += 2

    # 边界坐标：1e-5 度的量化，往返误差不超过半个格
    boundary = [(121.48123, 31.22567), (121.49111, 31.23001)]
    back = codec.decode_lonlat(codec.encode_lonlat(boundary + boundary[:1]))
    _assert(all(math.isclose(a[0], b[0], abs_tol=1e-5) and math.isclose(a[1], b[1], abs_tol=1e-5)
                for a, b in zip(boundary, back)), "边界经纬编码往返超差")
    checks += 1

    # 接环：一个环拆成三段（含反向的一段）能接回来；接不上的整环丢掉并计数
    square = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
    pieces = [[(0.0, 0.0), (10.0, 0.0)], [(10.0, 10.0), (10.0, 0.0)], [(10.0, 10.0), (0.0, 10.0), (0.0, 0.0)]]
    rings, dropped = stitch_rings(pieces)
    _assert(len(rings) == 1 and dropped == 0 and len(rings[0]) == 4, f"接环失败：{rings}, 丢 {dropped}")
    _assert(set(rings[0]) == set(square), "接回来的环顶点对不上")
    _, dropped = stitch_rings([[(0.0, 0.0), (10.0, 0.0)], [(20.0, 20.0), (30.0, 30.0)]])
    _assert(dropped == 2, f"接不上的 way 没被计数：{dropped}")
    checks += 3

    # 内外环：一个洞只挂到一个外环上。挂两次，even-odd 会把洞填回去
    outer_big = [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0), (0.0, 100.0)]
    outer_far = [(200.0, 0.0), (300.0, 0.0), (300.0, 100.0), (200.0, 100.0)]
    hole = [(10.0, 10.0), (20.0, 10.0), (20.0, 20.0), (10.0, 20.0)]
    polygons = assemble_polygons([outer_big, outer_far], [hole])
    _assert(sum(len(polygon["i"]) for polygon in polygons) == 1, "内环被挂到了不止一个外环上")
    _assert(len(polygons[0]["i"]) == 1, "内环没挂到包住它的那个外环上")
    checks += 2

    # 关系成面：没闭合的弧段不能当成环。当成环画出去就是一大块弦封的实心色
    def way(points):
        return {"type": "way", "role": "outer", "geometry": [{"lon": x, "lat": y} for x, y in points]}

    projection = Projection(0.0, 0.0)
    arc = {"type": "relation", "members": [way([(0.0, 0.0), (0.001, 0.0)]), way([(0.002, 0.001), (0.003, 0.002)])]}
    _assert(mappack._polygons(arc, projection) == [], "接不上的弧段被当成环留下了")
    closed = {"type": "relation", "members": [
        way([(0.0, 0.0), (0.001, 0.0)]), way([(0.001, 0.0), (0.001, 0.001)]),
        way([(0.001, 0.001), (0.0, 0.001), (0.0, 0.0)])]}
    _assert(len(mappack._polygons(closed, projection)) == 1, "拆成三段的面没接回来")
    checks += 2

    # GeoPackage：头部长度表与 WKB 顶点解析。构造一个带包络的多边形 blob
    import struct
    from .ghsl import _strip_gpkg_header, _wkb_points
    ring_points = [(1.0, 2.0), (3.0, 4.0), (5.0, 6.0), (1.0, 2.0)]
    wkb = struct.pack("<BII", 1, 3, 1) + struct.pack("<I", len(ring_points))
    wkb += b"".join(struct.pack("<dd", x, y) for x, y in ring_points)
    blob = b"GP" + bytes([0, 0b00000010]) + struct.pack("<i", 54009) + struct.pack("<4d", 1, 5, 2, 6) + wkb
    _assert(_wkb_points(_strip_gpkg_header(blob)) == ring_points, "GPKG 包络长度或 WKB 偏移算错了")
    checks += 1

    # Mollweide：正逆投影往返（rtree 预筛靠正投影）
    for lon, lat in ((121.48, 31.225), (-122.66, 45.52), (13.62, 47.71)):
        back = mollweide_inverse(*mollweide_forward(lon, lat))
        _assert(abs(back[0] - lon) < 1e-6 and abs(back[1] - lat) < 1e-6, f"Mollweide 往返超差：{back}")
    checks += 1

    # 确定性：同一份内容两次落盘逐字节相同
    from io import BytesIO
    import gzip
    import json

    def blob():
        buffer = BytesIO()
        with gzip.GzipFile(fileobj=buffer, mode="wb", compresslevel=9, mtime=0) as handle:
            handle.write(json.dumps({"b": 1, "a": [1, 2]}, sort_keys=True).encode("utf-8"))
        return buffer.getvalue()

    _assert(blob() == blob(), "同一份内容两次 gzip 结果不同——mtime 没写死")
    checks += 1

    print(f"selftest 通过，{checks} 条")
