"""发布物：`cityID` 名册、`continuity.json`、`latest.json`、索引页与许可通知。

许可这一节是义务，不是文案：分片与地图包都是从 OSM 裁剪简化出来的衍生数据库，
ODbL §§4.2–4.7 要求每份副本带许可与来源通知，并向接收者提供衍生数据库或它的生成方法。
CDN 上不加鉴权的 URL 本身就是「提供副本」，索引页把全部 URL 与这套脚本的位置列出来，
补上「生成方法」那一半。城市画框还用到 GHSL UCDB（CC BY 4.0，(c) European Union），
署名义务是第二条，与 ODbL 并列——PRD 第 6 节「许可义务」只写了 ODbL 一条，这里补上。
"""

from __future__ import annotations

import gzip
import io
import json
import shutil
from pathlib import Path

LICENSES = {
    # 署名义务跟着数据源走：要素层来自 Overture 的 parquet，而 Overture 的
    # divisions / transportation / base 三个主题都是 OSM 的再打包（每条记录的
    # `sources` 里写着 `provider=osm`、`license=ODbL-1.0`），它要求的署名是两家并列。
    # 行政边界仍然直接读区域 pbf，所以 OpenStreetMap 那半在这条线换血之前之后都在。
    "osm": {
        "source": "OpenStreetMap contributors, Overture Maps Foundation",
        "license": "ODbL v1.0",
        "uri": "https://opendatacommons.org/licenses/odbl/1-0/",
        "attribution": "© OpenStreetMap contributors, Overture Maps Foundation",
    },
    "ghsl": {
        "source": "GHSL Urban Centre Database R2024A, European Commission JRC",
        "license": "CC BY 4.0",
        "uri": "https://creativecommons.org/licenses/by/4.0/",
        "attribution": "© European Union, 1995-2026",
        "applies_to": "城市画框（frame）",
    },
}


def license_block(source_stamp: dict) -> dict:
    return {**LICENSES, "generatedFrom": source_stamp}


def write_gzip_json(path: Path, payload) -> int:
    """确定性 gzip：mtime 写 0、键排序固定，否则同一份内容每次跑出来的字节都不同。
    返回压缩前的字节数。分片、地图包、夹具都走这一个。"""
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", compresslevel=9, mtime=0) as handle:
        handle.write(blob)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(buffer.getvalue())
    return len(blob)


def load_registry(path: Path) -> dict:
    """`cityID` 名册。产品自分配、永不复用、不从任何外部对象 ID 派生——
    名册就是这句话的载体：一座城第一次进目录时在这里拿到号，此后只读。"""
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"nextSerial": 1, "cities": {}}


def assign(registry: dict, osm_relation: int, note: dict) -> str:
    """名册的键是 OSM 关系 id，不是 `cities.json` 里的 slug。

    slug 是人手写的字符串，改一个字（`portland` → `portland-or`）就会静默派出一个新号，
    已经归属到旧号的散步全部变成孤儿。用外部 id 来**认出是同一座城**不违背
    「`cityID` 不从外部对象 ID 派生」——派生的是号，认人的是键，两回事。
    """
    key = str(osm_relation)
    existing = registry["cities"].get(key)
    if existing is not None:
        # 号是不动的，但 `country` 每轮刷新：它不是这座城的身份，是「上一次是谁出的包」，
        # 增量出包靠它认出「这一国这次没再产出的城」（见 `__main__._emit`）。
        if "country" in note:
            existing["country"] = note["country"]
        return existing["cityID"]
    city_id = "c%05d" % registry["nextSerial"]
    registry["nextSerial"] += 1
    registry["cities"][key] = {"cityID": city_id, **note}
    return city_id


