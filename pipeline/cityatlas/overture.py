"""要素层：Overture 的 parquet + DuckDB，取代 `pbf` 的 `filter_tags` / `extract` / `to_elements`。

**为什么不是逐城切 pbf**：osmium 的 `extract` 要把每座城的框各切一份小文件，1739 座
要两小时，而且 `-s smart` 缓存跨框的关系成员，一批超过四十个框就把内存打爆（旧
`pbf.BATCH` 的注释记着这件事）。换成「一趟按国家包围盒拉、落地成带统计信息的列式
文件、用 SQL 切」之后，同一段从两小时降到十分钟出头。

**为什么是 Overture 而不是第二个数据源**：它就是 OSM 的再打包（每条记录的 `sources`
里写着 `provider=osm`、`license=ODbL-1.0`），所以标签能一一翻回来，行政边界与城市
名单继续走 pbf 那条路，两边说的是同一个 OSM。代价是数据落后约一个月（release
`2026-08-19.0` 用的是 planet `2026-07-23`），换来的是**可复现**：release 在对象存储上
不可变，钉版本号比钉 Overpass 快照的 sha256 更稳。

三步与 `pbf` 的要素三步同形——路径进、路径出，缓存靠文件里外显的口径判：

1. `fetch` 一趟拉四层到 `<国>/overture/`，落一份 `fetch.json` 记 release 与包围盒。
2. `cut` 一趟按画框切，落**一份**按城排序的 parquet。
3. `to_elements` 顺序读那一份，一座城一座城地流出 `{"elements": [...]}`。

形状与 `pbf.to_elements` 逐字段相同，所以 `mappack` 认得，下游一步不用改。
"""

from __future__ import annotations

import json
import os
import struct
from pathlib import Path

from .mappack import ROAD_TIERS

RELEASE = "2026-08-19.0"
BASE = f"s3://overturemaps-us-west-2/release/{RELEASE}"

# 四层。名字就是落盘的文件名，值是 Overture 的路径与「这一层只要哪些行」。
#
# **过滤条件写在这里而不是写在 `cut` 里**：`fetch` 的产物要对得起「这一层的全部」这句话，
# 改映射表不该触发一次两点七个 G 的重下。这里只挡整类不要的（渡轮航线是
# `transportation` 里唯一一个既不是路也不是轨的 subtype），映射表的筛选在 `cut` 里做。
LAYERS = {
    "segment": ("theme=transportation/type=segment", "subtype IN ('road', 'rail')"),
    "water": ("theme=base/type=water", None),
    "land_use": ("theme=base/type=land_use", None),
    "land": ("theme=base/type=land", None),
}

# ---------- 映射表：Overture 的词表 → OSM 的写法 ----------
#
# 方向是把 Overture 翻回 OSM，不是改 `mappack` 去认 Overture——`mappack` 的四张表
# （`ROAD_TIERS`、`RAILWAY_KINDS`、`WATER_WIDTHS`、`WATER_AREA_TAGS`、`GREEN_AREA_TAGS`）
# 是「我们画哪些要素」这件事的唯一出处，映射表负责对齐到它们，一个字不改。
#
# **判据必须带几何类型。** `mappack` 的 `waterway` 分支在面状分支之前 `continue`，
# 任何带 `waterway=` 的东西都走「线」那一支；而 Overture 的 `subtype=river` 面比线还多
# （全日本 44236 对 235092，上海样本 5194 对 4047）。只按 subtype 翻，黄浦江、隅田川
# 这些河面会被画成 8 米宽的两条描边，河面本身消失。

# 道路是恒等映射：Overture 的 class 用的就是 OSM 的 highway 值。`*_link` 在 `subclass`
# 里，四对与主档同级（`ROAD_TIERS` 里 `motorway_link` 与 `motorway` 都是一档），不必取。
# 不在 `ROAD_TIERS` 里的照旧不画：`bridleway`（全日本 374 条）与 `unknown`（5721 条，
# 占 0.035%）。
def _road_tags(cls: str) -> dict | None:
    return {"highway": cls} if cls in ROAD_TIERS else None


