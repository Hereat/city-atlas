"""读 GHSL Urban Centre Database（GeoPackage）。

GeoPackage 就是 SQLite，标准库 `sqlite3` 直接打开；几何是 GPKG blob 包一层 WKB。
我们只要三样东西：城市中心的质心经纬、2025 年人口、外接框——都能自己解出来，
不必为此装 GDAL。UCDB 的几何存在 ESRI:54009（Mollweide 米），逆投影见 `geometry.mollweide_inverse`。

数据下载：https://human-settlement.emergency.copernicus.eu/ghs_ucdb_2024.php
许可 CC BY 4.0（(c) European Union）——**画框是它的衍生物，署名义务跟着走**，见 `publish.LICENSES`。
"""

from __future__ import annotations

import sqlite3
import struct
from dataclasses import dataclass
from pathlib import Path

from .geometry import mollweide_forward, mollweide_inverse

# 「总体特征」那张主题表的名字**按模式查，不写死**：UCDB 的各个小版本前缀不一致
# （V1-0 是 `GHS_UCDB_THEME_…`，另一些版本是 `GHSL_UCDB_THEME_…`），写死会让换一版数据
# 就整条管线报「no such table」。要的表全库只有一张，模式匹配不会有歧义。
_THEME_PATTERN = "%UCDB_THEME_GENERAL_CHARACTERISTICS%"


def _theme_table(connection) -> str:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE ? AND name NOT LIKE 'rtree_%'",
        (_THEME_PATTERN,),
    ).fetchall()
    if len(rows) != 1:
        raise ValueError(f"UCDB 里「总体特征」表不是恰好一张：{[r[0] for r in rows]}")
    return rows[0][0]


@dataclass(frozen=True)
class UrbanCentre:
    uc_id: int
    name: str
    country: str
    population: float
    area_km2: float
    lon: float
    lat: float


class UCDB:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        self._theme = _theme_table(self.connection)
        self._centres: list[UrbanCentre] | None = None

    def _centroid_columns(self) -> tuple[str, str]:
        """质心那两列的名字也随版本变：V1-0 是 `PWCentroidX/Y`（人口加权质心），
        另一些版本是 `GC_UCC_LON/LAT_2025`。两种都认，认不出就直说是哪个版本不支持。"""
        columns = {row[1] for row in self.connection.execute('PRAGMA table_info("UC_centroids")')}
        for x, y in (("PWCentroidX", "PWCentroidY"), ("GC_UCC_LON_2025", "GC_UCC_LAT_2025")):
            if {x, y} <= columns:
                return x, y
        raise ValueError(f"UC_centroids 里认不出质心那两列：{sorted(columns)}")

    def centres(self) -> list[UrbanCentre]:
        """全部一万多个城市中心。质心那两列名不副实，存的是 Mollweide 米，
        所以这里逐行逆投影；一万一千行一次扫完，不值得为它建索引。"""
        if self._centres is None:
            x_column, y_column = self._centroid_columns()
            rows = self.connection.execute(
                f"""SELECT t.ID_UC_G0, t.GC_UCN_MAI_2025, t.GC_CNT_GAD_2025, t.GC_POP_TOT_2025, t.GC_UCA_KM2_2025,
                           c.{x_column}, c.{y_column}
                      FROM {self._theme} AS t
                      JOIN UC_centroids AS c ON c.ID_UC_G0 = t.ID_UC_G0"""
            ).fetchall()
            self._centres = [
                UrbanCentre(r[0], r[1] or "", r[2] or "", r[3] or 0.0, r[4] or 0.0,
                            *mollweide_inverse(r[5], r[6]))
                for r in rows
            ]
        return self._centres

    def centres_intersecting(self, box) -> list[UrbanCentre]:
        """**几何**与经纬矩形相交的城市中心。

        走 GeoPackage 自带的 R-tree 索引：它存的是 Mollweide 的外接框，
        把矩形正投影过去查一次就够，不必解一万一千个多边形，也不必自己编一个
        「质心在多远以内」的经验半径——那种数会在珠三角、东京—横滨这种连片城市中心上失效。
        """
        corners = _mollweide_envelope(box)
        rows = self.connection.execute(
            f"""SELECT t.ID_UC_G0 FROM rtree_{self._theme}_geom AS r
                  JOIN {self._theme} AS t ON t.fid = r.id
                 WHERE r.maxx >= ? AND r.minx <= ? AND r.maxy >= ? AND r.miny <= ?""",
            (corners[0], corners[2], corners[1], corners[3]),
        ).fetchall()
        wanted = {row[0] for row in rows}
        return [centre for centre in self.centres() if centre.uc_id in wanted]

    def polygons(self, uc_id: int) -> list[dict]:
        """城市中心的多边形，WGS-84，形如 `{"o": 外环, "i": [内环…]}`——
        与 `boundary.Boundary.polygons` 同一个形状，所以归属判定那一路不必分两种来源。
        """
        blob = self.connection.execute(
            f"SELECT geom FROM {self._theme} WHERE ID_UC_G0 = ?", (uc_id,)
        ).fetchone()[0]
        return [
            {"o": [mollweide_inverse(x, y) for x, y in polygon["o"]],
             "i": [[mollweide_inverse(x, y) for x, y in ring] for ring in polygon["i"]]}
            for polygon in _wkb_polygons(_strip_gpkg_header(blob))
        ]

    def centres_in(self, country: str) -> list[UrbanCentre]:
        """一个国家的全部城市中心。`GC_CNT_GAD_2025` 是 GADM 的国名。"""
        return [centre for centre in self.centres() if centre.country == country]

    def bbox(self, uc_id: int):
        """城市中心多边形的 WGS-84 外接框 `(min_lon, min_lat, max_lon, max_lat)`。"""
        blob = self.connection.execute(
            f"SELECT geom FROM {self._theme} WHERE ID_UC_G0 = ?", (uc_id,)
        ).fetchone()[0]
        points = [mollweide_inverse(x, y) for x, y in _wkb_points(_strip_gpkg_header(blob))]
        lons = [p[0] for p in points]
        lats = [p[1] for p in points]
        return min(lons), min(lats), max(lons), max(lats)


