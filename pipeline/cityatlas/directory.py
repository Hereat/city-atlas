"""目录分片：按 1°×1° 的格切开的「这一格里有哪几座城」。

App 只知道散步起点落在哪一格，下载那一格的分片，在本地做点在多边形内的判断。
CDN 因此只看到格号，看不到坐标——这是 PRD 第 6 节那句「归属判定在手机上完成」的载体。
"""

from __future__ import annotations

import math

from . import DIRECTORY_VERSION, codec
from .boundary import simplified
from .geometry import clip_ring, polygon_area_km2

# 边界简化容差（度）。1e-4 度约 11 米：判断一次散步的起点算哪座城，
# 十米的边界抖动改不了答案，而它把边界点数压掉一个数量级。
TOLERANCE = 1e-4


def cells_of(box) -> list[tuple[int, int]]:
    """一个经纬矩形压到的全部 1° 格，返回 `(lat, lon)` 的整数下标。"""
    return [(lat, lon)
            for lat in range(math.floor(box[1]), math.floor(box[3]) + 1)
            for lon in range(math.floor(box[0]), math.floor(box[2]) + 1)]


def cell_box(cell) -> tuple[float, float, float, float]:
    lat, lon = cell
    return float(lon), float(lat), float(lon + 1), float(lat + 1)


def cell_name(cell) -> str:
    return f"{cell[0]}_{cell[1]}"


def cell_of(name: str) -> tuple[int, int]:
    """`cell_name` 的逆。增量出包要按文件名认出格号才能把别国的条目并回去。"""
    lat, lon = name.split("_")
    return int(lat), int(lon)


def entries(city: dict, boundary) -> dict[tuple[int, int], dict]:
    """一座城在它压到的每个格里的分片条目。裁空的格不出现。

    `areaKm2` 是这座城**整个行政区**的面积（不是裁到格内那块），分片里的平局规则
    要用它排序，见 `shard`。逐格重复写同一个数是有意的：一次归属只下载一个格，
    那一格里就得有排序要用的全部信息。
    """
    polygons = simplified(boundary, TOLERANCE)
    area = round(polygon_area_km2(boundary.polygons), 1)
    out: dict[tuple[int, int], dict] = {}
    for cell in cells_of(boundary.bbox):
        box = cell_box(cell)
        clipped = []
        for polygon in polygons:
            outer = clip_ring(polygon["o"], box)
            if len(outer) < 4:
                continue
            encoded = codec.encode_lonlat(outer)
            if not encoded:
                continue
            holes = []
            for hole in polygon["i"]:
                ring = clip_ring(hole, box)
                if len(ring) < 4:
                    continue
                encoded_hole = codec.encode_lonlat(ring)
                if encoded_hole:
                    holes.append(encoded_hole)
            clipped.append({"o": encoded, "i": holes})
        if not clipped:
            continue
        out[cell] = {
            "cityID": city["cityID"],
            "name": city["name"],
            "nameLocal": city["nameLocal"],
            "nameEn": city["nameEn"],
            "mapDataVersion": city["mapDataVersion"],
            "areaKm2": area,
            "frame": {"center": city["center"], "size": city["frame"], "bounds": city["bounds"]},
            "boundary": clipped,
        }
    return out


def shard(cell, cities: list[dict], license_block: dict) -> dict:
    """一格的分片。**城市按面积从小到大排，这个顺序就是平局规则。**

    一个起点常常同时落在好几座城的边界里——中国的地级市把它下辖的县级市整个包住，
    义乌的每一步都同时在义乌市内和金华市内。App 取第一个包含起点的城（`CityDirectoryShard`
    里那句「顺序即平局规则」），所以谁排前面就是答案。

    该答哪一座？**最小的那座**——用户说的是「在义乌散步」，不会说「在金华散步」；
    这也是名单那一头 `frame.smallest_containing` 的同一条判据，两处该是一套说法。
    先前这里按 `cityID` 排，而 `cityID` 只是发号顺序，等于按「哪座城先被生成」随机选：
    全国 1458 座里有 1078 座被自己的上级市盖住（2026-09-08 实测），
    S0 那轮把远郊建成区单独立城的成果在归属这一步全被吃掉了。
    """
    return {
        "directoryVersion": DIRECTORY_VERSION,
        "cell": [cell[0], cell[1]],
        "boundaryScale": codec.BOUNDARY_SCALE,
        "cities": sorted(cities, key=lambda city: (city["areaKm2"], city["cityID"])),
        "license": license_block,
    }
