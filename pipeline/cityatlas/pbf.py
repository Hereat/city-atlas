"""从区域 `.osm.pbf` 读**行政边界与市中心点**——一次下载一个国家，一趟读出上万座城。

要素（道路、铁路、水系、绿地、海面）不从这里来，走 `overture`：那边是一趟按国家包围盒
拉 parquet、用 SQL 按画框切，同一段从两小时降到十分钟出头。这里只剩边界与 place 点，
因为它们要的是**名字**——Overture 的 `divisions` 在日本只有罗马字（「糸満市」会变成
「Itoman」），而名字不只是展示层：中国的名单靠「市/区/县」的后缀筛，各国的市中心点靠
`spellings` 试多种写法认人，只给罗马字会让这两处同时失效。

两步，都靠 osmium：`admin_boundaries` 抽出市级行政边界，`city_centres` 抽出 `place` 节点。
两份产物的文件名都带着判据（要哪几级、认哪几种 place）——判据改了文件名没变，
会静默读旧的，少掉一整类而什么都不报。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .boundary import spellings

# 「市」在各国是第几级、叫什么。行政边界回答的是「这是哪座城」，而这个级别各国不同——
# **逐国一行，不是逐城一行**，所以它是一张小表而不是一份清单。
# 出处：OSM wiki 的 Tag:boundary=administrative，各国 admin_level 对照。
#
# `suffix` 不是可有可无的修饰：中国的 `admin_level=4` 是**省级**，省、自治区、直辖市
# 同在这一层，只有直辖市是城市；这一层还混着「苏皖界」这类省界线。按名字结尾筛「市」
# 一次把三件事都解决了（2026-09-06 实测：不筛会把广东省当成一座城）。
#
# `cover` 是这一国的**覆盖层**：铺满国土、一定收、不会被吞的那一层（`_select_cities`
# 的两条规则里的第一条）。其余层是候补层，只在自成一片建成区时才独立成城。
# **不能按级数大小推**——中国的覆盖层在 4/5、候补层在 6，日本两者同在 7。
#
# `require` 是「必须带的标签」，用来把**已经不存在的行政单位**挡在外面。OSM 里旧的
# 市镇村边界不会被删，只是不再更新；哪个标签能认出「现役」逐国不同，所以也在这张表里。
CITY_LEVELS = {
    # 中国：直辖市在 4（省与自治区同在这一层，靠 suffix 排除），地级市在 5，
    # 区 / 县 / 县级市 / 旗在 6——最后这一层不是「市」，但远郊那些与主城不相连的
    # 城镇就落在它上面（涪陵、慈溪、常熟），名单怎么从这三层合成见 `frame` 的
    # 「城市名单」那一节。
    "China": {"levels": (3, 4, 5, 6), "cover": (3, 4, 5),
              "suffix": ("市", "区", "县", "旗", "州", "盟"),
              # 按后缀收之后还得明确排掉两类，它们不是城：
              # * 省级的「自治区」——新疆、内蒙古、广西、宁夏，正好以「区」结尾；
              # * 地级的「地区」行政公署——喀什地区、大兴安岭地区，是一片区域不是一座城，
              #   它下辖的县市才是。
              # 自治州与盟不排除：它们与地级市平级、下辖县市，延边、湘西、锡林郭勒都是。
              "exclude": ("自治区", "地区"),
              # `names` 是**专名白名单**：`名字 → 只认这一级的那个关系`。
              #
              # 两个特别行政区的名字不带任何后缀（OSM 里 `name:zh-Hans` 就写「香港」
              # 「澳门」），后缀白名单把它们整个挡在名单外——2026-09-08 实测：港岛与
              # 九龙没有任何城包含，在中环散步判 `unmatched`，只有以「区」结尾的荃湾、
              # 元朗、大埔、离岛靠「自成一片建成区」那条捞了上来。澳门连一片都没有。
              #
              # 为什么不改成「4 级里排掉省与自治区，剩下的都收」：那一层的 59 个面里
              # 有 17 个无名碎面、两块飞地、「苏皖界」「渝川界」「王屋」这类界线，
              # 后缀白名单正是在挡它们（2026-09-06 就为「广东省」栽过一次）。
              # 特别行政区只有两个，是**有限的专名**，穷举比造规则更贴事实。
              #
              # 带上级数是因为**香港在 OSM 里有两个关系**：3 级（特别行政区主体，与澳门
              # 同层）和 4 级（与省、直辖市同层）各一个，边界几乎重合。只按名字收会得到
              # 两座同名同地的「香港」，城市列表里并排出现两张卡。3 级那个是 SAR 主体，
              # 澳门只有这一级，所以两个专名都钉在 3。
              "names": {"香港": 3, "澳门": 3},
              # `region_suffix` 是**名字属于一片区域、不属于一座城的覆盖层单位**。
              # 它们照收进名单兜底，但**不吞候补层**（见 `frame.drop_swallowed`）。
              #
              # 自治州与盟的名字说的是一片区域：人说「我在西昌」，不说「我在凉山」。
              # 而它们的「主城」恰恰就在驻地那座城，于是按一般规则州府被自己的州吞掉——
              # 凉山彝族自治州吞掉西昌市，在西昌散步的城市卡写着「凉山彝族自治州」；
              # 延边州那格里图们、龙井、和龙、安图都在，唯独没有州府延吉市。
              # 一共 33 个这样的（30 个自治州 + 3 个盟）。
              #
              # 地级市不适用这条：地级市的名字就是它市区的名字，婺城区就是金华市区，
              # 该叫金华。喀什市早就是对的，只因为「喀什地区」被 exclude 排掉、没人吞它——
              # 同样的地理事实不该因为上级叫「地区」还是叫「州」得两套结果。
              #
              # 为什么不干脆把州从覆盖层里降下去：塔什库尔干那种州域深处的县城
              # 只有州盖得住，降级会直接出覆盖空洞（散步判 unmatched）。
              "region_suffix": ("自治州", "盟")},
    # 日本：市町村都在 7，东京的 23 特别区也在这一层（2026-09-07 实测，1979 个面里
    # 市 791、町 780、村 302、区 23，另有 82 个只有 admin_level 没有名字的碎面）。
    # 上下两层都不是城：6 是「郡」（370 个，一堆町村的合称），8 是政令市的行政区
    # （横浜市港北区那种），5 是北海道的振興局。
    #
    # **覆盖层就是这一层本身**，没有更粗的一层可用：上面是都道府県，那是省级
    # （神奈川県 ≠ 横滨）。市町村铺满国土，正好担「任何一次散步都答得出地名」这件事；
    # 代价是名单里 58% 是町村，多数没有 GHSL 建成区、画框走 OSM 市中心那条退路。
    # 候补层因此是空的——8 层的行政区全在某个市之内，独立不出来。
    #
    # `require` 挡的是**平成大合并前的旧町村**：OSM 里那些边界原样留着，也是
    # `admin_level=7`，名单里会多出 157 个已经不存在的单位（香南町、国分寺町、庵治町
    # 早在 2006 年并进高松市，圆座村、由佐村 更早）。它们与现役的市重叠，一次散步会
    # 落进两座城。判据是 `ref`——日本每个现役市町村都带全国地方公共団体コード，
    # 旧的一个都没有（2026-09-07 实测：带 ref 的 1739 个，对上官方的 1741；
    # 102 个旧单位另有 `historic:place`，剩下的只能靠 ref 认）。
    # 千叶那块「所属未定地」也没有 ref，顺带被这一条挡掉，不用再单列 exclude。
    "Japan": {"levels": (7,), "cover": (7,), "require": ("ref",)},
    "United States": {"levels": (8,), "cover": (8,)},        # city / town
    "Netherlands": {"levels": (8,), "cover": (8,)},          # gemeente
    "Austria": {"levels": (8,), "cover": (8,)},              # Gemeinde
}

# 认「城市中心」的 `place` 值，按大小排——挑的时候先按这个次序。
#
# `village` 是 2026-09-07 日本这一轮加的：日本的町村多数没有 city / town 节点，
# 293 座小城因此拿不到市中心，画框退到「行政区外接框的中心 + 2.4 km」，
# 而那个中心常常在山里（小笠原村的外接框横跨一千八百公里，中心在海面上）。
# 全日本只有 196 个 `place=village` 节点，其中 172 个正好落在这批城里——
# 这一类在日本几乎是专为町村用的，不是「把村都当成城」。
PLACE_KINDS = ("city", "town", "village")


def run(*args: str) -> None:
    subprocess.run([str(a) for a in args], check=True)


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
    # 过滤过的那份关系文件要**先保证在**，再看 geojsonseq 有没有缓存：
    # `admin_centres` 也要读它（取驻地成员），而先前这一步在 geojsonseq 命中缓存时会
    # 被跳过，于是日本那轮手上只有 `admin-7.geojsonseq`、没有 `admin-7.pbf`。
    # 已经在的话这两行不花时间。
    filtered = out.with_suffix(".pbf")
    if not filtered.exists():
        run("osmium", "tags-filter", source, "r/boundary=administrative", "-o", filtered, "--overwrite")
    if out.exists():
        return out
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
    """区域文件里的城市中心点：`PLACE_KINDS` 那几种 `place` 节点。

    画框的**几何中心不是城市中心**：成都的建成区往南铺得远（天府新区），重庆的主城被
    江切成几块，外接框的中心都会偏出真正的市中心（2026-09-07 用户在图上指出——
    渝中半岛跑到了右下角）。OSM 里有现成的答案，逐城可查、全国都有
    （中国 `place=city` 3176 个、`place=town` 32426 个），不需要逐城手调。

    返回 `{名字: [(lon, lat, 等级), …]}`，等级就是 `PLACE_KINDS` 里的次序：同名的镇很多，
    挑的时候先按等级、再按在不在这座城的界内。

    **`out` 的文件名要带上 `PLACE_KINDS`**，理由同 `admin_boundaries` 那边的层级：
    改了要哪几种还读旧缓存，会静默少掉一整类。
    """
    if not out.exists():
        filtered = out.with_suffix(".pbf")
        if not filtered.exists():
            run("osmium", "tags-filter", source,
                *[f"n/place={kind}" for kind in PLACE_KINDS], "-o", filtered, "--overwrite")
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
        coordinates = (feature.get("geometry") or {}).get("coordinates")
        names = spellings(tags)
        if not names or not coordinates:
            continue
        rank = PLACE_KINDS.index(tags.get("place")) if tags.get("place") in PLACE_KINDS else len(PLACE_KINDS)
        for name in names:
            places.setdefault(name, []).append((coordinates[0], coordinates[1], rank))
    return places


def admin_centres(admin_pbf: Path, out: Path) -> dict:
    """行政边界关系上的 `admin_centre` 成员：`{关系 id: (lon, lat)}`，也就是**驻地**。

    键是**字符串**，与 `osm_id_of` / `read_boundaries` 给出的那一种对齐（名册也用它认人）。
    用 int 做键会静默取不到——查得到的东西一个都对不上，而且一声不吭。

    **为什么要它**：`centre_for` 按城**自己的名字**去认 `place` 节点，而「甘孜藏族自治州」
    不是任何 place 节点的名字（驻地叫康定市）。认不到市中心，画框就一路退到最后那条
    「州外接框中心」——在四万平方公里的州中心开一个 2.4 × 3 km 的窗，落在草原上，
    包里一条路都没有（2026-09-09 实测 7 座这样的空包）。驻地在 OSM 的关系上现成挂着，
    这一步只是把它取出来。README 那句「画框的中心取 `admin_centre`」写的就是它，
    `country` 这条路先前漏了。

    **不能用「界内最大的 place 节点」代替。** 中国的县城在 OSM 里全都标成 `place=city`，
    同级之下无论按距离还是按名字挑，都是随机挑一个县：实测那 7 座里 5 座会取错
    （甘孜州取到新龙县而不是康定市，阿坝州取到红原县而不是马尔康市）。
    驻地是一个事实，不是一个估计，所以只认关系上写着的那个。

    两趟 osmium：先只读关系拿到 `{关系: 节点}`（关系行里带成员，一趟几秒），
    再把那些节点 id 交给 `getid` 取座标（几千个点，一趟几秒）。不整份读节点——
    那份过滤过的 pbf 里有七百万个节点，全读进内存要一个 G。
    """
    if out.exists():
        return {key: tuple(value) for key, value in
                json.loads(out.read_text(encoding="utf-8")).items()}

    seats: dict[str, int] = {}
    relations = subprocess.run(["osmium", "cat", "-f", "opl", "--object-type=relation",
                                str(admin_pbf)], check=True, capture_output=True, text=True).stdout
    for line in relations.splitlines():
        for field in line.split(" "):
            if not field.startswith("M"):
                continue
            for member in field[1:].split(","):
                if member.endswith("@admin_centre") and member.startswith("n"):
                    seats[line.split(" ", 1)[0][1:]] = int(member[1:].split("@")[0])
            break

    if not seats:
        out.write_text("{}", encoding="utf-8")
        return {}

    ids = out.with_suffix(".ids")
    ids.write_text("".join(f"n{node}\n" for node in sorted(set(seats.values()))), encoding="utf-8")
    # `getid` 没找全就返回 1，而没找全是常态：那份过滤过的 pbf 只带着关系用得上的成员，
    # 有些驻地节点不在里面（实测 19006 个里少 331 个）。1 照旧往下走，只用找到的那些；
    # 2 及以上是真出错（参数写错、文件读不了），照样炸。
    found = subprocess.run(["osmium", "getid", "-i", str(ids), "-f", "opl", str(admin_pbf)],
                           capture_output=True, text=True)
    if found.returncode > 1:
        raise SystemExit(f"osmium getid 失败（{found.returncode}）：{found.stderr.strip()}")
    nodes = found.stdout
    points: dict[int, tuple[float, float]] = {}
    for line in nodes.splitlines():
        if not line.startswith("n"):
            continue
        fields = dict((field[0], field[1:]) for field in line.split(" ") if field)
        if "x" in fields and "y" in fields and fields["x"]:
            points[int(line.split(" ", 1)[0][1:])] = (float(fields["x"]), float(fields["y"]))

    centres = {relation: points[node] for relation, node in seats.items() if node in points}
    out.write_text(json.dumps({key: list(value) for key, value in centres.items()},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    return centres


def centre_for(names, boundary, places: dict):
    """这座城的市中心。`names` 是这座城的几种写法（`boundary.spellings`），
    逐一去认同名的点（「成都市」也认「成都」），都不在界内就返回 None。"""
    candidates = []
    for name in names:
        candidates += places.get(name, []) + places.get(name.rstrip("市"), [])
    inside = [c for c in candidates if boundary.contains((c[0], c[1]))]
    if not inside:
        return None
    best = min(inside, key=lambda c: c[2])
    return best[0], best[1]
