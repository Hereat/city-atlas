"""OSM 快照：向 Overpass 要一次，落盘成 gzip JSON，之后所有生成都读这份快照。

**为什么输入不是 Geofabrik 的 `.osm.pbf`**（PRD 第 6 节这么写的）：管线要的是「画框内的
道路水系绿地」和「这座城的行政边界」，两者都是几平方公里到几百平方公里的局部查询。
Geofabrik 的最小分区（如 china-latest）是 1.5 GB 的 PBF，解析它需要 protobuf 与一套
索引，换来的却是同一批要素。Overpass 的 bbox 查询直接给出这批要素，
且**快照一旦落盘就是这条管线唯一的输入**——重跑读的是文件，不是网络，
「同一输入重跑逐字节相同」这条验收因此成立。
城市数量涨到几百上千、Overpass 不再合适时，换的是产出这份快照的程序，不是下游任何一步。

Overpass 是志愿者运营的公共服务：这里串行发请求、每次之间歇 `_COURTESY_PAUSE` 秒、
命中缓存就不发。不要把它并行化。
"""

from __future__ import annotations

import gzip
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
_COURTESY_PAUSE = 3.0
# User-Agent 必须是纯 ASCII：http.client 用 latin-1 编码请求头。
_USER_AGENT = "hereat-cityatlas/1 (one-off static city atlas build)"

_last_request = 0.0


