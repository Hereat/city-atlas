"""地图包的坐标编码：相对画框中心的**增量整数米**。

格式不是这里发明的，是稿 `reference/citymap.js` 里 `decode()` 的反函数：
一条线是一串扁平整数 `[x0, y0, dx1, dy1, dx2, dy2, …]`，逐对累加还原绝对米制坐标。
一米的量化步长是有意的：城市画框最长边六到十公里，一米在最终图上远小于一个像素，
而整数增量在 gzip 下比浮点数组小一个数量级。
"""

from __future__ import annotations

Point = tuple[float, float]


def encode(points: list[Point]) -> list[int]:
    """折线 → 增量整数。量化后重合的相邻点会被丢掉；不足两点返回空。"""
    if not points:
        return []
    out: list[int] = []
    px = py = 0
    for i, (x, y) in enumerate(points):
        qx, qy = round(x), round(y)
        if i == 0:
            out += [qx, qy]
        elif qx != px or qy != py:
            out += [qx - px, qy - py]
        px, py = qx, qy
    return out if len(out) >= 4 else []


def decode(flat: list[int]) -> list[Point]:
    """增量整数 → 折线。与稿的 `decode()` 逐字对应。"""
    out: list[Point] = []
    x = y = 0
    for i in range(0, len(flat), 2):
        x += flat[i]
        y += flat[i + 1]
        out.append((float(x), float(y)))
    return out


def encode_ring(ring: list[Point]) -> list[int]:
    """环不重复首点——稿的 `polygonPath()` 靠 `closePath()` 闭合。"""
    if len(ring) > 1 and abs(ring[0][0] - ring[-1][0]) < 1e-9 and abs(ring[0][1] - ring[-1][1]) < 1e-9:
        ring = ring[:-1]
    flat = encode(ring)
    return flat if len(flat) >= 6 else []


# 分片里的行政边界用同一套增量整数，只是单位从米换成 1e-5 度（约一米）。
# App 解码用的是同一个 `decode`，除完 `BOUNDARY_SCALE` 就是经纬度。
BOUNDARY_SCALE = 100000


def encode_lonlat(ring: list[Point]) -> list[int]:
    return encode_ring([(lon * BOUNDARY_SCALE, lat * BOUNDARY_SCALE) for lon, lat in ring])


def decode_lonlat(flat: list[int]) -> list[Point]:
    return [(x / BOUNDARY_SCALE, y / BOUNDARY_SCALE) for x, y in decode(flat)]
