"""从区域 `.osm.pbf` 出快照——一次下载一个国家，一趟本地切出上万座城。

**为什么不是 Overpass**（`overpass.py` 至今仍是七座样本城那条路）：那边是逐城一次请求、
每次三四分钟，且是志愿者运营的公共服务，串行还得歇三秒。一万一千座城按那条路要一个月，
不是慢，是不该那么用。`overpass.py` 的头注里早写了这句：「城市数量涨到几百上千、
Overpass 不再合适时，换的是产出这份快照的程序，不是下游任何一步。」这就是那个程序。

三步，都靠 osmium：

1. `filter_tags` 在整个国家的文件上过一遍，只留我们画得到的标签。1.6 GB 会掉到几分之一，
   后面每座城的切割都从这份小文件上走。
2. `extract` 用一份配置一次切出所有城市的框。**分批**是因为 osmium 会同时打开所有输出文件，
   macOS 默认的文件描述符上限是 256。
3. `export` 把每座城的 pbf 转成 GeoJSONSeq，再由 `to_elements` 折成 Overpass 那种形状。
   面在这一步已经由 osmium 接好环（多边形关系与边界关系都会被拼成 multipolygon），
   所以下游那份接环实现在这条路上不会被用到——它仍然服务 Overpass 那条路。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

# 与 `overpass.FEATURE_QUERY` 同一批标签。两边必须一致，否则同一座城从两条路出来的包不一样。
KEEP = [
    "nwr/highway", "nwr/railway", "nwr/natural", "nwr/waterway",
    "nwr/landuse", "nwr/leisure",
]

# 「市」在各国是第几级、叫什么。行政边界回答的是「这是哪座城」，而这个级别各国不同——
# **逐国一行，不是逐城一行**，所以它是一张小表而不是一份清单。
# 出处：OSM wiki 的 Tag:boundary=administrative，各国 admin_level 对照。
#
# `suffix` 不是可有可无的修饰：中国的 `admin_level=4` 是**省级**，省、自治区、直辖市
# 同在这一层，只有直辖市是城市；这一层还混着「苏皖界」这类省界线。按名字结尾筛「市」
# 一次把三件事都解决了（2026-09-06 实测：不筛会把广东省当成一座城）。
CITY_LEVELS = {
    "China": {"levels": (4, 5), "suffix": "市"},   # 直辖市 4，地级市 5；省与自治区同在 4，靠 suffix 排除
    "Japan": {"levels": (7,)},                     # 市
    "United States": {"levels": (8,)},             # city / town
    "Netherlands": {"levels": (8,)},               # gemeente
    "Austria": {"levels": (8,)},                   # Gemeinde
}

# 一批切多少座城。两条约束，取小的那个：
#
# * osmium 同时打开全部输出文件，macOS 默认 `ulimit -n` 是 256。
# * `-s smart` 要在内存里缓存跨框的关系成员，缓存量随框数涨。**120 个框会把 24 GB
#   打爆**（2026-09-06 实测，被 SIGKILL）。40 个框实测稳。
#
# 代价是每批都要完整扫一遍区域文件（1.2 GB），289 座要八趟。抓取本来就是一次性的，
# 用二十分钟换「不用守着看它会不会被杀」是划算的。
BATCH = 40


def run(*args: str) -> None:
    subprocess.run([str(a) for a in args], check=True)


def filter_tags(source: Path, out: Path) -> Path:
    """整个国家过一遍标签。只做一次，之后所有切割都从这份小文件上走。"""
    if out.exists():
        return out
    run("osmium", "tags-filter", source, *KEEP, "-o", out, "--overwrite")
    return out


def extract(source: Path, boxes: dict[str, tuple], out_dir: Path) -> dict[str, Path]:
    """`boxes` 是 `{slug: (min_lon, min_lat, max_lon, max_lat)}`，一批一次切。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {slug: out_dir / f"{slug}.osm.pbf" for slug in boxes}

    # 切好的文件要记住**自己是按哪个框切的**。画框规则一改，同一个 slug 要的就是另一片
    # 数据，而文件名没变——只看文件在不在会把旧框的产物当成新的用，图上少一半城区却
    # 什么都不报（Overpass 那条路的 `__query__` 比对防的是同一件事）。
    stamp_path = out_dir / "boxes.json"
    stamps = json.loads(stamp_path.read_text(encoding="utf-8")) if stamp_path.exists() else {}

    def stale(slug: str, path: Path) -> bool:
        # 判「切过没有」看的是**有没有内容**，不是文件在不在：osmium 被中途杀掉（内存
        # 打爆就会）会留下零字节的残file，只看 `exists()` 会把它们当成切好的跳过去，
        # 到出包那一步才炸，而且炸在离现场很远的地方（2026-09-07 实测，120 个残file）。
        # 一个空的 pbf 至少也有文件头几百字节；用 1 KB 当「显然是残的」的界。
        if not path.exists() or path.stat().st_size < 1024:
            return True
        return [round(v, 6) for v in stamps.get(slug, [])] != [round(v, 6) for v in boxes[slug]]

    todo = [slug for slug, path in paths.items() if stale(slug, path)]
    for start in range(0, len(todo), BATCH):
        batch = todo[start:start + BATCH]
        config = out_dir / "extract.json"
        config.write_text(json.dumps({"extracts": [
            {"output": paths[slug].name, "bbox": list(boxes[slug])} for slug in batch
        ], "directory": str(out_dir)}), encoding="utf-8")
        # -s smart：完整保留跨框的 way 与关系，边界上的路不会被切成半截
        run("osmium", "extract", "-c", config, "-s", "smart", "--overwrite", source)
        for slug in batch:
            stamps[slug] = list(boxes[slug])
        stamp_path.write_text(json.dumps(stamps), encoding="utf-8")
    return paths


