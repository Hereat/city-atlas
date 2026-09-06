"""目录分片：按 1°×1° 的格切开的「这一格里有哪几座城」。

App 只知道散步起点落在哪一格，下载那一格的分片，在本地做点在多边形内的判断。
CDN 因此只看到格号，看不到坐标——这是 PRD 第 6 节那句「归属判定在手机上完成」的载体。
"""

from __future__ import annotations

import math

from . import DIRECTORY_VERSION, codec
from .boundary import simplified
from .geometry import clip_ring

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


def entries(city: dict, boundary) -> dict[tuple[int, int], dict]:
    """一座城在它压到的每个格里的分片条目。裁空的格不出现。"""
    polygons = simplified(boundary, TOLERANCE)
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
            "mapDataVersion": city["mapDataVersion"],
            "frame": {"center": city["center"], "size": city["frame"], "bounds": city["bounds"]},
            "boundary": clipped,
        }
    return out


def shard(cell, cities: list[dict], license_block: dict) -> dict:
    return {
        "directoryVersion": DIRECTORY_VERSION,
        "cell": [cell[0], cell[1]],
        "boundaryScale": codec.BOUNDARY_SCALE,
        "cities": sorted(cities, key=lambda city: city["cityID"]),
        "license": license_block,
    }
