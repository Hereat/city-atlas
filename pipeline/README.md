# CityAtlas · 城市图静态资源生成管线（S0）

内部工具，不进 App 包。它不在 `project.yml` 的任何 target 里，是一个只依赖 Python 标准库的
脚本包，`python3 -m cityatlas <命令>` 直接在 macOS 上跑。

产出的东西有两种，都是发布时生成、放到对象存储与 CDN 上的**静态文件**：
目录分片回答「这次散步算哪座城、这座城的画框在哪」，地图包回答「底图长什么样」。
没有会处理用户坐标的服务器；归属判定在手机上完成（PRD 第 6 节）。

## 命令

```bash
cd app/tools/CityAtlas
UCDB=~/Downloads/GHS_UCDB_GLOBE_R2024A.gpkg

python3 -m cityatlas selftest                 # 24 条自检，一秒跑完，不联网
python3 -m cityatlas reference                # 从设计稿 HTML 解包出 CityMap 参考实现
python3 -m cityatlas snapshot --ucdb $UCDB    # 向 Overpass 要边界与要素快照 → snapshots/
python3 -m cityatlas build    --ucdb $UCDB    # 只读快照，生成 out/ 与 report.json
python3 -m cityatlas report                   # 打印上一次 build 的体积与耗时表
python3 -m cityatlas compare shanghai         # 「稿 vs 管线」并排比对页 → out/
python3 -m cityatlas fixtures                 # 稿的上海、杭州 → HereatTests/Fixtures/city-atlas/
```

`--only shanghai hangzhou` 限定城市（`build --only` 只出包，不重写分片与索引页——
按半份城市重写全局产物会静默丢掉别的城）；`snapshot --refresh` 忽略缓存重新联网。

**抓取与生成是分开的两步**：快照一旦落盘，`build` 就只读文件不联网。
「同一输入重跑逐字节相同」这条验收因此可核：gzip 的 mtime 写死 0、JSON 的键排序固定，
而「同一输入」指的是快照的 sha256——每个包的 `license.generatedFrom` 里记着产它的那份
快照的 sha256 与那份快照对应的 OSM 时刻，`report.json` 里也各记一份。**不是**「今天再抓一次」，
那样拿到的是今天的 OSM，字节必然不同。

抓快照时留意打印出来的 OSM 时刻：几个 Overpass 镜像的数据新旧能差好几个月，
同一版目录里混着几个月前的城不是有意的。真发生了就 `snapshot --refresh --only <slug>` 重抓。

## 目录

| 路径 | 是什么 |
| --- | --- |
| `cities.json` | 七座样本城的定义：OSM 上的 `name`、要试的 `admin_level`、找它用的经纬窗口 |
| `registry.json` | `cityID` 名册。产品自分配、永不复用、不从任何外部对象 ID 派生——这个文件就是那句话的载体 |
| `reference/` | 从设计稿解包出来的 `CityMap` 引擎、两座城的数据、八套样式表，以及把包画出来的 `preview.html` |
| `snapshots/` | Overpass 快照（gzip JSON），管线的**唯一输入** |
| `out/` | 产物：`directory/`、`package/`、`index.html` |
| `report.json` | 上一次 `build` 的体积与耗时，PRD 第 6 节的数字从这里来 |

模块各管一件事：`geometry`（投影、简化、裁剪，零依赖）、`codec`（增量整数米编码）、
`overpass`（快照）、`ghsl`（UCDB）、`boundary`（行政边界拼环）、`frame`（画框）、
`mappack`（地图包）、`directory`（1° 分片）、`publish`（名册、continuity、索引页、许可）、
`fixtures`（夹具）、`reference`（解包稿）。

## 三条与 PRD 第 6 节不同的地方

这三条都是 S0 实测之后改的，**需要产品拍板**；PRD 已按本文回写，结论那栏留空的仍待决。

### 1. 输入不是 Geofabrik extract，是 Overpass 快照

PRD 写「OSM 区域快照（Geofabrik extract）」。管线要的是画框内的道路水系绿地与一座城的
行政边界，都是局部查询；Geofabrik 的最小分区（china-latest）是 1.5 GB 的 PBF，
解析它要 protobuf 与一套索引，换来的是同一批要素。Overpass 的 bbox 查询直接给出这批要素，
且快照落盘之后就是管线唯一的输入。
城市数量涨到几百上千、Overpass 不再合适时，换的是产出快照的那一步，下游一步都不用动。

### 2. 画框的中心取 `admin_centre`，尺寸沿用稿

PRD 写「城市行政区与 GHSL 城市中心相交，取人口最大的中心，对其外接框按详情比例补足方向并加
15% 留白」。照这条跑，实测结果是：

* GHSL 城市中心的外接框是**建成区连片范围**，不是一个人走得到的范围：
  上海 75 × 68 km、杭州 73 × 60 km、成都 41 × 48 km、巴黎 46 × 56 km。
  补足比例加留白之后画框接近 90 × 110 km，落到详情图 345 pt 的宽度上是 260 米一个点——
  一个街区不到一个像素，路网糊成一片均匀的灰。稿的上海画框是 6 × 7.5 km，差一个数量级。
