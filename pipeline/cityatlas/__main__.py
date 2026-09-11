"""命令行入口。`python3 -m cityatlas <命令>`，在 app/tools/CityAtlas/ 下跑。

    reference   从设计稿 HTML 解包出 CityMap 参考实现与两座城的数据
    selftest    管线自检：接环、内外环、二进制解析、编码往返、裁剪、投影、落盘确定性
    snapshot    向 Overpass 要各城的边界与要素快照，落盘到 snapshots/
    build       从快照生成 out/ 下的目录分片、地图包、continuity/latest 与索引页
    country     一个国家一次做完：从区域 pbf 全量读市级边界，切要素、出包、出分片
    review      一个国家跑完之后的取证页：名单、画框、切片三件事摊开给人看
    compare     生成「稿 vs 管线」并排比对页
    fixtures    把稿的上海、杭州转成 HereatTests/Fixtures/city-atlas/
    report      打印上一次 build 的体积与耗时表

抓取与生成是分开的两步：**快照一旦落盘，生成就只读文件**。每个包的 manifest 里记着
它是哪份快照产的（sha256）以及那份快照对应的 OSM 时刻，所以「同一输入重跑逐字节相同」
这条验收可核——比的是快照的 sha256 与产物的 sha256，不是「今天再抓一次」。
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
import shutil
import time
from pathlib import Path

from . import DIRECTORY_VERSION, MAP_DATA_VERSION, WORK
from . import boundary as boundary_module
from . import compare as compare_module
from . import directory as directory_module
from . import fixtures as fixtures_module
from . import mappack, overpass, overture, pbf, publish, reference, review, selftest
from . import frame as frame_module
from .frame import compute as compute_frame, covering_centres, from_admin_and_ghsl
from .ghsl import UCDB

ROOT = Path(__file__).resolve().parent.parent

REPO = ROOT.parent.parent.parent
DESIGN_HTML = REPO / "docs/散步/demo/2026-09-04-散步城市图 · 优化稿（离线）.html"
# 生成这些文件的脚本副本，随数据一起发布（ODbL 要求向接收者提供的那一份）。
# **写相对路径**：主源与备源是两个域名，绝对地址会把备源的读者指回主源；
# 而先前写死的 `github.com/hereat/app` 是**死链**（那个仓库不存在），
# 等于把许可要求的「机器可读副本」指进了空处。
#
# **指到具体文件，不指目录**：两个源都不做目录索引，`pipeline/` 本身在主源上落进
# Worker 的 404、在 GitHub Pages 上也是 404（2026-09-11 两个源各实测过）。
# 那一行是这份副本的唯一入口，指在目录上等于换了个 404。
# 副本由 `deploy/assemble.sh` 拷进 `pipeline/`。
PIPELINE_URL = "pipeline/README.md"

# 名册里 `country` 这一列记的是「这座城的包由哪个生产者出」，`out/` 由几个生产者共用：
# `country` 出一整个国家，`build` 出 `cities.json` 里那批样例城。各自只清各自的。
SAMPLE_SOURCE = "sample"


def human(size: int) -> str:
    return f"{size / 1024:.0f} KB" if size < 1024 * 1024 else f"{size / 1024 / 1024:.2f} MB"


def load_cities() -> list[dict]:
    return json.loads((ROOT / "cities.json").read_text(encoding="utf-8"))["cities"]


def _boundary(city: dict, *, refresh: bool = False):
    query = overpass.boundary_query(city["name"], tuple(city["levels"]), city["box"])
    data = overpass.fetch(query, ROOT / "snapshots" / f"{city['slug']}-boundary.json.gz", refresh=refresh)
    relations = [element for element in data["elements"] if element["type"] == "relation"]
    if not relations:
        raise SystemExit(f"{city['slug']}：在给的窗口里没找到 {city['name']} 的行政边界")
    # 同名多个时取成员最多的那个——行政边界的成员数就是它的边界复杂度，市级远多于同名的镇或区
    return boundary_module.parse(max(relations, key=lambda element: len(element.get("members", ()))))


def _snapshot(slug: str) -> tuple[dict, dict]:
    """读要素快照，同时给出它的来源指纹：文件 sha256 与 Overpass 报的 OSM 时刻。"""
    path = ROOT / "snapshots" / f"{slug}-features.json.gz"
    if not path.exists():
        raise SystemExit(f"{slug}：先跑 `python3 -m cityatlas snapshot`")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        data = json.load(handle)
    return data, {
        "snapshot": path.name,
        "snapshotSha256": digest,
        "snapshotBytes": path.stat().st_size,
        "osmBase": data.get("osm3s", {}).get("timestamp_osm_base"),
    }


# ---------- 命令 ----------

def cmd_reference(args) -> None:
    for key, path in reference.unpack(DESIGN_HTML, ROOT / "reference").items():
        print(f"{key:10s} {path.relative_to(ROOT)}  {human(path.stat().st_size)}")


def cmd_snapshot(args) -> None:
    ucdb = UCDB(Path(args.ucdb))
    for city in load_cities():
        if args.only and city["slug"] not in args.only:
            continue
        boundary = _boundary(city, refresh=args.refresh)
        centres = covering_centres(boundary, ucdb)
        frame = compute_frame(boundary, centres)
        path = ROOT / "snapshots" / f"{city['slug']}-features.json.gz"
        data = overpass.fetch(overpass.feature_query(frame.bounds()), path, refresh=args.refresh)
        # 报一句 OSM 时刻：几个 Overpass 镜像的数据新旧差好几个月，
        # 同一版目录里混着几个月前的城不是有意的。
        print(f"{city['slug']:10s} 画框 {frame.width}×{frame.height} m  锚点 {frame.anchor}  "
              f"要素快照 {human(path.stat().st_size)}  OSM 截至 "
              f"{data.get('osm3s', {}).get('timestamp_osm_base', '未知')}")


def _select_cities(units: list[dict], ucdb, cover_levels: tuple[int, ...],
                   region_suffix: tuple[str, ...] = ()) -> list[dict]:
    """从行政单位里选出城市名单（规则见 `frame` 的「城市名单」一节）。

    `cover_levels` 是**覆盖层**：铺满国土、一定收、不会被别座城吞掉的那一层
    （中国的地级市与直辖市，日本的市町村）。其余层是**候补层**，只在自成一片
    建成区时才独立成城。哪一层担哪个角色**逐国不同**，所以由 `pbf.CITY_LEVELS`
    说了算，不能按级数大小判：日本两个角色都落在 `admin_level 7` 上，
    原来写死的 `level <= 5` 在那里是空集，一座城都选不出来。

    每座城同时带上它那片建成区的探针点，`drop_swallowed` 靠它判断「这个区县的地盘上
    是不是已经有别座城的主城铺过来」。`region_suffix` 是那条判断的例外：名字属于一片
    区域的覆盖层单位（自治州、盟）谁也不吞，出处见 `pbf.CITY_LEVELS`。
    """
    prepared = []
    for unit in units:
        bound = boundary_module.Boundary(
            osm_relation=unit["osm_id"], admin_level=int(unit["tags"].get("admin_level", 0)),
            name_zh=unit["name"], name_local=unit["name_local"], name_en=unit["name_en"],
            polygons=unit["polygons"])
        box = bound.bbox
        prepared.append({**unit, "boundary": bound, "bbox": box, "level": bound.admin_level,
                         "area": (box[2] - box[0]) * (box[3] - box[1])})

    patches = []
    for centre in ucdb.centres():
        points = [point for polygon in ucdb.polygons(centre.uc_id) for point in polygon["o"]]
        if not points:
            continue
        probe = points[:: max(1, len(points) // 40)] or points
        xs = [x for x, _ in probe]
        ys = [y for _, y in probe]
        patches.append({"probe": probe, "area": centre.area_km2,
                        "box": (min(xs), min(ys), max(xs), max(ys))})

    entries: dict = {}

    def offer(unit, patch):
        key = unit["osm_id"]
        if key not in entries or patch["area"] > entries[key]["area"]:
            entries[key] = {"unit": unit, "probe": patch["probe"],
                            "box": patch["box"], "area": patch["area"]}

    covering = [unit for unit in prepared if unit["level"] in cover_levels]
    for patch in patches:
        for unit in covering:
            box = unit["bbox"]
            if any(box[0] <= x <= box[2] and box[1] <= y <= box[3]
                   and unit["boundary"].contains((x, y)) for x, y in patch["probe"]):
                offer(unit, patch)
        unit = frame_module.smallest_containing(patch["probe"], prepared)
        if unit is not None and unit["level"] not in cover_levels:
            offer(unit, patch)

    # 一片建成区都没有的覆盖层单位照收。西藏的昌都、山南、林芝就是这样：GHSL 在那儿
    # 一片城市中心都没有，可它们确实是市（2026-09-07 实测，按「有建成区」筛会把这三座
    # 从名单里漏掉，而上一版名单里有）。没有建成区不代表没有城，只代表画框得靠
    # OSM 的市中心点来定——那条退路 `from_admin_and_ghsl` 里已经有了。
    for unit in covering:
        entries.setdefault(unit["osm_id"],
                           {"unit": unit, "probe": [], "box": unit["bbox"], "area": 0.0})

    kept = frame_module.drop_swallowed(sorted(entries.values(), key=lambda e: -e["area"]),
                                       cover_levels, region_suffix)
    return [entry["unit"] for entry in kept]


def cmd_country(args) -> None:
    """一个国家一次做完：行政边界与街道数据都出自同一份区域 pbf。

    与 `build` 的分别只在**城市名单与边界从哪来**：那边是逐城手写的 `cities.json`
    加 Overpass 逐城查询，这边是「把区域文件里的市级边界全读出来」。往下——画框、
    切矢量、压包、分片、索引页——两条路一模一样，所以下游一行都没有为这条路改过。
    """
    source = Path(args.pbf)
    rule = pbf.CITY_LEVELS.get(args.country)
    if not rule:
        raise SystemExit(f"{args.country}：不知道这个国家的「市」是第几级，"
                         f"往 pbf.CITY_LEVELS 里加一行（出处见那里的注释）")
    # 半份名单（`--only` / `--limit`）不重写分片，也不该用整国的标尺过闸门
    partial = bool(args.only or args.limit)
    levels, suffix, exclude = rule["levels"], rule.get("suffix"), rule.get("exclude", ())
    cover_levels, require = tuple(rule["cover"]), rule.get("require", ())
    region_suffix = tuple(rule.get("region_suffix", ()))
    names = rule.get("names", ())

    work = WORK / args.country.lower().replace(" ", "-")
    work.mkdir(parents=True, exist_ok=True)
    ucdb = UCDB(Path(args.ucdb))

    print(f"[1/4] 读 {args.country} 的市级行政边界（admin_level {'/'.join(map(str, levels))}）", flush=True)
    # 文件名带上层级：改了要哪几级还读旧缓存，会静默少掉一整层
    # （2026-09-07 实测：加了区县那一级，读到的仍是只有 4/5 级的那份）
    tag = "-".join(map(str, levels))
    boundaries_path = pbf.admin_boundaries(source, work / f"admin-{tag}.geojsonseq", levels)

    cities = []
    for osm_id, tags, polygons in pbf.read_boundaries(boundaries_path):
        name = boundary_module.chinese_name(tags) or tags.get("name")
        if not name:
            continue
        # 专名白名单先过：特别行政区按后缀筛不出来，而同名的关系可能不止一个
        # （香港在 3 级与 4 级各有一份），所以这条白名单连级数一起认（`pbf.CITY_LEVELS`）
        if name in names:
            if int(tags.get("admin_level", 0)) != names[name]:
                continue
        else:
            if suffix and not name.endswith(tuple(suffix)):
                continue
            if exclude and name.endswith(tuple(exclude)):
                continue
        if any(key not in tags for key in require):
            continue
        cities.append({"name": name,
                       "name_local": tags.get("name") or name,
                       "name_en": boundary_module.english_name(tags),
                       "osm_id": osm_id,
                       "polygons": polygons,
                       "tags": tags})
    print(f"      {len(cities)} 个行政单位", flush=True)

    # 名单不是「读到的边界全都要」，而是按 `frame` 那一节的两条规则合成：
    # 有建成区的地级市 / 直辖市，加上完整包住一片独立建成区、且不属于任何主城的区县。
    cities = _select_cities(cities, ucdb, cover_levels, region_suffix)
    print(f"      选出 {len(cities)} 座城", flush=True)
    if args.limit:
        cities = cities[:args.limit]
        print(f"      --limit {args.limit}，只做前 {len(cities)} 座", flush=True)
    if args.only:
        cities = [city for city in cities if city["name"] in set(args.only)]
        print(f"      --only，只做 {'、'.join(city['name'] for city in cities)}", flush=True)

    print("[2/4] 算画框（GHSL 城区 ∩ 行政区），默认取景以 OSM 市中心为心", flush=True)
    # 文件名带上认哪几种 place，理由同上面的层级：改了要哪几种还读旧缓存，
    # 会静默少掉一整类（日本的町村全靠 village 那一类）。
    places = pbf.city_centres(source, work / f"places-{'-'.join(pbf.PLACE_KINDS)}.geojsonseq")
    # 名字认不到 place 节点时的第二条路：关系上的 `admin_centre`，也就是驻地。
    # 自治州、盟这类单位的名字本来就不是任何 place 节点的名字（见 `pbf.admin_centres`）。
    seats = pbf.admin_centres(boundaries_path.with_suffix(".pbf"), work / f"seats-{tag}.json")
    boxes = {}
    without_centre = []
    from_seat = []
    for city in cities:
        bound = boundary_module.Boundary(
            osm_relation=city["osm_id"] or 0, admin_level=int(city["tags"].get("admin_level", 0)),
            name_zh=city["name"], name_local=city["name_local"], name_en=city["name_en"],
            polygons=city["polygons"])
        centres = covering_centres(bound, ucdb)
        centre = pbf.centre_for(boundary_module.spellings(city["tags"]), bound, places)
        if centre is None:
            centre = seats.get(city["osm_id"])
            (from_seat if centre else without_centre).append(city["name"])
        frame = from_admin_and_ghsl(bound, centres, ucdb, city_centre=centre)
        city["boundary"] = bound
        city["frame"] = frame
        city["slug"] = f"{args.country.lower()[:2]}-{city['osm_id']}"
        boxes[city["slug"]] = frame.bounds()

    if from_seat:
        print(f"      {len(from_seat)} 座的市中心取自关系上的驻地（admin_centre）："
              f"{'、'.join(from_seat[:8])}{' 等' if len(from_seat) > 8 else ''}", flush=True)
    if without_centre:
        # 报出来而不是静默：这些城连驻地都没有，画框退回外接框中心，可能落在没有街道的地方
        print(f"      {len(without_centre)} 座既没有同名 place 节点、也没有驻地，"
              f"画框退回外接框中心："
              f"{'、'.join(without_centre[:8])}{' 等' if len(without_centre) > 8 else ''}", flush=True)
    if not boxes:
        raise SystemExit("一座城都没选出来——`--only` 给的名字要与名单里的写法一致（如「上海市」）")
    print(f"[3/4] 取 {len(boxes)} 座城的要素（Overture {overture.RELEASE}）", flush=True)
    # 抓的是**所有画框的并集**，不是国土外接框：扫描包围盒是远程查询的主要成本，
    # 而画框加起来常常只占国土的一部分（`--only` 跑一座城时就只拉那一座的框）。
    bbox = overture.union_bbox(boxes)
    print(f"      包围盒 {bbox[0]:.3f},{bbox[1]:.3f} – {bbox[2]:.3f},{bbox[3]:.3f}", flush=True)
    connection = overture.connect(temp_dir=work / "duckdb-spill")
    overture.fetch(bbox, work / "overture", con=connection)
    cut_path = overture.cut(work / "overture", boxes, work / "cut.parquet", con=connection,
                            gate=not partial)
    print(f"      切好 {cut_path.stat().st_size / 1e6:.0f} MB", flush=True)

    print("[4/4] 出包")
    stamp = pbf.source_stamp(source)
    print(f"      行政边界的 OSM 截至 {stamp['osmBase']}，要素来自 Overture {overture.RELEASE}", flush=True)
    _emit(cities, lambda slug: overture.to_elements(cut_path, slug, con=connection),
          ucdb=ucdb, ghsl=Path(args.ucdb).name, stamp=stamp,
          out=ROOT / "out", country=args.country, partial=partial)


def _emit(cities, snapshot_of, *, ucdb, ghsl, stamp, out, country: str, partial: bool = False) -> None:
    """出包 + 分片 + 索引页。与 `cmd_build` 的尾巴同一件事，暂各写一份——
    两条路的产物格式一致之后再合并。

    **一轮只对这一个国家负责。** `out/` 是全球共用的：中国跑完再跑日本，日本这一轮
    既要把中国的包原样留着，又要把中国不再有的城清掉——只清自己那一份。
    「哪些城是这一国的」由名册里的 `country` 说了算（`publish.assign` 每轮刷新）。

    这不是「先清空再重建」的变体，那条路走不通：跑第二个国家会把第一个删光
    （原来就是这么写的，因为当时中国就是全部）。
    """
    registry = publish.load_registry(ROOT / "registry.json")
    # 这一国上一轮发布过的号。第一次跑某个国家时是空集，于是什么都不会被清掉。
    previously_mine = {entry["cityID"] for entry in registry["cities"].values()
                       if entry.get("country") == country}
    ghsl_stamp = {"ghsl": ghsl, "pipeline": "app/tools/CityAtlas"}
    shard_cities: dict = {}
    produced: set[str] = set()
    done = 0

    # `snapshot_of` 按 slug 取一座城的要素。一个国家的要素同时摆在内存里放不下，
    # 所以是逐城取而不是一次全读（`overture.to_elements` 的注释讲了为什么不走顺序流）。
    for city in cities:
        snapshot = snapshot_of(city["slug"])
        frame = city["frame"]
        city_id = publish.assign(registry, city["osm_id"],
                                 {"slug": city["slug"], "name": city["name"], "country": country,
                                  "assignedInDirectoryVersion": DIRECTORY_VERSION})
        produced.add(city_id)
        package = mappack.build(snapshot, name=city["name"], name_local=city["name_local"],
                                center=frame.center, frame=frame.size)
        package.update({
            "cityID": city_id,
            "directoryVersion": DIRECTORY_VERSION,
            "mapDataVersion": MAP_DATA_VERSION,
            "bounds": list(frame.bounds()),
            "defaultView": frame.default_view,
            "contentHash": mappack.content_hash(package),
            "license": publish.license_block({**ghsl_stamp, **stamp}),
        })
        path = out / "package" / city_id / f"{MAP_DATA_VERSION}.json.gz"
        publish.write_gzip_json(path, package)
        entry = {"cityID": city_id, "name": city["name"], "nameLocal": city["name_local"],
                 "nameEn": city["name_en"], "mapDataVersion": MAP_DATA_VERSION, "center": package["center"],
                 "frame": package["frame"], "bounds": package["bounds"]}
        for cell, shard_entry in directory_module.entries(entry, city["boundary"]).items():
            shard_cities.setdefault(cell, []).append(shard_entry)
        done += 1
        if done % 50 == 0 or done == len(cities):
            print(f"      {done}/{len(cities)}", flush=True)
    publish.save_registry(registry, ROOT / "registry.json")
    if partial:
        # 分片是全局产物，按半份名单重写会静默丢掉同格子里的别的城（同 `cmd_build`）。
        print("\n只跑了部分城市，分片没有重写——发布前跑一次完整的 country。")
        return

    # 这一国不再有的城：包删掉，下面的分片合并也会把它们从格子里摘走。
    retired = previously_mine - produced
    for city_id in sorted(retired):
        shutil.rmtree(out / "package" / city_id, ignore_errors=True)
    if retired:
        print(f"      退役 {len(retired)} 座（{country} 上一轮有、这一轮没有）", flush=True)

    licenses = publish.license_block(ghsl_stamp)
    shards = _write_shards(out, shard_cities, mine=previously_mine | produced, licenses=licenses)
    _write_json(out / "directory" / "latest.json", publish.latest(DIRECTORY_VERSION, ghsl_stamp))

    # 索引页也得跟着重写。它是 CDN 的落地页，写着这份数据有多少座城、出自哪一刻的 OSM、
    # 授权是什么——`cmd_build` 一直在写，这条路上漏了，于是全国跑完之后公网上挂的还是
    # 七座样例城那一版（2026-09-07 实测）。
    packages = _published_packages(out)
    (out / "index.html").write_text(
        publish.index_page(directory_version=DIRECTORY_VERSION,
                           generated_at=dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M UTC"),
                           osm_stamp=f"区域 pbf，OSM 数据截至 {stamp['osmBase']}",
                           pipeline_url=PIPELINE_URL, shards=shards,
                           packages=[_package_row(package) for package in packages]),
        encoding="utf-8")
    print(f"      分片 {len(shards)} 个，索引页 {len(packages)} 座", flush=True)


def _write_shards(out: Path, shard_cities: dict, *, mine: set[str], licenses: dict) -> list[dict]:
    """把这一轮的城并进盘上已有的分片，别国的条目原样留着。

    规则只有一条：**每个格子里，属于我这一国的条目全部换成这一轮产出的**，其余不动。
    `mine` 是这一国上一轮与这一轮的号的并集，所以三种情况一次覆盖——这一轮还在的城
    换成新条目、这一轮退役的城被摘走、这一轮换了格子的城不会在旧格子里留一份。

    「没访问到的格子也要扫」是必须的：一座城的画框挪了位置，它的旧格子这一轮不会被写到，
    只看这一轮写的格子就会在旧格子里留下一个指向新包的错条目。

    **目录一升版本，上一版的城要跟过来。** 分片按版本分目录，新版本那个目录是空的：
    只并盘上同版本的分片，等于「凡是这一轮没跑的国家全部消失」——升 v3 时中国重跑了，
    日本的 1739 座会从目录里蒸发，而 `latest.json` 已经指向 v3，日本用户的散步当场
    判不出城。这与「跑第二个国家不许动第一个」是同一条保证，只是跨了一次版本。
    """
    directory = out / "directory" / f"v{DIRECTORY_VERSION}"
    directory.mkdir(parents=True, exist_ok=True)
    merged = {cell: list(entries) for cell, entries in shard_cities.items()}
    previous = directory
    if not any(directory.glob("*.json.gz")):
        previous = out / "directory" / f"v{DIRECTORY_VERSION - 1}"
    for path in sorted(previous.glob("*.json.gz")):
        cell = directory_module.cell_of(path.stem.removesuffix(".json"))
        kept = [entry for entry in _read_shard(path) if entry["cityID"] not in mine]
        if cell in merged:
            merged[cell] = kept + merged[cell]
        elif kept:
            merged[cell] = kept
        elif previous == directory:
            path.unlink()          # 这一国搬走之后这格空了（上一版的分片原样留着）

    shards = []
    for cell, entries in sorted(merged.items()):
        path = directory / f"{directory_module.cell_name(cell)}.json.gz"
        publish.write_gzip_json(path, directory_module.shard(cell, entries, licenses))
        shards.append({"cell": f"{cell[0]}, {cell[1]}",
                       "cities": "、".join(entry["name"] for entry in sorted(entries, key=lambda e: e["cityID"])),
                       "path": f"directory/v{DIRECTORY_VERSION}/{path.name}",
                       "size": human(path.stat().st_size)})

    # 新一代写完了，才轮到清旧代——清在前面就把上面那份「别国的城从哪读」删掉了
    for version in publish.prune_directories(out, DIRECTORY_VERSION):
        print(f"      目录只留最近两代，删掉 v{version} 的分片", flush=True)
    return shards


def _read_shard(path: Path) -> list[dict]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)["cities"]


def cmd_build(args) -> None:
    """Overpass 那条路：`cities.json` 里那批样例城，产物落在 `out-sample/`。

    **它不再往 `out/` 里写。** `out/` 是发布目录，由 `country` 一国一轮地产出；
    而 `cities.json` 里有上海、杭州、成都，两条路会争同一批 `cityID`——谁后跑谁的包
    留在盘上，画框还不一样（这条路是 6000 m 的固定框）。分开目录之后
    `out/` 的每座城只有一个生产者，这条路回到它现在唯一的用途：**对照**，
    同一座城两条路出来的包应当一致（[09-06 全球覆盖实施三.4](../../docs/散步/2026-09-06-散步城市图-全球覆盖与缩放-实施.md)）。
    """
    started_all = time.monotonic()
    ucdb = UCDB(Path(args.ucdb))
    out = Path(args.out) if args.out else ROOT / "out-sample"
    generated_at = dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M UTC")
    ghsl_stamp = {"ghsl": Path(args.ucdb).name, "pipeline": "app/tools/CityAtlas"}

    previous_ids = _published_city_ids(out)
    registry = publish.load_registry(ROOT / "registry.json")
    # `out/` 是全球共用的，`build` 只对 `cities.json` 那批样例城负责。
    # 原来这里清空整个 out/，那是「样例城就是全部」时代的写法——现在跑一次
    # `build` 会把全国 1458 座删光。判据与 `country` 一致：名册里的 `country`。
    previously_mine = {entry["cityID"] for entry in registry["cities"].values()
                       if entry.get("country") == SAMPLE_SOURCE}
    produced: set[str] = set()
    shard_cities: dict[tuple[int, int], list[dict]] = {}
    report: list[dict] = []

    for city in load_cities():
        if args.only and city["slug"] not in args.only:
            continue
        started = time.monotonic()
        boundary = _boundary(city)
        centres = covering_centres(boundary, ucdb)
        frame = compute_frame(boundary, centres)
        city_id = publish.assign(registry, boundary.osm_relation,
                                 {"slug": city["slug"], "name": boundary.name_zh,
                                  "country": SAMPLE_SOURCE,
                                  "assignedInDirectoryVersion": DIRECTORY_VERSION})
        produced.add(city_id)
        snapshot, source = _snapshot(city["slug"])
        if not mappack.covers(snapshot, frame.bounds()):
            raise SystemExit(f"{city['slug']}：快照盖不住当前画框，重跑 "
                             f"`snapshot --refresh --only {city['slug']}`")
        loaded = time.monotonic()

        package = mappack.build(snapshot, name=boundary.name_zh, name_local=boundary.name_local,
                                center=frame.center, frame=frame.size)
        package.update({
            "cityID": city_id,
            "directoryVersion": DIRECTORY_VERSION,
            "mapDataVersion": MAP_DATA_VERSION,
            "bounds": list(frame.bounds()),
            "contentHash": mappack.content_hash(package),
            "license": publish.license_block({**ghsl_stamp, **source}),
        })
        package_path = out / "package" / city_id / f"{MAP_DATA_VERSION}.json.gz"
        raw_size = publish.write_gzip_json(package_path, package)
        built = time.monotonic()

        entry = {"cityID": city_id, "name": boundary.name_zh, "nameLocal": boundary.name_local,
                 "nameEn": boundary.name_en, "mapDataVersion": MAP_DATA_VERSION, "center": package["center"],
                 "frame": package["frame"], "bounds": package["bounds"]}
        for cell, shard_entry in directory_module.entries(entry, boundary).items():
            shard_cities.setdefault(cell, []).append(shard_entry)
        finished = time.monotonic()

        report.append({
            "slug": city["slug"], "name": boundary.name_zh, "cityID": city_id,
            "osmRelation": boundary.osm_relation,
            "frame": [frame.width, frame.height], "anchor": frame.anchor,
            "ucdbCovered": frame.ucdb_covered, "source": source,
            "packageRawBytes": raw_size, "packageBytes": package_path.stat().st_size,
            "packageSha256": hashlib.sha256(package_path.read_bytes()).hexdigest(),
            "seconds": {"loadSnapshot": round(loaded - started, 2),
                        "buildPackage": round(built - loaded, 2),
                        "buildShards": round(finished - built, 2),
                        "total": round(finished - started, 2)},
            "stats": mappack.stats(package),
        })
        print(f"{city['slug']:10s} {city_id}  画框 {frame.width}×{frame.height}m（{frame.anchor}）"
              f"  包 {human(package_path.stat().st_size)}（原始 {human(raw_size)}）"
              f"  {report[-1]['seconds']['total']}s")

    publish.save_registry(registry, ROOT / "registry.json")
    if args.only:
        # `--only` 只出包：分片、continuity 与索引页是全局产物，
        # 按半份城市重写它们会静默丢掉别的城。要发布就跑一次完整的 build。
        print("\n只跑了部分城市，分片、continuity 与索引页没有重写——发布前跑一次完整 build。")
        return

    for city_id in sorted(previously_mine - produced):
        shutil.rmtree(out / "package" / city_id, ignore_errors=True)

    licenses = publish.license_block(ghsl_stamp)
    shards = _write_shards(out, shard_cities, mine=previously_mine | produced, licenses=licenses)
    for row in shards:
        row["bytes"] = (out / row["path"]).stat().st_size

    packages = _published_packages(out)
    previous_version = DIRECTORY_VERSION - 1 if previous_ids and DIRECTORY_VERSION > 1 else None
    _write_json(out / "directory" / f"v{DIRECTORY_VERSION}" / "continuity.json",
                publish.continuity(previous_ids, {package["cityID"] for package in packages},
                                   previous_version, DIRECTORY_VERSION))
    _write_json(out / "directory" / "latest.json", publish.latest(DIRECTORY_VERSION, ghsl_stamp))
    (out / "index.html").write_text(
        publish.index_page(directory_version=DIRECTORY_VERSION, generated_at=generated_at,
                           osm_stamp=_osm_stamp(report), pipeline_url=PIPELINE_URL,
                           shards=shards, packages=[_package_row(package) for package in packages]),
        encoding="utf-8")

    total = round(time.monotonic() - started_all, 2)
    _write_json(ROOT / "report.json",
                {"generatedAt": generated_at, "totalSeconds": total, "shards": shards, "cities": report})
    print(f"\n分片 {len(shards)} 个，全部 {total}s。报表 report.json，索引页 out/index.html")


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _osm_stamp(report: list[dict]) -> str:
    bases = sorted({city["source"]["osmBase"] for city in report if city["source"]["osmBase"]})
    return f"Overpass 快照，OSM 数据截至 {bases[0]} – {bases[-1]}" if bases else "Overpass 快照"


def _published_packages(out: Path) -> list[dict]:
    """out/ 里已经发布的全部地图包。索引页与 continuity 看的是这个。"""
    published = []
    for path in sorted(out.glob("package/*/*.json.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            package = json.load(handle)
        published.append({"cityID": package["cityID"], "name": package["name"],
                          "frame": package["frame"], "mapDataVersion": package["mapDataVersion"],
                          "bytes": path.stat().st_size,
                          "path": f"package/{package['cityID']}/{package['mapDataVersion']}.json.gz"})
    return published


def _published_city_ids(out: Path) -> set[str]:
    return {path.parent.name for path in out.glob("package/*/*.json.gz")}


def _package_row(package: dict) -> dict:
    return {"name": package["name"], "cityID": package["cityID"],
            "frame": f"{package['frame'][0]} × {package['frame'][1]} m",
            "path": package["path"], "size": human(package["bytes"])}


def cmd_report(args) -> None:
    report = json.loads((ROOT / "report.json").read_text(encoding="utf-8"))
    print(f"{'城市':<10} {'cityID':<8} {'画框':<14} {'包(gz)':>9} {'包(原始)':>10} {'生成':>7}  快照")
    for city in report["cities"]:
        print(f"{city['name']:<10} {city['cityID']:<8} "
              f"{city['frame'][0]}×{city['frame'][1]} m{'':<3} "
              f"{human(city['packageBytes']):>9} {human(city['packageRawBytes']):>10} "
              f"{city['seconds']['buildPackage']:>6.1f}s  {human(city['source']['snapshotBytes'])}")
    print(f"\n分片：{len(report['shards'])} 个，"
          f"{human(min(s['bytes'] for s in report['shards']))} – {human(max(s['bytes'] for s in report['shards']))}")
    print(f"全部 {report['totalSeconds']}s")


def cmd_review(args) -> None:
    path = review.write(ROOT, args.country)
    print(f"取证页 {path.relative_to(ROOT)}  {human(path.stat().st_size)}")


def cmd_compare(args) -> None:
    registry = publish.load_registry(ROOT / "registry.json")
    entry = next((city for city in registry["cities"].values() if city.get("slug") == args.slug), None)
    if entry is None:
        raise SystemExit(f"registry.json 里没有 {args.slug}，先跑 build")
    print(compare_module.write(ROOT, args.slug, entry["cityID"], args.style))


def cmd_fixtures(args) -> None:
    for path in fixtures_module.build(ROOT, REPO):
        print(f"{path.relative_to(REPO)}  {human(path.stat().st_size)}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="cityatlas", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("reference").set_defaults(handler=cmd_reference)
    sub.add_parser("selftest").set_defaults(handler=lambda args: selftest.run())
    sub.add_parser("report").set_defaults(handler=cmd_report)
    sub.add_parser("fixtures").set_defaults(handler=cmd_fixtures)

    snapshot = sub.add_parser("snapshot")
    snapshot.add_argument("--refresh", action="store_true", help="忽略快照缓存，重新向 Overpass 要")
    build = sub.add_parser("build")
    for command, handler in ((snapshot, cmd_snapshot), (build, cmd_build)):
        command.add_argument("--ucdb", required=True, help="GHS_UCDB_GLOBE_R2024A.gpkg 的路径")
        command.add_argument("--only", nargs="*", help="只跑这几个 slug")
        command.set_defaults(handler=handler)
    build.add_argument("--out", help="产物目录，默认 out-sample/（发布目录 out/ 归 country 命令）")

    country = sub.add_parser("country")
    country.add_argument("--country", required=True, help="GADM 国名，如 China")
    country.add_argument("--pbf", required=True, help="Geofabrik 的区域 .osm.pbf")
    country.add_argument("--ucdb", required=True, help="GHS_UCDB_GLOBE_R2024A.gpkg 的路径")
    country.add_argument("--limit", type=int, default=0, help="只做前 N 座，用来先跑通")
    country.add_argument("--only", nargs="+", default=None,
                         help="只重做这几座（按中文名），其余产物原样留着")
    country.set_defaults(handler=cmd_country)

    review_page = sub.add_parser("review")
    review_page.add_argument("--country", required=True, help="给哪一国出取证页，如 Japan")
    review_page.set_defaults(handler=cmd_review)

    compare = sub.add_parser("compare")
    compare.add_argument("slug", help="reference/cities/ 里有数据的城（shanghai 或 hangzhou）")
    compare.add_argument("--style", default="plain")
    compare.set_defaults(handler=cmd_compare)

    args = parser.parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
