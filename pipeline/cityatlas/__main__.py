"""命令行入口。`python3 -m cityatlas <命令>`，在 app/tools/CityAtlas/ 下跑。

    reference   从设计稿 HTML 解包出 CityMap 参考实现与两座城的数据
    selftest    管线自检：接环、内外环、二进制解析、编码往返、裁剪、投影、落盘确定性
    snapshot    向 Overpass 要各城的边界与要素快照，落盘到 snapshots/
    build       从快照生成 out/ 下的目录分片、地图包、continuity/latest 与索引页
    country     一个国家一次做完：从区域 pbf 全量读市级边界，切要素、出包、出分片
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

from . import DIRECTORY_VERSION, MAP_DATA_VERSION
from . import boundary as boundary_module
from . import compare as compare_module
from . import directory as directory_module
from . import fixtures as fixtures_module
from . import mappack, overpass, pbf, publish, reference, selftest
from .frame import compute as compute_frame, covering_centres, from_admin_and_ghsl
from .ghsl import UCDB

ROOT = Path(__file__).resolve().parent.parent
REPO = ROOT.parent.parent.parent
DESIGN_HTML = REPO / "docs/散步/demo/2026-09-04-散步城市图 · 优化稿（离线）.html"
PIPELINE_URL = "https://github.com/hereat/app/tree/main/app/tools/CityAtlas"


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
    levels, suffix = rule["levels"], rule.get("suffix")

    work = ROOT / "work" / args.country.lower().replace(" ", "-")
    work.mkdir(parents=True, exist_ok=True)
    ucdb = UCDB(Path(args.ucdb))

    print(f"[1/4] 读 {args.country} 的市级行政边界（admin_level {'/'.join(map(str, levels))}）", flush=True)
    boundaries_path = pbf.admin_boundaries(source, work / "admin.geojsonseq", levels)

    cities = []
    for osm_id, tags, polygons in pbf.read_boundaries(boundaries_path):
        name = tags.get("name:zh") or tags.get("name")
        if not name:
            continue
        if suffix and not name.endswith(suffix):
            continue
        cities.append({"name": name,
                       "name_local": tags.get("name") or name,
                       "osm_id": osm_id,
                       "polygons": polygons,
                       "tags": tags})
    print(f"      {len(cities)} 座", flush=True)
    if args.limit:
        cities = cities[:args.limit]
        print(f"      --limit {args.limit}，只做前 {len(cities)} 座", flush=True)

    print("[2/4] 算画框（GHSL 城区 ∩ 行政区），默认取景以 OSM 市中心为心", flush=True)
    places = pbf.city_centres(source, work / "places.geojsonseq")
    boxes = {}
    without_centre = []
    for city in cities:
        bound = boundary_module.Boundary(
            osm_relation=city["osm_id"] or 0, admin_level=int(city["tags"].get("admin_level", 0)),
            name_zh=city["name"], name_local=city["name_local"], polygons=city["polygons"])
        centres = covering_centres(bound, ucdb)
        centre = pbf.centre_for(city["name"], bound, places)
        if centre is None:
            without_centre.append(city["name"])
        frame = from_admin_and_ghsl(bound, centres, ucdb, city_centre=centre)
        city["boundary"] = bound
        city["frame"] = frame
        city["slug"] = f"{args.country.lower()[:2]}-{city['osm_id']}"
        boxes[city["slug"]] = frame.bounds()

    if without_centre:
        # 报出来而不是静默：这些城的默认取景退回画框中心，可能偏出市中心
        print(f"      {len(without_centre)} 座没找到 OSM 市中心点，默认取景退回画框中心："
              f"{'、'.join(without_centre[:8])}{' 等' if len(without_centre) > 8 else ''}", flush=True)
    print(f"[3/4] 从区域文件切出 {len(boxes)} 座城的要素", flush=True)
    filtered = pbf.filter_tags(source, work / "features.osm.pbf")
    extracts = pbf.extract(filtered, boxes, work / "extract")

    print("[4/4] 出包")
    stamp = pbf.source_stamp(source)
    print(f"      OSM 截至 {stamp['osmBase']}", flush=True)
    _emit(cities, extracts, ucdb=ucdb, ghsl=Path(args.ucdb).name, stamp=stamp, out=ROOT / "out")


def _emit(cities, extracts, *, ucdb, ghsl, stamp, out) -> None:
    """出包 + 分片 + 索引页。与 `cmd_build` 的尾巴同一件事，暂各写一份——
    两条路的产物格式一致之后再合并，先让中国这一轮跑通。"""
    registry = publish.load_registry(ROOT / "registry.json")
    ghsl_stamp = {"ghsl": ghsl, "pipeline": "app/tools/CityAtlas"}
    shard_cities: dict = {}
    done = 0
    for city in cities:
        frame = city["frame"]
        snapshot = pbf.to_elements(extracts[city["slug"]])
        city_id = publish.assign(registry, city["osm_id"],
                                 {"slug": city["slug"], "name": city["name"],
                                  "assignedInDirectoryVersion": DIRECTORY_VERSION})
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
                 "mapDataVersion": MAP_DATA_VERSION, "center": package["center"],
                 "frame": package["frame"], "bounds": package["bounds"]}
        for cell, shard_entry in directory_module.entries(entry, city["boundary"]).items():
            shard_cities.setdefault(cell, []).append(shard_entry)
        done += 1
        if done % 50 == 0 or done == len(cities):
            print(f"      {done}/{len(cities)}", flush=True)
    publish.save_registry(registry, ROOT / "registry.json")

    licenses = publish.license_block(ghsl_stamp)
    for cell, entries in sorted(shard_cities.items()):
        path = out / "directory" / f"v{DIRECTORY_VERSION}" / f"{directory_module.cell_name(cell)}.json.gz"
        publish.write_gzip_json(path, directory_module.shard(cell, entries, licenses))
    _write_json(out / "directory" / "latest.json", publish.latest(DIRECTORY_VERSION, ghsl_stamp))
    print(f"      分片 {len(shard_cities)} 个", flush=True)


def cmd_build(args) -> None:
    started_all = time.monotonic()
    ucdb = UCDB(Path(args.ucdb))
    out = ROOT / "out"
    generated_at = dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M UTC")
    ghsl_stamp = {"ghsl": Path(args.ucdb).name, "pipeline": "app/tools/CityAtlas"}

    previous_ids = _published_city_ids(out)
    if not args.only:
        # 完整 build 从空的 out/ 开始：留着上一轮的文件，删掉一座城之后它的包与分片
        # 还会跟着 rsync 上 CDN，continuity 里的 retired 也永远不会出现。
        for stale in (out / "directory", out / "package"):
            shutil.rmtree(stale, ignore_errors=True)

    registry = publish.load_registry(ROOT / "registry.json")
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
                                  "assignedInDirectoryVersion": DIRECTORY_VERSION})
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
                 "mapDataVersion": MAP_DATA_VERSION, "center": package["center"],
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

    licenses = publish.license_block(ghsl_stamp)
    shards = []
    for cell, cities in sorted(shard_cities.items()):
        path = out / "directory" / f"v{DIRECTORY_VERSION}" / f"{directory_module.cell_name(cell)}.json.gz"
        publish.write_gzip_json(path, directory_module.shard(cell, cities, licenses))
        shards.append({"cell": f"{cell[0]}, {cell[1]}",
                       "cities": "、".join(city["name"] for city in sorted(cities, key=lambda c: c["cityID"])),
                       "path": f"directory/v{DIRECTORY_VERSION}/{directory_module.cell_name(cell)}.json.gz",
                       "size": human(path.stat().st_size), "bytes": path.stat().st_size})

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

    country = sub.add_parser("country")
    country.add_argument("--country", required=True, help="GADM 国名，如 China")
    country.add_argument("--pbf", required=True, help="Geofabrik 的区域 .osm.pbf")
    country.add_argument("--ucdb", required=True, help="GHS_UCDB_GLOBE_R2024A.gpkg 的路径")
    country.add_argument("--limit", type=int, default=0, help="只做前 N 座，用来先跑通")
    country.set_defaults(handler=cmd_country)

    compare = sub.add_parser("compare")
    compare.add_argument("slug", help="reference/cities/ 里有数据的城（shanghai 或 hangzhou）")
    compare.add_argument("--style", default="plain")
    compare.set_defaults(handler=cmd_compare)

    args = parser.parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