def fetch(query: str, cache: Path, *, refresh: bool = False) -> dict:
    """跑一条 Overpass QL，结果 gzip 落到 `cache`。缓存命中就不联网。

    快照里带着产生它的那条查询（`__query__`）：缓存的键是文件名，而查询会随画框改变——
    比对一次，画框变了就重抓。真正的保险是 `build` 里那道覆盖检查：
    快照必须盖住画框，无论它是怎么来的。老快照没有这一列，按「没变」处理，交给那道检查。
    """
    if cache.exists() and not refresh:
        with gzip.open(cache, "rt", encoding="utf-8") as handle:
            cached = json.load(handle)
        if cached.get("__query__", query) == query:
            return cached
        print(f"{cache.name}：查询变了，重新抓")

    data = json.loads(_request(query))
    if "remark" in data:
        # 超时的 Overpass 返回 200、合法 JSON、以及**超时前收到的那部分要素**。
        # 落盘就等于把半份数据缓存成了「输入」，而 `mappack.covers` 拦不住它：
        # 半份要素通常照样铺满整个 bbox。这是整条管线唯一的联网入口，在这里拦住。
        raise SystemExit(f"Overpass 只给了半份数据：{data['remark']}")
    data["__query__"] = query
    cache.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(cache, "wt", encoding="utf-8", compresslevel=6) as handle:
        json.dump(data, handle, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return data


def fetch_xml(query: str, cache: Path) -> Path:
    """跑一条 `[out:xml]` 查询，原样落到 `cache`，osmium 直接读得动。

    与 `fetch` 的分别只在格式：那边把 JSON 交给下游自己解析，这边把 OSM XML 交给
    osmium（`pbf` 用它补行政边界被区域文件裁掉的成员）。礼貌暂停、多端点、三轮重试、
    半份数据的拦截四件都是 `_request` 里同一套，只有 `remark` 不同——
    XML 里它是一个元素而不是一个键。

    缓存不比对查询（XML 里没地方塞它）。**所以文件名必须说出这份数据是按什么要的**，
    调用方把范围的指纹写进文件名，理由与 `pbf` 那边的层级一样。

    **只走主端点，不回退到镜像。** 补边界成员是分几趟取的，而镜像可能落后好几个月
    （2026-09 实测过一次）：两趟拿到不同代的数据，同一个节点就会有两个版本，
    `osmium export` 当场拒绝整份文件；更坏的是落后那一代的成员表指向已经删掉的节点，
    于是环照样不闭合——而那是**静默**失败，正是这一步要修的那个形状。
    要素那条路（`fetch`）没有这个问题：它一次查一个画框，一趟就是一份完整快照。
    """
    if cache.exists():
        return cache
    # 按 id 直取是秒级的查询，120 秒没回音就是挂住了，重试比等着划算
    body = _request(query, endpoints=ENDPOINTS[:1], timeout=120)
    text = body.decode("utf-8", errors="replace")
    if "<remark>" in text:
        remark = text[text.index("<remark>") + 8:text.index("</remark>")]
        raise SystemExit(f"Overpass 只给了半份数据：{remark}")
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(body)
    return cache


def _request(query: str, endpoints: tuple[str, ...] = ENDPOINTS, timeout: int = 900) -> bytes:
    """发一次查询，端点轮着试、三轮重试，返回原始响应体。

    `timeout` 要配着查询的量级给：画框那种大查询本来就要算几分钟（默认 900），
    而按 id 直取是秒级的，给它 900 秒只意味着**一次挂死要等一刻钟**才轮到重试。
    """
    payload = urllib.parse.urlencode({"data": query}).encode("utf-8")
    last_error: Exception | None = None
    for attempt in range(3):
        for endpoint in endpoints:
            _pause()
            request = urllib.request.Request(endpoint, data=payload, headers={"User-Agent": _USER_AGENT})
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    return response.read()
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                last_error = error
        time.sleep(20 * (attempt + 1))
    raise SystemExit(f"Overpass 三轮都没要到数据：{last_error}")


def _pause() -> None:
    global _last_request
    wait = _COURTESY_PAUSE - (time.monotonic() - _last_request)
    if wait > 0:
        time.sleep(wait)
    _last_request = time.monotonic()


# ---------- 查询 ----------

def boundary_query(name: str, levels: tuple[int, ...], box) -> str:
    """按名字在一个经纬矩形里找行政边界关系。`box` 是 `(min_lon, min_lat, max_lon, max_lat)`。"""
    bbox = f"{box[1]},{box[0]},{box[3]},{box[2]}"
    levels_re = "|".join(str(level) for level in levels)
    return (
        "[out:json][timeout:600];\n"
        f'rel["boundary"="administrative"]["admin_level"~"^({levels_re})$"]'
        f'["name"~"^{name}$"]({bbox});\n'
        "out geom;"
    )


# 图层与 OSM 标签的对应表。四档道路的分档理由写在 `mappack.ROAD_TIERS`。
# 要素查询**由 `mappack` 的四张表推出来**，不另手写一份。
#
# 先前这里是一段写死的 Overpass QL，`pbf.KEEP` 里还有第三份同样的清单，注释写着
# 「两边必须一致」——一致靠人记就是迟早不一致。「我们画哪些要素」这件事的唯一出处
# 是 `mappack` 的 `ROAD_TIERS` / `RAILWAY_KINDS` / `WATER_WIDTHS` / `WATER_AREA_TAGS`
# / `GREEN_AREA_TAGS`，Overpass 这条路与 Overture 那条路都从它推。
#
# **岸线不在里面。** `coastline` 那一列的语义换了：它现在装的是闭合的海面环
# （管线裁到画框、摆正绕向），而 Overpass 给的是一地被裁碎的线段，两者不是一种东西。
# 这条路只服务 `cities.json` 里那七座样例城的对照，海面不是它要对照的东西。
_TUNNEL = '["tunnel"!~"."]'


def _any_of(values) -> str:
    return "^(" + "|".join(sorted(values)) + ")$"


def _by_key(pairs) -> dict:
    grouped: dict = {}
    for key, value in pairs:
        grouped.setdefault(key, set()).add(value)
    return grouped


def feature_query(box) -> str:
    from .mappack import (GREEN_AREA_TAGS, RAILWAY_KINDS, ROAD_TIERS,
                          WATER_AREA_TAGS, WATER_WIDTHS)

    lines = [f'way["highway"~"{_any_of(ROAD_TIERS)}"]({{bbox}});',
             f'way["railway"~"{_any_of(RAILWAY_KINDS)}"]{_TUNNEL}({{bbox}});',
             f'way["waterway"~"{_any_of(WATER_WIDTHS)}"]{_TUNNEL}({{bbox}});']
    for key, values in sorted(_by_key(WATER_AREA_TAGS + GREEN_AREA_TAGS).items()):
        for kind in ("way", "rel"):
            lines.append(f'{kind}["{key}"~"{_any_of(values)}"]({{bbox}});')
    query = "[out:json][timeout:900];\n(\n  " + "\n  ".join(lines) + "\n);\nout geom;"
    bbox = f"{box[1]},{box[0]},{box[3]},{box[2]}"
    return query.replace("{bbox}", bbox)
