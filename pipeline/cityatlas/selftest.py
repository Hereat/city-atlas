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

    checks += _incremental_shards()
    checks += _shard_tie_break()
    checks += _region_units_dont_swallow()
    checks += _directory_generations()
    checks += _overture_mapping()
    checks += _still_water_clip()
    checks += _sea_winding()
    checks += _english_names()

    print(f"selftest 通过，{checks} 条")


def _english_names() -> int:
    """英文名只从 `name:en` 来，缺就是缺，一路缺到目录条目里。

    最容易犯的是「取不到就拿 `name` 顶上」——那样日本那 1739 座的英文名会全是汉字
    （東京都），而产物看起来一切正常、字段也非空。管线这里必须如实答「OSM 没给」，
    退路归 App 那一头（`City.displayName` 退当地写法）。
    """
    from . import directory as directory_module
    from .boundary import Boundary, english_name

    _assert(english_name({"name": "東京都", "name:en": "Tokyo"}) == "Tokyo", "有 name:en 时没取到")
    _assert(english_name({"name": "東京都"}) is None, "没有 name:en 时拿当地写法顶替了英文名")
    _assert(english_name({"name": "巴黎", "name:en": "  "}) is None, "空白的 name:en 该当成没有")

    def entry(name_en):
        polygons = [{"o": [(120.0, 29.0), (120.2, 29.0), (120.2, 29.2), (120.0, 29.2)], "i": []}]
        boundary = Boundary(osm_relation=0, admin_level=0, name_zh="义乌市", name_local="义乌市",
                            name_en=name_en, polygons=polygons)
        city = {"cityID": "c00310", "name": "义乌市", "nameLocal": "义乌市", "nameEn": name_en,
                "mapDataVersion": 1, "center": [120.1, 29.1], "frame": [6000, 7500],
                "bounds": [0, 0, 0, 0]}
        return directory_module.entries(city, boundary)[(29, 120)]

    _assert(entry("Yiwu")["nameEn"] == "Yiwu", "目录条目里没带上英文名")
    _assert(entry(None)["nameEn"] is None, "没有英文名时目录条目该留空，不该退回中文名")
    return 5


def _shard_tie_break() -> int:
    """平局取最小的那座城。

    地级市把下辖的县级市整个包住，一个起点因此同时落在两座城里，而 App 取分片里
    第一个包含它的。先前按 `cityID`（发号顺序）排，义乌的散步全归了金华。
    """
    from . import directory as directory_module
    from .boundary import Boundary

    def rectangle(city_id, name, half):
        polygons = [{"o": [(120 - half, 29 - half), (120 + half, 29 - half),
                           (120 + half, 29 + half), (120 - half, 29 + half)], "i": []}]
        boundary = Boundary(osm_relation=0, admin_level=0, name_zh=name, name_local=name,
                            polygons=polygons)
        city = {"cityID": city_id, "name": name, "nameLocal": name, "nameEn": None,
                "mapDataVersion": 1,
                "center": [120.0, 29.0], "frame": [6000, 7500], "bounds": [0, 0, 0, 0]}
        return directory_module.entries(city, boundary)[(29, 120)]

    # 大的先发号（现实里就是这样：地级市在名单里排在它下辖的县级市前面）
    big = rectangle("c00187", "金华市", 0.4)
    small = rectangle("c00310", "义乌市", 0.1)
    shard = directory_module.shard((29, 120), [big, small], {"pipeline": "selftest"})
    _assert([city["name"] for city in shard["cities"]] == ["义乌市", "金华市"],
            "分片里大城排在了小城前面——起点落在两座城里时会归给大的那座")
    _assert(small["areaKm2"] < big["areaKm2"], "面积算反了")
    return 2


