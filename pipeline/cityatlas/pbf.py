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

import hashlib
import json
import subprocess
from pathlib import Path

from . import overpass
from .boundary import chinese_name, spellings

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
              # 按后缀收之后还得明确排掉三类，它们不是城：
              # * 省级的「自治区」——新疆、内蒙古、广西、宁夏，正好以「区」结尾；
              # * 地级的「地区」行政公署——喀什地区、大兴安岭地区，是一片区域不是一座城，
              #   它下辖的县市才是；
              # * 「综合实验区」——省直管的功能区管理机构，与「地区」同类：全国只有平潭
              #   一个，它与自己下辖的平潭县地盘完全重合，两个都收就是同地两张卡，
              #   而且两者都落在福州市界内（平潭县的区划代码 350128 仍挂在福州下），
              #   闸门 2「同级不重叠」因此报红。收下辖的平潭县，人说的也是「我在平潭」。
              # 「新区」不在此列：浦东、滨海、雄安、两江、沈北都是正经的行政区。
              # 自治州与盟不排除：它们与地级市平级、下辖县市，延边、湘西、锡林郭勒都是。
              "exclude": ("自治区", "地区", "综合实验区"),
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
              "region_suffix": ("自治州", "盟"),
              # 闸门的容忍值（`gates.py`）。阿坝藏族羌族自治州与草湖项目区的包是空白：
              # OSM 里没写它们的驻地，画框只能退到州域的外接框中心，落在没有城区的地方。
              # 2026-09-09 已拍板留着不动（对症的是画框规则，不是从名单里删人），
              # 所以这两座是**已知且认过**的，不该每轮再拦一次。
              "gates": {"empty_packages": 2}},
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


def run_to(target: Path, *args: str) -> None:
    """跑一条 osmium，产物先落到临时名、成功后才改成正式名。

    中途失败（网络断、磁盘满、Ctrl-C）留下的半份文件若占着正式名字，下一趟就会把它
    当成缓存。这条管线为「静默的半份数据」栽过一次（见 `overpass.fetch` 里那段注释），
    这里不给它第二次机会。
    """
    # 临时名把 `.part` 插在后缀**之前**：osmium 靠后缀认格式，`.pbf.part` 它认不出来
    tmp = target.with_name(f"{target.stem}.part{target.suffix}")
    run(*args, "-o", tmp, "--overwrite")
    tmp.replace(target)


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