# 今天有车在跑的那几种轨道。**白名单而不是黑名单**，与 `mappack.RAILWAY_KINDS` 同一个
# 形状、同一条判据。Overture 把废线、停用线、在建线单独标成 `class='unknown'`
# （关东 925 条里 disused 577、abandoned 468、under_construction 70），白名单自然把
# 它们挡在外面；站台与站房是面和点、在 `base/infrastructure` 里，我们不查那一层。
#
# 不写成「`class != 'unknown'` 就收」：那样 Overture 哪天把站台补进 segment，
# 站场那一片会悄悄糊成一块。
RAIL_CLASSES = {
    "standard_gauge": "rail", "narrow_gauge": "rail", "broad_gauge": "rail",
    "subway": "subway", "tram": "tram", "light_rail": "light_rail",
    "monorail": "monorail", "funicular": "funicular",
}

# 线状水系。**判据是 class 不是 subtype**：Overture 把沟渠（全日本 ditch 186933）与
# 排水沟（drain 74424）也放在 `subtype=canal` 底下，按 subtype 翻会凭空多出二十六万条
# 六米宽的水线，而 OSM 那条路上它们是 `waterway=ditch` / `waterway=drain`，
# `mappack.WATER_WIDTHS` 里没有、从来不画。
WATER_LINE_CLASSES = {"river": "river", "canal": "canal", "stream": "stream"}

# 面状水系，按 subtype。这一层 class 的粒度比我们细（pond 底下分 pond / fishpond），
# 而 `mappack.WATER_AREA_TAGS` 只认「是不是水」，所以 subtype 是对的颗粒度。
#
# **subtype 要跟着翻出去**，因为下游还要问第二个问题：这块水面是不是静的？
# 水线落在湖、塘、水库里的那一段该裁掉，落在河面、运河面里的不裁
# （`mappack.STILL_WATER_TAGS`）。带出去的写法用 OSM 自己的 `water=<值>` 而不是给
# Overture 单开一个字段，这样「哪些算静水」只有 `STILL_WATER_TAGS` 一处说了算。
#
# **但这不是「两条路说同一句话」，是管线替上游补一句 OSM 没说的话**：实测大津画框内
# 1389 块 `natural=water` 里只有 85% 带着 `water=*`，另外 211 块什么都没标。所以
# Overpass 那条路（样例城的对照）会有一批水面认不出是不是静的、不参与裁线，而这条路
# 每一块都认得出。补得对不对取决于 Overture 的 subtype 比 OSM 的 `water=*` 更全——
# 这正是换血的收益之一，但两条路的包会因此有一处系统性差异，做对照的人要知道。
#
# 还有一个缺口在下面 `"water"` 那一行：Overture 归在「就是一片水」的面（中国 36.7%、
# 日本 18.0%）没有 subtype 可翻，认不出是不是静的，照旧不裁。实测广州还剩 12.9 km、
# 南通 21.3 km 的水线画在这类面上（同口径下静水面上是 0.0 km）。
#
# `reservoir` 底下的 basin 与 water_storage 一并归 `landuse=reservoir`：
# `WATER_AREA_TAGS` 里 `landuse=reservoir` 与 `landuse=basin` 画出来一模一样，
# 分两个值只是多一条没有区别的分支。
#
# 挡在外面的四个 subtype，理由各不相同，所以逐个记：
# * `human_made`（全日本 23622）里 22871 个是游泳池。OSM 那条路上它是
#   `leisure=swimming_pool`，不在 `WATER_AREA_TAGS` 里、不画。方案文档按 50 城样本
#   写的是「坞与蓄水池，收」，那份样本里没有城市住宅区，看不见游泳池占了 97%。
# * `physical`（bay / strait / cape / sea）是有名字的水域范围，不是水面本身。
# * `spring`（泉眼）、`wastewater`（污水池）在 OSM 那条路上同样不画。
WATER_AREA_SUBTYPES = {
    "river": {"natural": "water", "water": "river"},
    "canal": {"natural": "water", "water": "canal"},
    "stream": {"natural": "water", "water": "stream"},
    "lake": {"natural": "water", "water": "lake"},
    "pond": {"natural": "water", "water": "pond"},
    "water": {"natural": "water"},          # 「就是一片水」，Overture 没再往下分
    "reservoir": {"landuse": "reservoir", "water": "reservoir"},
}

# 海面。`subtype=ocean` 是**面**，不是岸线——它在 `cut` 里就被裁到画框、
# 在 `to_elements` 里摆正绕向，走 `mappack` 的 coastline 那一列。
OCEAN_SUBTYPE = "ocean"
COASTLINE_TAGS = {"natural": "coastline"}