def to_elements(city_pbf: Path) -> dict:
    """一座城的 pbf → `{"elements": [...]}`，形状与 Overpass 的 `out geom` 一致。

    面拆成两种：没有洞的当闭合 way，有洞的当带 outer/inner 成员的关系——
    下游 `mappack._polygons` 认的就是这两种，不必为这条路另开一个分支。
    """
    finished = subprocess.run(
        ["osmium", "export", str(city_pbf), "-f", "geojsonseq",
         "--geometry-types=linestring,polygon"],
        capture_output=True, text=True,
    )
    if finished.returncode != 0:
        # 不能只 `check=True`：那样 osmium 说的话（「blob contains no data」之类）
        # 全被吞在 stderr 里，异常上只剩一句「退出码 1」，等于蒙着眼查
        raise RuntimeError(f"{city_pbf.name}：osmium export 失败——"
                           f"{finished.stderr.strip() or '没有输出'}")
    raw = finished.stdout

    elements: list[dict] = []
    for line in raw.splitlines():
        line = line.strip().lstrip("\x1e")          # GeoJSONSeq 的记录分隔符
        if not line:
            continue
        feature = json.loads(line)
        tags = feature.get("properties") or {}
        geometry = feature.get("geometry") or {}
        kind, coordinates = geometry.get("type"), geometry.get("coordinates")
        if not coordinates:
            continue
        if kind == "LineString":
            elements.append({"type": "way", "tags": tags, "geometry": _points(coordinates)})
        elif kind == "MultiLineString":
            for line_coordinates in coordinates:
                elements.append({"type": "way", "tags": tags, "geometry": _points(line_coordinates)})
        elif kind == "Polygon":
            elements.append(_polygon_element(tags, coordinates))
        elif kind == "MultiPolygon":
            for polygon in coordinates:
                elements.append(_polygon_element(tags, polygon))
    return {"elements": elements}


def _points(coordinates) -> list[dict]:
    return [{"lon": lon, "lat": lat} for lon, lat in coordinates]


def _polygon_element(tags: dict, rings) -> dict:
    outer, holes = rings[0], rings[1:]
    if not holes:
        return {"type": "way", "tags": tags, "geometry": _points(outer)}
    members = [{"type": "way", "role": "outer", "geometry": _points(outer)}]
    members += [{"type": "way", "role": "inner", "geometry": _points(ring)} for ring in holes]
    return {"type": "relation", "tags": tags, "members": members}


def source_stamp(source: Path) -> dict:
    """这份区域文件是什么时候的 OSM。进每个包的 manifest，与 Overpass 那条路的
    `timestamp_osm_base` 是同一个意思。"""
    info = subprocess.run(["osmium", "fileinfo", "-e", "-j", str(source)],
                          check=True, capture_output=True, text=True).stdout
    data = json.loads(info)
    return {
        "source": source.name,
        "osmBase": (data.get("header", {}).get("option", {}) or {}).get("osmosis_replication_timestamp"),
    }