def _region_units_dont_swallow() -> int:
    """自治州与盟不吞下驻地那座城。

    「地盘上已经铺着别座城的主城就不独立」这条对地级市是对的（婺城区就是金华市区，
    该叫金华），对自治州是错的：州的主城恰恰就在州府，于是州府被自己的州吞掉——
    在西昌散步，城市卡写着「凉山彝族自治州」。
    """
    from . import frame as frame_module
    from .boundary import Boundary

    cover = (4, 5)
    # 州与州府共用同一片主城，探针就是那片建成区，整片落在州府的地盘里
    patch = [(102.0 + 0.001 * i, 27.0 + 0.001 * i) for i in range(8)]

    def entry(osm_id, name, level, half):
        box = (102 - half, 27 - half, 102 + half, 27 + half)
        polygons = [{"o": [(box[0], box[1]), (box[2], box[1]), (box[2], box[3]), (box[0], box[3])],
                     "i": []}]
        boundary = Boundary(osm_relation=osm_id, admin_level=level, name_zh=name,
                            name_local=name, polygons=polygons)
        return {"unit": {"name": name, "level": level, "osm_id": osm_id,
                         "bbox": box, "boundary": boundary},
                "probe": patch, "box": (102.0, 27.0, 102.007, 27.007), "area": half}

    def names(entries, region_suffix):
        kept = frame_module.drop_swallowed(entries, cover, region_suffix)
        return [item["unit"]["name"] for item in kept]

    # 大的先占（`drop_swallowed` 的入参按建成区面积从大到小给）
    prefecture = [entry(1, "凉山彝族自治州", 5, 0.5), entry(2, "西昌市", 6, 0.05)]
    _assert(names(prefecture, ("自治州", "盟")) == ["凉山彝族自治州", "西昌市"],
            "州府被自己的州吞掉了——在西昌散步会写成凉山")
    _assert(names(prefecture, ()) == ["凉山彝族自治州"], "不给区域名后缀时这条规则不该生效")

    city = [entry(3, "金华市", 5, 0.5), entry(4, "婺城区", 6, 0.05)]
    _assert(names(city, ("自治州", "盟")) == ["金华市"], "地级市不再吞下自己的市区")
    return 3


def _incremental_shards() -> int:
    """增量出包：跑第二个国家不许动第一个。

    这是最贵的一类静默错误——产物照常生成、体积正常，只是上一个国家的城
    从目录里消失了，而那要等用户打开 App 发现自己的城不见了才知道。
    """
    import tempfile
    from pathlib import Path

    from . import directory as directory_module, publish
    from .__main__ import _write_shards

    licenses = {"pipeline": "selftest"}

    def entry(city_id, name, area_km2=1.0):
        return {"cityID": city_id, "name": name, "nameLocal": name, "areaKm2": area_km2}

    def cities_in(out, cell, version=None):
        version = version or directory_module.DIRECTORY_VERSION
        path = out / "directory" / f"v{version}" / f"{directory_module.cell_name(cell)}.json.gz"
        if not path.exists():
            return []
        import gzip as _gzip, json as _json
        with _gzip.open(path, "rt", encoding="utf-8") as handle:
            return [city["cityID"] for city in _json.load(handle)["cities"]]

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        china, japan = (31, 121), (35, 139)
        shared = (43, 131)          # 中俄边境那种两国共用的格子

        # 第一国
        _write_shards(out, {china: [entry("c1", "上海")], shared: [entry("c2", "珲春")]},
                      mine={"c1", "c2"}, licenses=licenses)
        # 第二国：只写自己的格子，其中一个与第一国共用
        _write_shards(out, {japan: [entry("c3", "東京")], shared: [entry("c4", "稚内")]},
                      mine={"c3", "c4"}, licenses=licenses)
        _assert(cities_in(out, china) == ["c1"], "跑第二个国家把第一个国家的格子弄没了")
        _assert(cities_in(out, shared) == ["c2", "c4"], "共用格子没有把两国的城并在一起")
        _assert(cities_in(out, japan) == ["c3"], "第二个国家自己的格子没写出来")

        # 第一国重跑：c2 退役、c1 搬到别的格子
        moved = (30, 120)
        _write_shards(out, {moved: [entry("c1", "上海")]}, mine={"c1", "c2"}, licenses=licenses)
        _assert(cities_in(out, china) == [], "搬走之后旧格子该被删掉")
        _assert(cities_in(out, moved) == ["c1"], "搬到新格子没写出来")
        _assert(cities_in(out, shared) == ["c4"], "退役的城没有从共用格子里摘走，或者把别国的城误伤了")
        _assert(cities_in(out, japan) == ["c3"], "第一国重跑动了第二国的格子")

        # 升一版目录：新版本那个目录是空的，上一版里别国的城要跟过来。
        # 不跟过来就等于「凡是这一轮没跑的国家全部消失」，而 latest.json 已经指向新版。
        from . import __main__ as main_module
        bumped = directory_module.DIRECTORY_VERSION + 1
        main_module.DIRECTORY_VERSION = bumped
        try:
            _write_shards(out, {moved: [entry("c1", "上海")]}, mine={"c1", "c2"}, licenses=licenses)
        finally:
            main_module.DIRECTORY_VERSION = directory_module.DIRECTORY_VERSION
        _assert(cities_in(out, japan, bumped) == ["c3"], "升版本把上一版里别国的城弄丢了")
        _assert(cities_in(out, moved, bumped) == ["c1"], "升版本后这一轮的城没写进新目录")

    # 名册：号不变，但 country 每轮刷新——退役判定靠它
    registry = {"nextSerial": 1, "cities": {}}
    first = publish.assign(registry, 100, {"name": "上海", "country": "China"})
    again = publish.assign(registry, 100, {"name": "上海市", "country": "China"})
    _assert(first == again == "c00001", "同一个 OSM id 派了两个号")
    _assert(registry["cities"]["100"]["name"] == "上海", "名册的既有字段被改写了")
    _assert(registry["cities"]["100"]["country"] == "China", "country 没有被刷新")
    return 10