# 绿地。`base/land` 与 `base/land_use` 共用这一张：两层的 class 取值不重叠，
# 同名的（grass、meadow）在两层里说的是同一件事。对齐的是
# `mappack.GREEN_AREA_TAGS` 那十四条，一个不多一个不少。
#
# 明确不收的两个大类：`pitch`（球场，全日本 80516）不在 `GREEN_AREA_TAGS` 里，
# 收不收是内容变更、不在换血范围内；`national_park`（58 个）面积极大，
# OSM 那条路上它是 `boundary=national_park`，也不画。
GREEN_CLASSES = {
    "park": {"leisure": "park"},
    "garden": {"leisure": "garden"},
    "nature_reserve": {"leisure": "nature_reserve"},
    "village_green": {"landuse": "village_green"},
    "allotments": {"landuse": "allotments"},
    "cemetery": {"landuse": "cemetery"},
    "grave_yard": {"landuse": "cemetery"},
    "recreation_ground": {"landuse": "recreation_ground"},
    "grass": {"landuse": "grass"},
    "meadow": {"landuse": "meadow"},
    "forest": {"landuse": "forest"},
    "wood": {"natural": "wood"},
    "grassland": {"natural": "grassland"},
    "scrub": {"natural": "scrub"},
    "heath": {"natural": "heath"},
}


def tags_for(subtype: str, cls: str, *, area: bool) -> dict | None:
    """一行 Overture → 一份 OSM tags，翻不出来就是 None（这一行不画）。

    `area` 是几何类型，不是可有可无的第三个参数：同一个 `(subtype, class)` 在线和面
    上是两件事（见上面 `subtype=river` 那一段）。
    """
    if subtype == "road":
        return _road_tags(cls) if not area else None
    if subtype == "rail":
        kind = RAIL_CLASSES.get(cls)
        return {"railway": kind} if kind and not area else None
    if subtype == OCEAN_SUBTYPE:
        return COASTLINE_TAGS if area else None
    if not area:
        way = WATER_LINE_CLASSES.get(cls)
        return {"waterway": way} if way else None
    tags = WATER_AREA_SUBTYPES.get(subtype) or GREEN_CLASSES.get(cls)
    return dict(tags) if tags else None


# `cut` 用来预筛的那两串。它们是从上面几张表推出来的，不另手写一份——
# 手写的那份会在改映射表时忘记跟着改，然后整类要素在图上静默消失。
def _kept_line_classes() -> set[str]:
    return set(ROAD_TIERS) | set(RAIL_CLASSES) | set(WATER_LINE_CLASSES)


def _kept_area_keys() -> tuple[set[str], set[str]]:
    return set(WATER_AREA_SUBTYPES) | {OCEAN_SUBTYPE}, set(GREEN_CLASSES)


def _sql_list(values) -> str:
    return ", ".join("'" + v.replace("'", "''") + "'" for v in sorted(values))


# ---------- 抓取 ----------

def connect(memory_limit: str | None = None, temp_dir: Path | None = None):
    """一个装好 `httpfs` 与 `spatial` 的 DuckDB 连接。

    `memory_limit` 与 `temp_directory` 都不是调参：切城是一次几千万行的连接再排序，
    不给上限它会一路吃到系统内存耗尽（19 GB 实测），给了就溢到磁盘上慢一点跑完。

    **`CITYATLAS_MEMORY` 可以把它压低。** 这台机器上常同时开着几十个 session（Xcode
    构建、别的管线），12 GB 撞上去会让整轮**被系统按内存杀掉**——不是报错，是进程没了
    （2026-09-09 中国那轮实测，抓到一半没的）。机器忙的时候压到 6GB，DuckDB 会多溢一些
    到磁盘，慢一点但跑得完。

    行序那个开关**不在这里定**，`cut` 与 `to_elements` 各自声明：一个要关掉才排得动，
    另一个要开着才读得对，写在这儿等于让两边共用一个错误的默认。
    """
    import duckdb

    con = duckdb.connect(config={"memory_limit": memory_limit
                                 or os.environ.get("CITYATLAS_MEMORY", "12GB")})
    con.sql("INSTALL httpfs; LOAD httpfs; INSTALL spatial; LOAD spatial;")
    con.sql("SET s3_region='us-west-2';")
    # 一层要下二十多分钟、上千个 part 文件，中间任何一次闪断都会让整层白下重来
    # （2026-09-09 实测：中国那轮下到 1634 MB 时报 `SSL connect error`，一层作废）。
    # 默认只重试 3 次、不等待，对这个长度的下载不够。
    con.sql("SET http_retries=10; SET http_retry_wait_ms=500; SET http_retry_backoff=2;")
    con.sql("CREATE SECRET (TYPE s3, PROVIDER config, KEY_ID '', SECRET '', REGION 'us-west-2');")
    if temp_dir:
        temp_dir.mkdir(parents=True, exist_ok=True)
        con.sql(f"SET temp_directory='{temp_dir}';")
    return con