* 「取人口最大的相交中心」会把画框放到隔壁城市：京都在 GHSL 里没有自己的城市中心，
  它并进了「大阪」那一个，质心在四十公里外的大阪湾；旧金山同理，质心落在湾区中部的 Hayward。

所以改成三条：

* **中心**取 OSM 行政边界关系上的 `admin_centre` 节点——市政府所在地，这正是「城市中心」的
  行政定义，逐城可查、可审计。没有这个节点的城（成都、波特兰）退回「质心落在本市边界内、
  人口最大的 GHSL 城市中心」；再没有才用边界外接框中心。
* **尺寸**沿用稿：短边 6000 m、比例 4:5（稿的上海 6000 × 7500、杭州 4800 × 6000 都是 4:5）。
  能给出的推导只到量级这一层——画框大到几十公里，街区糊成一片、一次五公里的散步只有
  二十个像素；收到几公里，街区的织理读得出来、一次散步横跨大半张图。这两条把区间钉在几公里，
  钉不到「恰好 6000」。6000 是稿选的，S1 截图验证后冻结。
  **不要**把它论证成「一条五米宽的支路正好占一个像素」——算术不对，机制也反了：
  `minPx` 恰恰是让线宽不随画框尺度变的（`lineWidth = max(roadW 米, minPx 像素换算回米)`），
  道路的画宽从来不是它的实际宽度。画框变大时先坏掉的是密度，不是细路。

  版面的数：稿的手机画布 390 宽、图位容器 `padding: 6px 24px 0`，详情图因此是 342 × 462 pt；
  本 App 画布 393 → 345 × 462。**这不是 4:5**——稿的渲染器只按宽定标（`s = W / FW`），
  把 4:5 的画框画进 345 × 462 里纵向可见 8035 m，比画框高 535 m。S1 定取景时先看这条。
* **GHSL 只剩一个二值判断**：这座城有没有被城市中心覆盖（几何相交，走 GeoPackage 自带的
  R-tree 索引）。覆盖的用 6000 m，没覆盖的用 PRD 说的「最小画框」兜底，2400 m。

**这个布尔值本身是有问题的，S0 没解决**：判据用的正是上面刚被否掉的那个量。一个离大都市
二十公里的小镇，行政区多半与那座大都市连片的城市中心相交 → 判成「覆盖」→ 拿到 6000 m 的
画框 → 画出来是一片田野，而这正是最小画框想接住的情形。Bad Ischl 判成 False 是因为它在
阿尔卑斯山里离谁都远，不是因为判据准。真正想问的是「这里的路网密到值得 6000 m 的画框吗」，
用手上已有的 OSM 数据就能答（画框内一到三档道路的总长度 ÷ 面积），**不需要 GHSL**。
现在的状况是：一份 285 MB 的外部依赖只贡献一个布尔值，而这个布尔值还用错了量。
画框规则定稿时应当一起换掉。

### 3. 许可义务有两条，不是一条

PRD「许可义务」一节只写了 ODbL。城市画框是从 GHSL UCDB 推出来的，
UCDB 按 CC BY 4.0 发布（© European Union），署名义务与 ODbL 并列，两条都要落到
每个文件的 `license` 块、索引页和 App 的城市页页脚。见 `publish.LICENSES`。

## 已知缺口

* **岸线**：`natural=coastline` 是一条线，把它闭合成海面多边形需要沿画框边界接边。
  管线目前把它按线存进包的 `coastline` 一列，不闭合成面；七座样本城的画框里没有出现岸线，
  所以本版没有为它写那五十行。真正沿海的城市（例如把画框放到外滩以东）进目录之前必须补上。
* **目录分片的完整性**：分片里只有 `cities.json` 列出的城市。真正的目录要能回答
  「这一格里所有的城」，那需要按格扫描全球的 `admin_level` 边界，并解决市级层号各国不同
  （中国 4 / 5，日本 7，荷兰与美国 8）这件事。本版是七座样本城的管线，不是全球目录。
* **`continuity.json` 只产出得了三种状态**：`unchanged` / `added` / `retired`。
  PRD 第 6 节列的 `renamed` / `merged` / `split` 要么需要 continuity 记名字（它不记），
  要么需要人工判定两版城市的对应关系；全球目录还没建起来，这三种也无从产生。
  等真做目录修订时再补，现在不留空壳。
* **零依赖的代价**：不引 shapely 换来的是零构建依赖，五个几何算法各二三十行、都跑通了；
  代价是 shapely 免费给的两件事得自己补齐并测到——**把散 way 接成环**（`geometry.stitch_rings`）
  和**内环归给哪个外环**（`geometry.assemble_polygons`）。S0 的第一版恰恰在这两处各有一个
  会静默画错图的 bug（接不上的弧被弦封成实心块；一个洞挂到多个外环上被 even-odd 填回去），
  现在两处都进了 `selftest`。以后再有「关系成面」类的新代码，先想这两件事。
