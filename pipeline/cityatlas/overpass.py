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

    payload = urllib.parse.urlencode({"data": query}).encode("utf-8")
    last_error: Exception | None = None
    for attempt in range(3):
        for endpoint in ENDPOINTS:
            _pause()
            request = urllib.request.Request(endpoint, data=payload, headers={"User-Agent": _USER_AGENT})
            try:
                with urllib.request.urlopen(request, timeout=900) as response:
                    body = response.read()
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                last_error = error
                continue
            data = json.loads(body)
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