def _mollweide_envelope(box):
    """经纬矩形在 Mollweide 平面上的外接框。Mollweide 的经线是弯的，
    所以沿矩形四边采样再取极值，不能只投四个角。"""
    steps = 16
    xs, ys = [], []
    for i in range(steps + 1):
        t = i / steps
        for lon, lat in ((box[0] + (box[2] - box[0]) * t, box[1]),
                         (box[0] + (box[2] - box[0]) * t, box[3]),
                         (box[0], box[1] + (box[3] - box[1]) * t),
                         (box[2], box[1] + (box[3] - box[1]) * t)):
            x, y = mollweide_forward(lon, lat)
            xs.append(x)
            ys.append(y)
    return min(xs), min(ys), max(xs), max(ys)


def _strip_gpkg_header(blob: bytes) -> bytes:
    """GPKG blob = 'GP' 魔数 + 版本 + 标志 + srs_id + 可选包络 + WKB。"""
    if blob[:2] != b"GP":
        raise ValueError("不是 GeoPackage 几何")
    flags = blob[3]
    envelope_doubles = (0, 4, 6, 6, 8)[(flags >> 1) & 0x07]
    return blob[8 + envelope_doubles * 8:]


def _wkb_points(wkb: bytes) -> list[tuple[float, float]]:
    """只取顶点坐标——我们要的是外接框，不关心环的层级。支持 (Multi)Polygon 与集合。"""
    points: list[tuple[float, float]] = []

    def read(offset: int) -> int:
        endian = "<" if wkb[offset] == 1 else ">"
        raw_type = struct.unpack_from(endian + "I", wkb, offset + 1)[0]
        if raw_type >= 1000:
            # 带 Z / M 的几何每个点不是两个 double，下面的偏移推进会整段读错位而不报错。
            # UCDB 是纯 2D；真遇到带高程的版本，宁可在这里停。
            raise ValueError(f"只支持二维几何，读到类型码 {raw_type}")
        geometry_type = raw_type
        offset += 5
        if geometry_type == 1:  # Point
            points.append(struct.unpack_from(endian + "dd", wkb, offset))
            return offset + 16
        if geometry_type in (2, 3):  # LineString / Polygon
            rings = 1
            if geometry_type == 3:
                rings = struct.unpack_from(endian + "I", wkb, offset)[0]
                offset += 4
            for _ in range(rings):
                count = struct.unpack_from(endian + "I", wkb, offset)[0]
                offset += 4
                flat = struct.unpack_from(endian + "%dd" % (count * 2), wkb, offset)
                points.extend((flat[i], flat[i + 1]) for i in range(0, count * 2, 2))
                offset += count * 16
            return offset
        count = struct.unpack_from(endian + "I", wkb, offset)[0]  # Multi* / Collection
        offset += 4
        for _ in range(count):
            offset = read(offset)
        return offset

    read(0)
    return points


def _wkb_polygons(wkb: bytes) -> list[dict]:
    """(Multi)Polygon → `[{"o": 外环, "i": [内环…]}]`，保留环的层级。

    与 `_wkb_points` 是两个用途：那个只要外接框，把所有点拍平即可；
    判「这个坐标在不在这座城里」必须分得清外环与洞。
    """
    polygons: list[dict] = []

    def read(offset: int) -> int:
        endian = "<" if wkb[offset] == 1 else ">"
        raw_type = struct.unpack_from(endian + "I", wkb, offset + 1)[0]
        if raw_type >= 1000:
            raise ValueError(f"只支持二维几何，读到类型码 {raw_type}")
        offset += 5
        if raw_type == 3:  # Polygon
            count = struct.unpack_from(endian + "I", wkb, offset)[0]
            offset += 4
            rings = []
            for _ in range(count):
                n = struct.unpack_from(endian + "I", wkb, offset)[0]
                offset += 4
                flat = struct.unpack_from(endian + "%dd" % (n * 2), wkb, offset)
                rings.append([(flat[i], flat[i + 1]) for i in range(0, n * 2, 2)])
                offset += n * 16
            polygons.append({"o": rings[0], "i": rings[1:]})
            return offset
        if raw_type in (4, 6, 7):  # MultiPoint / MultiPolygon / Collection
            count = struct.unpack_from(endian + "I", wkb, offset)[0]
            offset += 4
            for _ in range(count):
                offset = read(offset)
            return offset
        raise ValueError(f"城市中心的几何不是面，类型码 {raw_type}")

    read(0)
    return polygons