def admin_boundaries(source: Path, out: Path, rule: dict) -> tuple[Path, set[str]]:
    """把整个区域里的市级行政边界抽成一份 GeoJSONSeq。

    **这一步取代了逐城手写的 `cities.json`。** 手写清单存在的唯一理由是 Overpass 要
    逐城查询；区域文件在手上，边界就是一次筛选的事——城市名单与边界一起出来了。

    产物三份，`out` 只当基名用，真正的名字各自说出自己是什么：

    * `<基名>.pbf`——按 `boundary=administrative` 筛过的关系（连成员）。
    * `<基名>-full-<指纹>.pbf`——成员从上游补齐之后的那份。`admin_centres` 读它。
    * `<基名>-full-<指纹>.geojsonseq`——面。这一份是返回值。

    **指纹是「补齐范围」的指纹**，不是内容的：名单规则一改，该补的成员就跟着变，
    而缓存必须当场失效。理由与文件名带层级那条一样（见模块开头）。

    第二个返回值是**边界补过成员的那些关系**。区域文件按国界裁，所以「边界在这份文件里
    本来就完整」等价于「主体在这个国家境内」——名单那头拿它把邻国的单位挡在外面
    （`__main__._select_cities` 的兜底那条）。
    """
    filtered = out.with_suffix(".pbf")
    if not filtered.exists():
        run_to(filtered, "osmium", "tags-filter", source, "r/boundary=administrative")

    missing = missing_city_members(filtered, rule)
    # 排序方式是**缓存键的一部分**（指纹按这个序列算，批次也按它切）。换一种排法，
    # 指纹与每一批的内容都会变，已经取回来的几十万个对象当场全部失效，得重下一遍。
    ids = sorted({way for ways in missing.values() for way in ways})
    digest = hashlib.sha1("\n".join(ids).encode("utf-8")).hexdigest()[:8]
    whole = out.with_name(f"{out.stem}-full-{digest}.pbf")
    if not whole.exists():
        _patch_members(filtered, whole, out.with_name(f"{out.stem}-patch-{digest}"), ids)

    final = out.with_name(f"{out.stem}-full-{digest}.geojsonseq")
    if final.exists():
        return final, set(missing)
    levels_set = {str(level) for level in rule["levels"]}
    staging = final.with_name(f"{final.stem}.part{final.suffix}")
    with staging.open("w", encoding="utf-8") as sink:
        process = subprocess.Popen(
            # -u type_id：给每个 feature 一个 `@id`（形如 `a1826135`）。**不能省**——
            # 没有它，每座城的身份都是 None，名册会把全国挤成一条（2026-09-06 实测）
            ["osmium", "export", str(whole), "-f", "geojsonseq",
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
        if process.wait() != 0:
            raise SystemExit(f"osmium export 失败（{process.returncode}）")
    staging.replace(final)
    return final, set(missing)


def city_name(tags: dict, rule: dict) -> str | None:
    """这个行政单位按名单规则该叫什么；不该进名单就返回 None。

    名单规则**只此一份**。`country` 读边界时按它筛，`admin_boundaries` 决定「哪些关系
    值得把裁掉的成员补回来」时也按它筛——两处各写一遍，补齐的范围就会与名单的范围
    悄悄错开，而错开的那几座城什么都不报，只是不在名单里。
    """
    level = int(tags.get("admin_level") or 0)
    if level not in rule["levels"]:
        return None
    name = chinese_name(tags) or tags.get("name")
    if not name:
        return None
    # 专名白名单先过：特别行政区按后缀筛不出来，而同名的关系可能不止一个
    # （香港在 3 级与 4 级各有一份），所以这条白名单连级数一起认（`CITY_LEVELS`）
    names = rule.get("names") or {}
    if name in names:
        if level != names[name]:
            return None
    else:
        suffix = rule.get("suffix")
        if suffix and not name.endswith(tuple(suffix)):
            return None
        exclude = rule.get("exclude") or ()
        if exclude and name.endswith(tuple(exclude)):
            return None
    if any(key not in tags for key in rule.get("require") or ()):
        return None
    return name


# 组成面要靠这两个角色的成员；`subarea` 缺了不影响面，`label` 是个点。
_RING_ROLES = frozenset({"outer", "inner", ""})
# 一批取多少个对象。骨架与节点都**按 id 直取**，那是索引查询：3 万个一批实测 6.4 秒、
# 1.7 MB。递归（`>`）则贵得多——3899 条 way 一次递归出 30 万个节点，公共端点直接 504。
# Overpass 是志愿者运营的服务，这里串行、每批之间歇着（`overpass._pause`）。
_PATCH_BATCH = 30000


def missing_city_members(filtered: Path, rule: dict) -> dict[str, list[str]]:
    """这份关系文件里，**本该成城却组不成面**的关系各缺哪些成员 way。

    区域文件按陆地裁。福建沿海那一排市把 12 海里领海基线画进了自己的行政边界
    （台海那一带标得格外细，马祖一带的限制水域还被挖成内环），那些成员 way 落在裁切
    范围外。**osmium 组不成环就静默跳过整个关系**——`export -e` 一条错误都不报——
    于是福州、厦门、泉州、莆田、漳州、宁德、平潭从第一代目录起就不在名单里，
    而线上看不出任何异常（2026-09-12 查实根因，13 条补齐后 osmium 当场组得出面）。

    **这不是福建特有的性质**：凡是把领海或跨境段画进边界的地方都会这样，所以判据是
    「缺了担环的成员」，不是「在福建」。
    """
    present: set[str] = set()
    process = subprocess.Popen(["osmium", "cat", "-f", "opl", "--object-type=way", str(filtered)],
                               stdout=subprocess.PIPE, text=True)
    for line in process.stdout:
        if line.startswith("w"):
            present.add(line[1:line.index(" ")])
    if process.wait() != 0:
        raise SystemExit("osmium cat 读不出 way")

    missing: dict[str, list[str]] = {}
    for osm_id, tags, members in _relations(filtered):
        if city_name(tags, rule) is None:
            continue
        lost = sorted({ref for kind, ref, role in members
                       if kind == "w" and role in _RING_ROLES and ref not in present}, key=int)
        if lost:
            missing[osm_id] = lost
    return missing


def _relations(pbf_path: Path):
    """`osmium cat -f opl` 的关系行 → `(id, tags, members)`，一趟几秒。

    整份读进来的是关系，不是节点：那份过滤过的 pbf 里有七百万个节点。
    """
    process = subprocess.Popen(["osmium", "cat", "-f", "opl", "--object-type=relation", str(pbf_path)],
                               stdout=subprocess.PIPE, text=True)
    for line in process.stdout:
        if not line.startswith("r"):
            continue
        fields = line.rstrip("\n").split(" ")
        tags: dict[str, str] = {}
        members: list[tuple[str, str, str]] = []
        for field in fields[1:]:
            if field.startswith("T") and len(field) > 1:
                for pair in field[1:].split(","):
                    if "=" in pair:
                        key, value = pair.split("=", 1)
                        tags[key] = _opl_unescape(value)
            elif field.startswith("M") and len(field) > 1:
                for member in field[1:].split(","):
                    if "@" in member:
                        ref, role = member.split("@", 1)
                        members.append((ref[0], ref[1:], role))
        yield fields[0][1:], tags, members
    if process.wait() != 0:
        raise SystemExit("osmium cat 读不出关系")


def _opl_unescape(value: str) -> str:
    """OPL 把非 ASCII 与分隔符写成 `%十六进制%`（「福州市」是 `%798f%%5dde%%5e02%`）。

    不还原就认不出任何一个中文名字，而名字正是名单的判据。
    """
    out, index = [], 0
    while index < len(value):
        if value[index] == "%":
            end = value.find("%", index + 1)
            if end > index + 1:
                out.append(chr(int(value[index + 1:end], 16)))
                index = end + 1
                continue
        out.append(value[index])
        index += 1
    return "".join(out)


def _patch_members(filtered: Path, whole: Path, base: Path, ids: list[str]) -> None:
    """把 `ids` 这些 way（连它们的节点）从上游取回来，合进关系文件。

    为什么补而不是换一份更大的区域文件：`asia-latest` 有 12 GB，而且**未必**带着这些
    way——它也是按框裁的，只是框更大；更要紧的是它会把邻国的行政关系带得更全，
    而名单靠名字后缀筛，「阿拉木图州」「高雄市」都以白名单里的字结尾。
    缺什么取什么是唯一不牵动别处的那条路（四条路的比较见城市图实施文档）。
    """
    if not ids:
        run_to(whole, "osmium", "cat", filtered)
        return

    # **分两趟取，而且每一趟都只取「确实还缺的」。** `osmium merge` 只收得拢一模一样的
    # 对象，所以同一个 id 一旦有两份数据，`osmium export` 就拒绝整份文件。两处会撞上：
    #
    # * 让 Overpass 用 `>` 把节点一起递归出来，相邻的 way 共用端点，同一个节点会落进
    #   好几批；两批若来自不同代的数据，同一个 id 就带着两个版本活下来
    #   （2026-09-13 实测 5 个节点各有 v2 与 v3，因为其中一批走了落后的镜像）。
    # * 补回来的 way 与原有的边界也共用端点——中国这一轮 301753 个节点里有 155 个
    #   本来就在文件里，而文件里那份带着版本号、取回来的是骨架，osmium 认不出是同一个。
    #
    # 所以先把 way 并进去，再让 osmium 自己说还缺哪些节点，只取那些。
    way_parts = [_as_pbf(overpass.fetch_xml(
        f"[out:xml][timeout:300];way(id:{','.join(batch)});out skel;",
        _batch_file(base, "ways", index)))
        for index, batch in enumerate(_batches(ids))]
    staged = base.with_name(f"{base.name}-ways.pbf")
    if not staged.exists():
        run_to(staged, "osmium", "merge", filtered, *way_parts)

    node_parts = [_as_pbf(overpass.fetch_xml(
        f"[out:xml][timeout:300];node(id:{','.join(batch)});out skel;",
        _batch_file(base, "nodes", index)))
        for index, batch in enumerate(_batches(_missing_nodes(staged)))]
    run_to(whole, "osmium", "merge", staged, *node_parts)


def _batch_file(base: Path, kind: str, index: int) -> Path:
    """批文件名带上批大小：换了批大小切分就变了，而**文件名不变就会读到切分不同的旧缓存**。
    补丁最终的内容与怎么分批无关，所以指纹里不带它，只带在批文件名上。"""
    return base.with_name(f"{base.name}-{kind}-{_PATCH_BATCH}-{index}.osm")


def _missing_nodes(pbf_path: Path) -> list[str]:
    """这份文件里，way 引用了却不在的节点。

    `check-refs -i` 对每条缺失的成员只报一个引用者（所以**不能**拿它统计谁缺成员，
    福州市就是这么被漏掉的），但这里要的只是 id，够用。缺引用时它返回 1，那是常态。
    """
    found = subprocess.run(["osmium", "check-refs", "-i", str(pbf_path)],
                           capture_output=True, text=True)
    if found.returncode > 1:
        raise SystemExit(f"osmium check-refs 失败（{found.returncode}）：{found.stderr.strip()}")
    return sorted({line.split(" ", 1)[0][1:] for line in found.stdout.splitlines()
                   if line.startswith("n")}, key=int)


def _batches(ids: list[str]):
    for start in range(0, len(ids), _PATCH_BATCH):
        yield ids[start:start + _PATCH_BATCH]


def _as_pbf(xml: Path) -> Path:
    """Overpass 的 XML → pbf。顺手排序：`sort` 与 `merge` 都要求输入有序，
    而 Overpass 给的顺序是它自己的。"""
    part = xml.with_suffix(".pbf")
    if not part.exists():
        run_to(part, "osmium", "sort", xml)
    return part


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