def _directory_generations() -> int:
    """目录分片只留最近两代，而**任何一版地图包都不删**。

    两件事的删除规则不是一回事，混了就是一次线上事故：没升级的 App 一直请求
    `1.json.gz`（换血文档第八节那张表），把旧包删掉，那批人的城市图当场空掉。
    """
    import tempfile
    from pathlib import Path

    from . import publish

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        for version in (1, 2, 3):
            shard = out / "directory" / f"v{version}" / "31_121.json.gz"
            shard.parent.mkdir(parents=True)
            shard.write_bytes(b"")
        for version in (1, 2):
            package = out / "package" / "c00001" / f"{version}.json.gz"
            package.parent.mkdir(parents=True, exist_ok=True)
            package.write_bytes(b"")

        _assert(publish.prune_directories(out, 3) == [1], "该删的是 v1 那一代")
        _assert(not (out / "directory" / "v1").exists(), "v1 那一代报了删掉、盘上还在")
        _assert((out / "directory" / "v2").is_dir() and (out / "directory" / "v3").is_dir(),
                "最近两代被删掉了——升版本那一轮要从上一代读别国的城")
        _assert(sorted(path.name for path in (out / "package" / "c00001").iterdir())
                == ["1.json.gz", "2.json.gz"],
                "旧版地图包被删了——那是没升级的 App 唯一取得到的包")
    return 4