def admin_boundaries(source: Path, out: Path, levels: tuple[int, ...]) -> Path:
    """把整个区域里的市级行政边界抽成一份 GeoJSONSeq。

    **这一步取代了逐城手写的 `cities.json`。** 手写清单存在的唯一理由是 Overpass 要
    逐城查询；区域文件在手上，边界就是一次筛选的事——城市名单与边界一起出来了。
    """
    if out.exists():
        return out
    filtered = out.with_suffix(".pbf")
    if not filtered.exists():
        run("osmium", "tags-filter", source, "r/boundary=administrative", "-o", filtered, "--overwrite")
    levels_set = {str(level) for level in levels}
    with out.open("w", encoding="utf-8") as sink:
        process = subprocess.Popen(
            # -u type_id：给每个 feature 一个 `@id`（形如 `a1826135`）。**不能省**——
            # 没有它，每座城的身份都是 None，名册会把全国挤成一条（2026-09-06 实测）
            ["osmium", "export", str(filtered), "-f", "geojsonseq",
             "--geometry-types=polygon", "-u", "type_id"],
            stdout=subprocess.PIPE, text=True)
        for line in process.stdout:
            line = line.strip().lstrip("\x1e")
            if not line:
                continue
            # 先按字串挑一遍再解析：全国的行政边界有十几万条，逐条 json.loads 是几分钟
            if '"admin_level"' not in line:
                continue
            feature = json.loads(line)
            if str((feature.get("properties") or {}).get("admin_level")) in levels_set:
                sink.write(json.dumps(feature, ensure_ascii=False) + "\n")
        process.wait()
    return out


def osm_id_of(feature: dict) -> str:
    """osmium 的 area id → OSM 原始 id。

    `-u type_id` 给的是**面 id**（`a` + 数字），由原始对象推出来：关系是 `id×2+1`，
    闭合 way 是 `id×2`。**必须还原**——名册以 OSM id 为键认人，上海的键是关系 913067；
    直接拿面 id 当键，同一座城会被认成新城、派一个新号，用户手机上已有的归属全变孤儿
    （`publish.assign` 的注释讲的就是这件事）。

    way 与关系的 id 空间是分开的，所以 way 那一支加前缀区分。
    """
    raw = str(feature.get("id") or "")
    if not raw.startswith("a") or not raw[1:].isdigit():
        raise ValueError(f"认不出的 area id：{raw!r}——`osmium export` 少了 `-u type_id`？")
    number = int(raw[1:])
    return str((number - 1) // 2) if number % 2 else f"w{number // 2}"


def read_boundaries(path: Path):
    """`admin_boundaries` 的产物 → `(osm_id, tags, polygons)`，polygons 形如
    `[{"o": 外环, "i": [内环…]}]`，与 `boundary.Boundary.polygons` 同一个形状。"""
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        feature = json.loads(line)
        geometry = feature.get("geometry") or {}
        coordinates = geometry.get("coordinates")
        if not coordinates:
            continue
        rings = [coordinates] if geometry["type"] == "Polygon" else coordinates
        polygons = [{"o": [tuple(p) for p in ring[0]],
                     "i": [[tuple(p) for p in hole] for hole in ring[1:]]}
                    for ring in rings if ring]
        yield osm_id_of(feature), feature.get("properties") or {}, polygons


def city_centres(source: Path, out: Path) -> dict:
    """区域文件里的城市中心点：`place=city` / `place=town` 的节点。

    画框的**几何中心不是城市中心**：成都的建成区往南铺得远（天府新区），重庆的主城被
    江切成几块，外接框的中心都会偏出真正的市中心（2026-09-07 用户在图上指出——
    渝中半岛跑到了右下角）。OSM 里有现成的答案，逐城可查、全国都有
    （中国 `place=city` 3176 个、`place=town` 32426 个），不需要逐城手调。

    返回 `{名字: [(lon, lat, 等级), …]}`，等级 0 是 city、1 是 town：同名的镇很多，
    挑的时候先按等级、再按在不在这座城的界内。
    """
    if not out.exists():
        filtered = out.with_suffix(".pbf")
        if not filtered.exists():
            run("osmium", "tags-filter", source, "n/place=city", "n/place=town",
                "-o", filtered, "--overwrite")
        with out.open("w", encoding="utf-8") as sink:
            process = subprocess.Popen(
                ["osmium", "export", str(filtered), "-f", "geojsonseq", "--geometry-types=point"],
                stdout=subprocess.PIPE, text=True)
            for line in process.stdout:
                sink.write(line)
            process.wait()

    places: dict = {}
    for line in out.read_text(encoding="utf-8").splitlines():
        line = line.strip().lstrip("\x1e")
        if not line:
            continue
        feature = json.loads(line)
        tags = feature.get("properties") or {}
        name = tags.get("name:zh") or tags.get("name")
        coordinates = (feature.get("geometry") or {}).get("coordinates")
        if not name or not coordinates:
            continue
        rank = 0 if tags.get("place") == "city" else 1
        places.setdefault(name, []).append((coordinates[0], coordinates[1], rank))
    return places


def centre_for(name: str, boundary, places: dict):
    """这座城的市中心。先认同名的点（「成都市」也认「成都」），都不在界内就返回 None。"""
    candidates = places.get(name, []) + places.get(name.rstrip("市"), [])
    inside = [c for c in candidates if boundary.contains((c[0], c[1]))]
    if not inside:
        return None
    best = min(inside, key=lambda c: c[2])
    return best[0], best[1]
