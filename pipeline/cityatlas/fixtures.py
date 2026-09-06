"""把稿的上海、杭州转成 `HereatTests/Fixtures/city-atlas/`。

夹具不走管线：它的矢量数据**就是稿里那两座城的原样**，这样 S1 的渲染器截图能和稿并排比，
比出来的差异只可能来自渲染，不会掺进管线的简化与量化。
补上的只有 manifest 那几列和一份矩形的行政边界——UI 测试要的是「起点落在上海还是杭州」，
一个矩形就够回答，把真边界搬进夹具只会让测试数据大一个数量级。

分片走 `directory` 里那一套（同一个裁格、同一种编码、同一个 `shard()`），
所以夹具与线上不只是「路径一样」，是同一条代码路径产出来的。

文件名是 CDN 路径把 `/` 换成 `-`：`package/c00001/1.json.gz` → `package-c00001-1.json.gz`。
不保留目录层级，是因为测试 bundle 里的资源是扁平的，两座城的 `1.json.gz` 会撞名；
夹具版 `CityAtlas` 按同一条规则把请求路径映射成资源名。
"""

from __future__ import annotations

import json
from pathlib import Path

from . import DIRECTORY_VERSION, MAP_DATA_VERSION, directory, mappack, publish
from .boundary import Boundary
from .frame import bounds_of

# 夹具里的「行政边界」是画框外扩这么多倍的矩形。三倍足够让画框内的散步都落在城内，
# 又不会让相距一百三十公里的上海与杭州两个矩形碰上。
RECT_INFLATION = 3.0

# 稿里属于「合成散步」而不属于地图包的那几列，转夹具时丢掉。
DEMO_ONLY = ("walks", "snapped", "stays", "home", "since", "km")


def build(root: Path, repo: Path) -> list[Path]:
    registry = publish.load_registry(root / "registry.json")
    by_slug = {city.get("slug"): city for city in registry["cities"].values()}
    out = repo / "app/HereatTests/Fixtures/city-atlas"
    licenses = publish.license_block({"osm": "稿 2026-09-04-散步城市图 · 优化稿（离线）.html",
                                      "note": "夹具，不是生产数据"})
    written: list[Path] = []
    shard_cities: dict[tuple[int, int], list[dict]] = {}

    for slug in ("shanghai", "hangzhou"):
        if slug not in by_slug:
            raise SystemExit(f"registry.json 里还没有 {slug} 的 cityID，先跑 `python3 -m cityatlas build`")
        city_id = by_slug[slug]["cityID"]
        city = json.loads((root / "reference" / "cities" / f"{slug}.json").read_text(encoding="utf-8"))
        package = {key: value for key, value in city.items() if key not in DEMO_ONLY}
        package["nameLocal"] = package["name"]
        package.update({
            "cityID": city_id,
            "directoryVersion": DIRECTORY_VERSION,
            "mapDataVersion": MAP_DATA_VERSION,
            "bounds": list(bounds_of(*package["center"], *package["frame"])),
            "license": licenses,
        })
        package["contentHash"] = mappack.content_hash(package)
        written.append(_write(out / f"package-{city_id}-{MAP_DATA_VERSION}.json.gz", package))

        entry = {"cityID": city_id, "name": package["name"], "nameLocal": package["nameLocal"],
                 "mapDataVersion": MAP_DATA_VERSION, "center": package["center"],
                 "frame": package["frame"], "bounds": package["bounds"]}
        for cell, shard_entry in directory.entries(entry, _rectangle_boundary(package)).items():
            shard_cities.setdefault(cell, []).append(shard_entry)

    for cell, cities in sorted(shard_cities.items()):
        written.append(_write(out / f"directory-v{DIRECTORY_VERSION}-{directory.cell_name(cell)}.json.gz",
                              directory.shard(cell, cities, licenses)))

    latest = out / "directory-latest.json"
    latest.parent.mkdir(parents=True, exist_ok=True)
    latest.write_text(json.dumps(publish.latest(DIRECTORY_VERSION, {"note": "夹具"}),
                                 ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    written.append(latest)
    return written


def _rectangle_boundary(package: dict) -> Boundary:
    lon, lat = package["center"]
    west, south, east, north = bounds_of(lon, lat,
                                         package["frame"][0] * RECT_INFLATION,
                                         package["frame"][1] * RECT_INFLATION)
    return Boundary(osm_relation=0, admin_level=0, name_zh=package["name"], name_local=package["name"],
                    polygons=[{"o": [(west, south), (east, south), (east, north), (west, north)], "i": []}])


def _write(path: Path, payload) -> Path:
    publish.write_gzip_json(path, payload)
    return path