def fetch(bbox, out_dir: Path, *, release: str = RELEASE, con=None) -> Path:
    """一趟把四层拉到 `out_dir`，落一份 `fetch.json` 记口径。

    `bbox` 取**所有画框的并集**而不是国土外接框：日本两者差不多，中国差一半，
    传输量按面积走（下面那条注释解释了为什么）。

    **扫描包围盒是主要成本，与城市数量基本无关**：全日本 500 平方度四层十分钟，
    50 座城 35 平方度四层五分半——面积差 14 倍、耗时差 1.8 倍。所以取法是「一趟按
    国家拉、落地之后本地切」，而不是把画框推进远程查询逐城取：后者会把同一片几何
    复制进每座沿海城市，50 城那次 `base/land` 只有 16525 条却占了 371 MB。

    **缓存判据是「覆盖」不是「相等」。** 已经落盘的那份只要 release 相同、包围盒
    包得住这次要的，它就是对的数据；画框往里缩不该触发一次几个 G 的重下，往外长
    必须触发。只记文件名或只比 hash 都答不出这个问题——S7 那批
    `admin-<层级>`、`places-<种类>` 的改名防的是同一件事的另一半。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp_path = out_dir / "fetch.json"
    if _covered(stamp_path, bbox, release):
        return out_dir

    con = con or connect()
    x0, y0, x1, y1 = bbox
    where = f"bbox.xmin < {x1} AND bbox.xmax > {x0} AND bbox.ymin < {y1} AND bbox.ymax > {y0}"
    counts = {}
    for name, (path, extra) in LAYERS.items():
        target = out_dir / f"{name}.parquet"
        clause = f"{where} AND ({extra})" if extra else where
        con.sql(f"""COPY (SELECT subtype, class, geometry FROM read_parquet('{BASE}/{path}/*')
                    WHERE {clause})
                 TO '{target}' (FORMAT parquet, COMPRESSION zstd)""")
        counts[name] = con.sql(f"SELECT count(*) FROM read_parquet('{target}')").fetchone()[0]
        print(f"      {name:<10} {counts[name]:>10} 条  {target.stat().st_size / 1e6:6.0f} MB", flush=True)

    stamp_path.write_text(json.dumps(
        {"release": release, "bbox": list(bbox), "layers": _layer_filters(), "counts": counts},
        ensure_ascii=False, indent=2),
        encoding="utf-8")
    report_coverage(con, out_dir)
    return out_dir




def report_coverage(con, out_dir: Path) -> None:
    """每一层里映射表翻不出来的那些类，逐个报条数与占比。**只报不拦**，拦在 `cut`。

    报出来是为了让「Overture 换了词表」看得见：那种事的失败长相是图上少掉一整类要素而
    什么都不报——`park` 哪天改叫 `public_park`，这一行会从「没画的」里冒出来。
    `selftest` 守不住这个，它只能守逻辑（几何类型进不进判据、沟渠有没有被当成运河）。

    这里的分母是**整个抓取矩形**，那里面装着一整圈邻国（见 `UNKNOWN_ROAD_LIMIT`），
    所以这几个数只能横向比、不能当判据：同一层的占比忽然翻倍才是信号。

    只按 `(subtype, class)` 判、不解几何：一个类只要在线和面上都翻不出来，它就是真没收。
    这样这一步是纯列扫描，几秒钟的事。
    """
    for name in LAYERS:
        rows = con.sql(f"SELECT subtype, class, count(*) FROM "
                       f"read_parquet('{out_dir}/{name}.parquet') GROUP BY 1, 2").fetchall()
        total = sum(count for _, _, count in rows) or 1
        missing = sorted(((subtype, cls, count) for subtype, cls, count in rows
                          if tags_for(subtype, cls, area=False) is None
                          and tags_for(subtype, cls, area=True) is None),
                         key=lambda row: -row[2])
        if missing:
            head = "、".join(f"{subtype}/{cls} {count}" for subtype, cls, count in missing[:4])
            share = sum(count for _, _, count in missing) / total
            print(f"      {name:<10} 不画的 {share:5.1%}：{head}"
                  f"{' 等' if len(missing) > 4 else ''}", flush=True)

    roads = con.sql(f"SELECT class, count(*) FROM read_parquet('{out_dir}/segment.parquet') "
                    f"WHERE subtype = 'road' GROUP BY 1").fetchall()
    total = sum(count for _, count in roads) or 1
    unknown = sum(count for cls, count in roads if cls == "unknown") / total
    print(f"      整个矩形里道路 Overture 没认出来的 {unknown:.2%}（含一圈邻国，判据在 cut 那一步）",
          flush=True)


def _layer_filters() -> dict:
    """落盘那四份 parquet 是按什么条件拉的。

    **它必须进缓存判据。** `LAYERS` 的过滤条件改了而文件名没变，缓存照旧命中，
    图上少一整层而什么都不报——日本那份 `segment.parquet` 正是按 `subtype='road'`
    拉的，轨道整层不在里面，而 `LAYERS` 早已改成 `IN ('road','rail')`
    （2026-09-08 的「还没做的」第一条）。缓存该答的是「盘上这份包不包得住这次要的」，
    包围盒是一半，取哪几类是另一半。
    """
    return {name: extra for name, (_, extra) in LAYERS.items()}


def _covered(stamp_path: Path, bbox, release: str) -> bool:
    if not stamp_path.exists():
        return False
    # **口令戳在不等于数据在。** 那份 json 是我们自己写的一行字，而四层 parquet 是几个 G
    # 的文件——手工放进来的、清过盘的、上一轮写到一半被掐的，都会留下「戳在文件不在」。
    # 只信戳的话，下一步 `cut` 会炸在一句 DuckDB 的 "No files found"，离现场很远。
    if any(not (stamp_path.parent / f"{name}.parquet").exists() for name in LAYERS):
        return False
    stamp = json.loads(stamp_path.read_text(encoding="utf-8"))
    if stamp.get("release") != release or stamp.get("layers") != _layer_filters():
        return False
    had = stamp.get("bbox") or [0, 0, 0, 0]
    return (had[0] <= bbox[0] and had[1] <= bbox[1]
            and had[2] >= bbox[2] and had[3] >= bbox[3])


def union_bbox(boxes) -> tuple[float, float, float, float]:
    """所有画框的并集。`boxes` 是 `{slug: (minlon, minlat, maxlon, maxlat)}`。"""
    values = list(boxes.values())
    return (min(b[0] for b in values), min(b[1] for b in values),
            max(b[2] for b in values), max(b[3] for b in values))


# ---------- 按画框切 ----------

def cut(overture_dir: Path, boxes: dict, out: Path, *, con=None, gate: bool = True) -> Path:
    """一趟切出所有城的要素，落**一份**按 slug 排好序的 parquet。

    **不是一城一份文件。** 按城分目录写过一版，1739 个分区把内存打爆（19 GB），
    磁盘 11 GB——每个小文件的行组太小、压不动，而源文件才 2.7 GB。一份大的按城排序，
    落盘 1.0 GB、顺序读，`to_elements` 一趟流完。

    切之前先按映射表筛类，两个理由：翻不出来的行搬过去也是丢，而 `base/land` 里
    `class='land'` 那 78736 块陆地面正是「搬过去也是丢、搬的时候先把内存吃光」的那种。

    **海面在这一步就裁到画框**（`ST_Intersection`），不是只按包围盒筛。海面是 1° 见方的
    瓦片、单块最多带三千五百个洞，一块能压到几百座沿海城市的框上——按包围盒筛等于把
    同一块巨型几何复制几百份，正是远程逐城查那条路被否掉的原因，本地做一样会炸。
    裁完 1739 个框只要 1.5 秒。

    切之前过一道判据：画框内的道路里 Overture 没认出来的占比（`_gate_unknown_roads`）。
    它要 `frames` 表，所以在这一步而不在 `fetch`。`gate=False` 只报不拦——那 2% 是按
    一整国的画框聚合出来的（中国 1483 座是 0.23%），`--only` 跑一座小城时分母只有几百条
    路，八条 Overture 没认出来的就 2.7%，拦下来报的却是「多半是 Overture 换了词表」。
    判据要拦的是整体性事件，半份名单不承载这个信号。
    """
    con = con or connect(temp_dir=out.parent / "duckdb-spill")
    # 几千万行的连接再排序，不许 DuckDB 为了保持行序攒着不放，否则内存兜不住
    con.sql("SET preserve_insertion_order=false")
    out.parent.mkdir(parents=True, exist_ok=True)
    con.sql("DROP TABLE IF EXISTS frames")
    con.sql("CREATE TABLE frames(city VARCHAR, xmin DOUBLE, ymin DOUBLE, xmax DOUBLE, ymax DOUBLE)")
    con.executemany("INSERT INTO frames VALUES (?, ?, ?, ?, ?)",
                    [(slug, *box) for slug, box in boxes.items()])

    _gate_unknown_roads(con, overture_dir, gate=gate)

    lines = _sql_list(_kept_line_classes())
    water_subtypes, green_classes = _kept_area_keys()
    parts = [
        _overlap("segment", overture_dir, f"class IN ({lines})"),
        _overlap("water", overture_dir, f"subtype IN ({_sql_list(water_subtypes - {OCEAN_SUBTYPE})})"),
        _overlap("land_use", overture_dir, f"class IN ({_sql_list(green_classes)})"),
        _overlap("land", overture_dir, f"class IN ({_sql_list(green_classes)})"),
        # 海面：裁到画框，裁空的丢掉
        f"""SELECT f.city, s.subtype, s.class,
                   ST_AsWKB(ST_Intersection(s.geometry,
                       ST_MakeEnvelope(f.xmin, f.ymin, f.xmax, f.ymax))) AS wkb
            FROM {_boxed('water', overture_dir, f"subtype = '{OCEAN_SUBTYPE}'")} s
            JOIN frames f ON s.x0 < f.xmax AND s.x1 > f.xmin AND s.y0 < f.ymax AND s.y1 > f.ymin
            WHERE NOT ST_IsEmpty(ST_Intersection(s.geometry,
                       ST_MakeEnvelope(f.xmin, f.ymin, f.xmax, f.ymax)))""",
    ]
    con.sql(f"COPY ({' UNION ALL '.join(parts)} ORDER BY city) TO '{out}' "
            f"(FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 100000)")
    return out

# 道路里 `class='unknown'` 占多少算太多。这一条**只管道路**：画框里的每一条路都是我们
# 可能画的，`unknown` 意味着「Overture 自己也没认出来」，真的是丢了东西
# （日本全国 0.035%、上海画框内 0.09%、中国 1483 座画框内见 `_road_coverage`）。
#
# **别的层不能套这个数**，套了就是自己把自己拦下来：
# * 轨道那边 `unknown` 占两成，是**有意丢**的——废线、停用线、在建线全标在那里（拍板 39）；
# * `base/land_use` 有八成是农田、住宅、学校、工业用地，`base/land` 有大半是树点与岩石，
#   它们本来就不画。拿「没翻出来的占比」当判据，这两层永远超标。
#
# **量在画框里量，不在抓取的矩形里量**（2026-09-09 改）。矩形是所有画框的并集外接框，
# 中国最西的城把它拉到 75.9°E，于是整个印度次大陆被圈进来：那边 road 一千五百万条、
# unknown 占 13.61%，把全矩形的比例推到 6.06%，闸门当场拦下一轮完全正常的中国。
# 100°E 以东（中国本土）是 0.35%。判据的原话是「那一层每一行都是我们可能画的路」，
# 印度的路一条都不会画——错的是分母，不是那 2%。
UNKNOWN_ROAD_LIMIT = 0.02


def _gate_unknown_roads(con, overture_dir: Path, *, gate: bool = True) -> float:
    """**画框里**的道路，Overture 没认出来的占多少；超过 `UNKNOWN_ROAD_LIMIT` 就停。

    要在 `frames` 表建好之后跑，因为判据只对画框里的路成立（那一节的注释讲了为什么
    不能对着抓取矩形量）。跨两个画框的路数两遍，与 `cut` 自己的口径一致。

    放在切之前而不是切之后：切是这一步里最贵的一段，而这个判据要拦的是「映射表跟
    上游对不上了」——那种情况下切出来的东西整批是错的，没有理由先花那几分钟。
    """
    total, unknown = con.sql(f"""
        SELECT count(*), count(*) FILTER (s.class = 'unknown')
        FROM {_boxed('segment', overture_dir, "subtype = 'road'")} s
        JOIN frames f ON s.x0 < f.xmax AND s.x1 > f.xmin AND s.y0 < f.ymax AND s.y1 > f.ymin
    """).fetchone()
    share = unknown / (total or 1)
    print(f"      画框内道路 {total:,} 条（跨框的重复计），Overture 没认出来的 {share:.2%}", flush=True)
    if gate and share > UNKNOWN_ROAD_LIMIT:
        raise SystemExit(f"画框内道路里 class='unknown' 占了 {share:.1%}，超过 "
                         f"{UNKNOWN_ROAD_LIMIT:.0%}——多半是 Overture 换了词表，"
                         f"对着 overture.py 的映射表看一眼再跑")
    return share


def _boxed(layer: str, overture_dir: Path, where: str) -> str:
    """一层的子查询，带上每行的包围盒——`geometry` 里没有现成的 bbox 列，得自己算。"""
    return (f"(SELECT subtype, class, geometry, ST_XMin(geometry) x0, ST_XMax(geometry) x1, "
            f"ST_YMin(geometry) y0, ST_YMax(geometry) y1 "
            f"FROM read_parquet('{overture_dir}/{layer}.parquet') WHERE {where})")


def _overlap(layer: str, overture_dir: Path, where: str) -> str:
    return (f"SELECT f.city, s.subtype, s.class, ST_AsWKB(s.geometry) AS wkb "
            f"FROM {_boxed(layer, overture_dir, where)} s "
            f"JOIN frames f ON s.x0 < f.xmax AND s.x1 > f.xmin AND s.y0 < f.ymax AND s.y1 > f.ymin")


# ---------- 折成 elements ----------

def to_elements(cut_parquet: Path, slug: str, *, con=None) -> dict:
    """一座城的要素，形状与旧的 `pbf.to_elements` 逐字段相同：线是 `way` + `geometry`，
    无洞的面是闭合的 `way`，有洞的面是带 outer / inner 成员的 `relation`——
    `mappack._polygons` 认的就是这两种。

    切好的那份按 slug 排过序，parquet 的行组统计因此能把扫描剪到那几组：
    实测一座城 20 毫秒，1739 座合计半分钟。画框里一条要素都没有的城返回空 elements。

    **不走「一趟顺序读、按 city 分组」那条路，试过两次都是坑。** 关掉 DuckDB 的行序保持
    （切城那一步必须关，否则几千万行排不动），并行扫描会把同一座城拆到不相邻的行组里，
    「换城就交出去」的分组把一座城读成好几段——1739 座读出两千多组，而下游只认第一段，
    图上少一半城区还一声不吭。开着行序保持，它又要先把 1.6 GB 的结果整个攒住再吐，
    二十分钟一组都没出来。按城取既不依赖行序，也不用攒。
    """
    con = con or connect()
    rows = con.execute(f"SELECT subtype, class, wkb FROM read_parquet('{cut_parquet}') "
                       f"WHERE city = ?", [slug]).fetchall()
    elements: list[dict] = []
    for subtype, cls, wkb in rows:
        _fold(elements, subtype, cls, wkb)
    return {"elements": elements}


def _fold(sink: list, subtype: str, cls: str, wkb: bytes) -> None:
    kind, payload = _geometry(memoryview(wkb), 0)[:2]
    if kind is None:
        return
    area = kind in ("polygon", "multipolygon")
    tags = tags_for(subtype, cls, area=area)
    if tags is None:
        return
    if subtype == OCEAN_SUBTYPE:
        for rings in (payload if kind == "multipolygon" else [payload]):
            sink += _sea_rings(tags, rings)
        return
    if kind == "linestring":
        sink.append({"type": "way", "tags": tags, "geometry": payload})
    elif kind == "multilinestring":
        sink += [{"type": "way", "tags": tags, "geometry": line} for line in payload]
    elif kind == "polygon":
        sink.append(_area(tags, payload))
    elif kind == "multipolygon":
        sink += [_area(tags, rings) for rings in payload]


def _area(tags: dict, rings: list) -> dict:
    outer, holes = rings[0], rings[1:]
    if not holes:
        return {"type": "way", "tags": tags, "geometry": outer}
    return {"type": "relation", "tags": tags,
            "members": [{"type": "way", "role": "outer", "geometry": outer}] +
                       [{"type": "way", "role": "inner", "geometry": hole} for hole in holes]}


def _sea_rings(tags: dict, rings: list) -> list[dict]:
    """海面的一个多边形 → 一环一个闭合 `way`，**绕向就是它的角色**。

    海面外环摆成顺时针、岛（洞）摆成逆时针，渲染器按 nonzero 填：岛在海里自然成洞，
    岛中的湖又自然填回水，不必逐层特判（`CityMapRenderer` 分两次填海与内陆水面的
    理由也在这里——两者绕向相反，合成一次会让河口绕数抵消成纸色）。

    **摆正而不是照搬。** Overture 给的是 OGC 的约定：外环逆时针、洞顺时针
    （实测全日本 743 个海面瓦片 743 个外环全是逆时针、抽样 400 个洞全是顺时针），
    正好与我们要的相反。照搬的后果是海陆对调，所以这里按角色算一次有向面积再定方向，
    不依赖上游哪天不改约定。
    """
    out = []
    for index, ring in enumerate(rings):
        clockwise = index == 0
        points = ring if (_signed_area(ring) < 0) == clockwise else ring[::-1]
        out.append({"type": "way", "tags": tags, "geometry": points})
    return out


def _signed_area(ring: list) -> float:
    """经纬度上的鞋带公式，y 朝北，> 0 是逆时针。投影保向，所以这里定的方向
    到了米制画布上还是同一个方向。"""
    total = 0.0
    for index in range(len(ring)):
        a, b = ring[index], ring[(index + 1) % len(ring)]
        total += a["lon"] * b["lat"] - b["lon"] * a["lat"]
    return total / 2.0


# WKB 的几何类型编号（OGC）。DuckDB 的 `ST_AsWKB` 出的就是这一套。
_POINT, _LINESTRING, _POLYGON, _MULTIPOINT, _MULTILINESTRING, _MULTIPOLYGON, _COLLECTION = range(1, 8)


def _geometry(buffer: memoryview, pos: int):
    """WKB → `(类型名, 内容, 下一个位置)`。线是点列，面是环列表，多重是它们的列表。

    自己读而不是走 shapely：这条管线的几何算法一律是二三十行的自带实现
    （`geometry` 那一节的「零依赖的代价」），而 WKB 只是「字节序 + 类型 + 计数 + 双精度」，
    引一个 C 扩展进来换不到什么。
    """
    little = buffer[pos] == 1
    pos += 1
    prefix = "<" if little else ">"
    kind = struct.unpack_from(f"{prefix}I", buffer, pos)[0]
    pos += 4
    if kind == _LINESTRING:
        points, pos = _points(buffer, pos, prefix)
        return "linestring", points, pos
    if kind == _POLYGON:
        rings, pos = _rings(buffer, pos, prefix)
        return "polygon", rings, pos
    if kind in (_MULTILINESTRING, _MULTIPOLYGON, _COLLECTION):
        count = struct.unpack_from(f"{prefix}I", buffer, pos)[0]
        pos += 4
        parts = []
        for _ in range(count):
            part_kind, payload, pos = _geometry(buffer, pos)
            if payload is not None:
                parts.append((part_kind, payload))
        if kind == _MULTILINESTRING:
            return "multilinestring", [payload for _, payload in parts], pos
        if kind == _MULTIPOLYGON:
            return "multipolygon", [payload for _, payload in parts], pos
        # 集合只在海面裁剪的边角上出现（画框正好切在海角上，交集里混进一条线或一个点）。
        # 只留面，其余在这里没有意义。
        polygons = [payload for part_kind, payload in parts if part_kind == "polygon"]
        return ("multipolygon", polygons, pos) if polygons else (None, None, pos)
    if kind == _POINT:
        return None, None, pos + 16
    if kind == _MULTIPOINT:
        count = struct.unpack_from(f"{prefix}I", buffer, pos)[0]
        return None, None, pos + 4 + count * 21
    raise ValueError(f"认不出的 WKB 类型：{kind}")


def _points(buffer: memoryview, pos: int, prefix: str):
    count = struct.unpack_from(f"{prefix}I", buffer, pos)[0]
    pos += 4
    flat = struct.unpack_from(f"{prefix}{count * 2}d", buffer, pos)
    return ([{"lon": flat[i], "lat": flat[i + 1]} for i in range(0, len(flat), 2)],
            pos + count * 16)


def _rings(buffer: memoryview, pos: int, prefix: str):
    count = struct.unpack_from(f"{prefix}I", buffer, pos)[0]
    pos += 4
    rings = []
    for _ in range(count):
        ring, pos = _points(buffer, pos, prefix)
        rings.append(ring)
    return rings, pos