def _overture_mapping() -> int:
    """映射表：几何类型进判据，沟渠不当运河，轨道走白名单。

    三条都不是抽象的正确性，是画错图的三种具体长相。
    """
    from . import overture

    # **几何类型必须进判据**：`mappack` 的 waterway 分支在面状分支之前 continue，
    # 任何带 `waterway=` 的东西都走「线」那一支。只按 subtype 翻，黄浦江、隅田川那样的
    # 河面会被画成两条 8 米宽的描边，河面本身消失。Overture 的 subtype=river
    # 面比线还多（全日本 44236 对 235092），所以这是常态不是边角。
    _assert(overture.tags_for("river", "river", area=False) == {"waterway": "river"},
            "河的线没翻成 waterway")
    _assert(overture.tags_for("river", "river", area=True) == {"natural": "water", "water": "river"},
            "河面没翻成水面——会被当成 8 米宽的水线画出去")

    # **沟渠与排水沟不是运河**：Overture 把 ditch（全日本 186933）与 drain（74424）
    # 放在 subtype=canal 底下。按 subtype 翻会凭空多出二十六万条六米宽的水线，
    # 而 OSM 那条路上 `mappack.WATER_WIDTHS` 里没有它们、从来不画。
    _assert(overture.tags_for("canal", "canal", area=False) == {"waterway": "canal"},
            "运河的线没翻出来")
    for cls in ("ditch", "drain"):
        _assert(overture.tags_for("canal", cls, area=False) is None, f"{cls} 被当成了运河")

    # **轨道是白名单**：判据是「这条轨道今天有车在跑」。废线、停用线、在建线在
    # Overture 里统一标成 class='unknown'，白名单自然把它们挡在外面；写成
    # 「不是 unknown 就收」会在 Overture 哪天把站台补进 segment 时把站场糊成一块。
    _assert(overture.tags_for("rail", "standard_gauge", area=False) == {"railway": "rail"},
            "在跑的轨道没收")
    _assert(overture.tags_for("rail", "unknown", area=False) is None, "废线与在建线被收了")
    _assert(overture.tags_for("rail", "platform", area=False) is None, "站台被当成轨道收了")

    # 道路是恒等映射，但只认 `ROAD_TIERS` 里有的
    _assert(overture.tags_for("road", "residential", area=False) == {"highway": "residential"},
            "道路没恒等翻过去")
    _assert(overture.tags_for("road", "bridleway", area=False) is None, "马道不在四档里，不该收")

    # 绿地两层共用一张表，同名的类在两层里说的是同一件事
    _assert(overture.tags_for("forest", "wood", area=True) == {"natural": "wood"}, "林地没翻出来")
    _assert(overture.tags_for("park", "park", area=True) == {"leisure": "park"}, "公园没翻出来")
    _assert(overture.tags_for("recreation", "pitch", area=True) is None,
            "球场不在 GREEN_AREA_TAGS 里，收它是内容变更")

    # **水面的 subtype 要带得到 `mappack` 那边。** 湖、塘、水库翻出来的写法必须落进
    # `STILL_WATER_TAGS`（水线按它们裁），而河面、运河面必须落不进去——宽河中间
    # 那道线是现在的画法，被自己的河面裁掉就是内容变更。
    still = set(mappack.STILL_WATER_TAGS)
    for subtype in ("lake", "pond", "reservoir"):
        _assert(set(overture.tags_for(subtype, subtype, area=True).items()) & still,
                f"{subtype} 翻出来的写法 mappack 认不出是静水面，湖面上的水线不会被裁")
    for subtype in ("river", "canal"):
        _assert(not set(overture.tags_for(subtype, subtype, area=True).items()) & still,
                f"{subtype} 面被当成了静水面，河的中心线会被自己的河面裁掉")

    # **映射表翻出来的每一份 tags 都得画得出来**：至少有一对落进 `WATER_AREA_TAGS`
    # 或 `GREEN_AREA_TAGS`。两边对不上就是白画一场——管线认认真真把一类要素翻成了
    # `landuse=xxx`，`mappack` 那边没有这一条，于是它在图上根本不出现，而且一声不吭。
    # 判的是「这一份 tags」而不是「每一个值」：`water=<subtype>` 本来就不负责画，
    # 它只回答上面那个「是不是静水面」，逐值判会把它算成漏画。
    drawn = set(mappack.WATER_AREA_TAGS) | set(mappack.GREEN_AREA_TAGS)
    for tags in (*overture.WATER_AREA_SUBTYPES.values(), *overture.GREEN_CLASSES.values()):
        _assert(set(tags.items()) & drawn, f"{tags} 翻出来了，可 mappack 一张表都不认，图上不会出现")
    return 18