def save_registry(registry: dict, path: Path) -> None:
    path.write_text(json.dumps(registry, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# `continuity.json` 里 status 的取值。PRD 第 6 节列了五个，这条管线只产出得了三个：
# `renamed` 要求 continuity 记名字（它不记）、`merged` / `split` 要求人工判定两版城市的对应关系。
# 本版全球目录还没建起来，这三种也无从产生；等真正做目录修订时再补，别现在留空壳。
CONTINUITY_STATUSES = ("unchanged", "added", "retired")


def continuity(previous_ids: set[str], current_ids: set[str],
               previous_version: int | None, directory_version: int) -> dict:
    """旧版每个 `cityID` 到新版的关系。首版（没有上一版）全是 `unchanged`。

    本版 App 不消费这份文件——它存在是为了让「目录升级不改写已确认的归属」这句话
    有一份可核对的记录，重划历史归属是独立迁移的事。
    """
    first = previous_version is None
    relations = {city_id: {"status": "unchanged", "to": [city_id]}
                 for city_id in sorted(previous_ids & current_ids)}
    for city_id in sorted(previous_ids - current_ids):
        relations[city_id] = {"status": "retired", "to": []}
    for city_id in sorted(current_ids - previous_ids):
        relations[city_id] = {"status": "unchanged" if first else "added", "to": [city_id]}
    return {
        "directoryVersion": directory_version,
        "previousDirectoryVersion": previous_version,
        "cities": relations,
    }


# 目录留几代。两代不是「保险起见多留一代」：`_write_shards` 升版本那一轮要从上一代
# 读别国的城（升 v3 时中国重跑、日本的 1739 座得跟过来），只留一代就没有那份可读的了。
KEEP_DIRECTORY_GENERATIONS = 2


def prune_directories(out: Path, directory_version: int, *, keep: int = KEEP_DIRECTORY_GENERATIONS) -> list[int]:
    """新一代分片写完之后，`v{N-keep}` 及更早的整代删掉。返回删掉的版本号。

    **这条规则只写给 `directory/`，不写给 `package/`。** 目录分片能删是因为
    `latest.json` 每次启动都问网络，旧代只服务持续离线的用户；而地图包取哪一版由
    **每台设备自己落库的号**决定，可以长期停在旧号上——没升级的 App 会一直请求
    `1.json.gz`（换血文档第八节那张表），删掉旧包就是让那批人的城市图当场空掉。
    两件事的删除规则不是一回事，别混。

    留着不删的代价是文件数：一代全球分片一千多个，而单个 Cloudflare Worker
    有两万文件的上限。
    """
    removed = []
    for path in sorted((out / "directory").glob("v*")):
        if not path.is_dir() or not path.name[1:].isdigit():
            continue
        version = int(path.name[1:])
        if version <= directory_version - keep:
            shutil.rmtree(path)
            removed.append(version)
    return removed


def latest(directory_version: int, source_stamp: dict) -> dict:
    return {"directoryVersion": directory_version, "generatedFrom": source_stamp}


INDEX_TEMPLATE = """<!doctype html>
<meta charset="utf-8">
<title>此间 · 城市图静态资源</title>
<style>
 body {{ font: 15px/1.7 -apple-system, "PingFang SC", sans-serif; max-width: 46em; margin: 5vh auto; padding: 0 1.5em; color: #2E3A4D; background: #FDFCFA; }}
 h1 {{ font-size: 1.5em; margin-bottom: .2em; }}
 h2 {{ font-size: 1.05em; margin-top: 2.2em; }}
 table {{ border-collapse: collapse; width: 100%; font-size: .92em; }}
 th, td {{ text-align: left; padding: .35em .6em .35em 0; border-bottom: 1px solid #E7E4DD; }}
 code {{ font-family: ui-monospace, Menlo, monospace; font-size: .92em; }}
 p.note {{ color: #6B7280; }}
</style>
<h1>此间 · 城市图静态资源</h1>
<p class="note">目录版本 v{directory_version}，生成于 {generated_at}。<br>
这些文件是从 OpenStreetMap 数据裁剪、简化得到的衍生数据库，按 ODbL 发布；下面列出全部文件与生成方法。<br>
道路、铁路、水系、绿地与海面取自 Overture Maps 对 OSM 的再打包，行政边界与市中心点直接读 OSM 区域包。</p>

<h2>许可与来源</h2>
<ul>
<li>地图数据 © OpenStreetMap contributors、Overture Maps Foundation，按 <a href="https://opendatacommons.org/licenses/odbl/1-0/">ODbL v1.0</a> 提供。源数据快照：{osm_stamp}。</li>
<li>城市画框依据 <a href="https://human-settlement.emergency.copernicus.eu/ghs_ucdb_2024.php">GHSL Urban Centre Database R2024A</a>，© European Union，按 <a href="https://creativecommons.org/licenses/by/4.0/">CC BY 4.0</a> 提供。</li>
<li>生成这些文件的全部脚本：<a href="{pipeline_url}"><code>{pipeline_url}</code></a>（这份产物旁边的副本）。</li>
</ul>

<h2>目录分片</h2>
<p class="note">按 1°×1° 的格切开，路径里的两个数是格的西南角整数纬度与经度。</p>
<table><tr><th>格</th><th>城市</th><th>文件</th><th>体积</th></tr>
{shard_rows}
</table>

<h2>地图包</h2>
<table><tr><th>城市</th><th>cityID</th><th>画框</th><th>文件</th><th>体积</th></tr>
{package_rows}
</table>

<h2>版本文件</h2>
<table><tr><th>文件</th><th>是什么</th></tr>
<tr><td><code>directory/latest.json</code></td><td>当前目录版本号</td></tr>
<tr><td><code>directory/v{directory_version}/continuity.json</code></td><td>旧版 cityID 到新版的关系</td></tr>
</table>
"""


def index_page(*, directory_version: int, generated_at: str, osm_stamp: str, pipeline_url: str,
               shards: list[dict], packages: list[dict]) -> str:
    shard_rows = "\n".join(
        "<tr><td>{cell}</td><td>{cities}</td><td><code>{path}</code></td><td>{size}</td></tr>".format(**row)
        for row in shards)
    package_rows = "\n".join(
        "<tr><td>{name}</td><td><code>{cityID}</code></td><td>{frame}</td>"
        "<td><code>{path}</code></td><td>{size}</td></tr>".format(**row)
        for row in packages)
    return INDEX_TEMPLATE.format(
        directory_version=directory_version, generated_at=generated_at, osm_stamp=osm_stamp,
        pipeline_url=pipeline_url, shard_rows=shard_rows, package_rows=package_rows)