def _still_water_clip() -> int:
    """湖面上的水线按面裁：进湖的那一段没了，岸上那一段一米不动。

    这是大津那张图的最小复现——琵琶湖面上一把放射状细线（95 条、107.6 km）。先前的
    判据是「两端都落在水面里」，而真实的失败形状是**一端在岸上、一端伸进湖里**，
    0 比 95 一条都没挡住；改成「有一端在水里就整条删」又会把河口那段岸上的河道
    一起删掉。所以这三条各盯一件事：进湖的裁掉、岸上的留着、河面不参与裁。
    """
    lake = [(0.000, -0.002), (0.004, -0.002), (0.004, 0.002), (0.000, 0.002)]

    def way(tags, points, *, closed=False):
        ring = points + points[:1] if closed else points
        return {"type": "way", "tags": tags,
                "geometry": [{"lon": lon, "lat": lat} for lon, lat in ring]}

    def water_lines(area_tags, line) -> list[list]:
        snapshot = {"elements": [way(area_tags, lake, closed=True), way({"waterway": "river"}, line)]}
        package = mappack.build(snapshot, name="湖城", name_local="湖城",
                                center=(0.0, 0.0), frame=(4000, 4000))
        _assert(len(package["water"]) == 1, "水面自己都没画出来，这条自检不成立")
        return [codec.decode(line["p"]) for line in package["waterLines"]]

    # 从岸上（西边 445 米处）一直画到湖心。湖的西岸在 x=0，裁完只该剩下 x<0 那半。
    into_lake = [(-0.004, 0.0), (0.002, 0.0)]
    pieces = water_lines({"natural": "water", "water": "lake"}, into_lake)
    _assert(len(pieces) == 1, f"按面裁完应当只剩岸上那一段，得到 {len(pieces)} 段")
    xs = [x for x, _ in pieces[0]]
    _assert(max(xs) <= 1.0, f"进湖的那一段没裁掉：线还画到 x={max(xs):.0f} m")
    _assert(min(xs) <= -440.0, f"岸上那一段被裁短了：只剩到 x={min(xs):.0f} m")

    # 河面、运河面不参与：宽河中间那道线是现在的画法，不能被自己的河面裁掉
    pieces = water_lines({"natural": "water", "water": "river"}, into_lake)
    _assert(len(pieces) == 1 and max(x for x, _ in pieces[0]) > 200.0,
            "河面把河的中心线裁掉了——river / canal 的面不参与裁线")

    # 整条泡在湖里的（老判据唯一挡得住的那类）照旧一条都不画
    _assert(water_lines({"natural": "water", "water": "lake"}, [(0.001, 0.0), (0.003, 0.0)]) == [],
            "整条在湖里的水线还留着")

    # **裁线要用画出来的那份面。** 同一个湖在 OSM 里常画两遍，一遍画了湖心岛一遍没画
    # （`_keep_area` 的注释记着广州、上海各有几处）。包里画的是带岛那份，如果裁线拿
    # 没岛那份去裁，岛上那截河道会跟着湖面一起消失——岛画着，岛上的河没了。
    island = [(0.0015, -0.0005), (0.0025, -0.0005), (0.0025, 0.0005), (0.0015, 0.0005)]
    lake_tags = {"natural": "water", "water": "lake"}
    twice = {"elements": [
        way(lake_tags, lake, closed=True),                      # 没画岛的那份
        {"type": "relation", "tags": lake_tags, "members": [     # 画了岛的那份
            {"role": "outer", **way(lake_tags, lake, closed=True)},
            {"role": "inner", **way(lake_tags, island, closed=True)}]},
        way({"waterway": "river"}, [(0.0018, 0.0), (0.0022, 0.0)]),   # 岛上那截河道
    ]}
    package = mappack.build(twice, name="湖城", name_local="湖城",
                            center=(0.0, 0.0), frame=(4000, 4000))
    _assert(len(package["water"]) == 1 and package["water"][0]["i"],
            "同一个湖的两份没有去重成带岛那一份，这条自检不成立")
    _assert(len(package["waterLines"]) == 1, "岛上那截河道被湖面裁掉了——裁线用的不是画出来的那份面")
    return 7


def _sea_winding() -> int:
    """海面的绕向：外环顺时针、岛逆时针，而且裁到画框之后仍然是。

    摆错的后果是海陆对调，或者河口那种海面与内陆水面重叠的地方透出纸色——
    渲染器分两次填就是为了这个（`CityMapRenderer`）。
    """
    from . import overture

    def ring(points):
        return [{"lon": x, "lat": y} for x, y in points]

    # Overture 给的是 OGC 的约定：外环逆时针、洞顺时针，正好与我们要的相反。
    # 这里给的正是它那个方向，摆正之后必须翻过来。
    outer_ccw = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
    hole_cw = [(0.2, 0.2), (0.2, 0.4), (0.4, 0.4), (0.4, 0.2)]
    rings = overture._sea_rings({"natural": "coastline"}, [ring(outer_ccw), ring(hole_cw)])
    _assert(overture._signed_area(rings[0]["geometry"]) < 0, "海面外环没摆成顺时针——海陆会对调")
    _assert(overture._signed_area(rings[1]["geometry"]) > 0, "岛没摆成逆时针——岛会被填成海")

    # 已经是对的方向就不该被再翻一次
    again = overture._sea_rings({"natural": "coastline"},
                               [r["geometry"] for r in rings])
    _assert(overture._signed_area(again[0]["geometry"]) < 0, "摆正不是幂等的")

    # **裁剪保向**：`_encode_polygon` 走的是 Sutherland–Hodgman，按原顺序走一遍四条边。
    # 这一条是上面那两条的前提——绕向在管线里摆正，到 App 手上必须还是同一个方向。
    box = (-100.0, -100.0, 100.0, 100.0)
    clockwise = [(-300.0, -300.0), (-300.0, 300.0), (300.0, 300.0), (300.0, -300.0)]
    clipped = clip_ring(clockwise, box)
    _assert(_ring_area_sign(clipped) < 0, "顺时针的环裁完变成了逆时针")
    _assert(_ring_area_sign(clip_ring([(x, -y) for x, y in clockwise], box)) > 0,
            "逆时针的环裁完变成了顺时针")
    return 5


def _ring_area_sign(ring) -> float:
    total = 0.0
    for index in range(len(ring)):
        (x0, y0), (x1, y1) = ring[index], ring[(index + 1) % len(ring)]
        total += x0 * y1 - x1 * y0
    return total
