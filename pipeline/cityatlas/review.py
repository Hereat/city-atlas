"""一个国家跑完之后的取证页：把这一轮的名单与画框摊开给人看。

S7 那条线写着「每轮必须自己核三件」——名单里有没有非城市、有没有城的画框落在离
城区十几公里外、osmium 有没有留下零字节的切片。前两件**只能靠眼睛**：一座城的画框
对不对，数字答不了，得把图画出来。所以这一页不是报表的附庸，它就是那一轮的验收现场。

三件事各有各的问法：

* **名单**：按名字末字分组数一遍。混进来的非城市（省、地区、界线、已经撤销的旧町村）
  在这张表上是显眼的——中国那轮的「广东省」、日本这轮的 157 个旧町村都是这么露出来的。
* **画框**：不问「中心离城区多远」，问**默认取景里画得出多少条路**。前者要另一份
  真值来比，后者就是用户打开城市图看到的东西——一张空纸就是空纸。最空的那些排在
  最前面，逐张画出来。
* **切片**：`WORK/<国家>/extract/` 里小于 1 KB 的文件。osmium 被内存打爆时会留下
  零字节的残件，而缓存会把它们当成切好的（`pbf.extract` 的注释讲的就是这件事）。

图用的是与 App 同一套画法（素白，含 S6 的海面闭合），不是参考实现——参考实现没有
岸线，日本这样的岛国半张图会是纸色，看图的人分不清是画框错了还是海没画。
"""

from __future__ import annotations

import base64
import gzip
import json
from pathlib import Path

from . import MAP_DATA_VERSION, WORK

# 画出来的城数。多了页面几十兆打不开，少了看不出面上的问题；
# 分四组各取一批（见 `_pick`），四十张一屏滚得完。
SAMPLE = 40

# 每张图的像素宽。页面上的画布就是这么宽，`_thin` 按它算「这一档路看不看得见」。
CANVAS_WIDTH = 560

# 各档路的淡出区间（米/像素），与 `CityMapMetrics.roadLOD` 同一张表。
# 这一页只画默认取景，所以带上一档在这个尺度上 alpha 已经是 0 的路是白撑页面：
# 上海的默认取景 19 km 宽、一个像素 34 米，第 3、4 档在那儿一根都画不出来。
ROAD_LOD = ((None, None), (90, 45), (30, 12), (8, 3))


def write(root: Path, country: str, *, out_dir: str = "out") -> Path:
    registry = json.loads((root / "registry.json").read_text(encoding="utf-8"))["cities"]
    mine = [entry for entry in registry.values() if entry.get("country") == country]
    packages = []
    for entry in sorted(mine, key=lambda e: e["cityID"]):
        path = root / out_dir / "package" / entry["cityID"] / f"{MAP_DATA_VERSION}.json.gz"
        if not path.exists():
            continue
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            packages.append((json.load(handle), path.stat().st_size))
    if not packages:
        raise SystemExit(f"{country}：{out_dir}/ 里没有这一国的包，先跑 country")

    slices = _slice_check(WORK / country.lower().replace(" ", "-") / "extract")
    groups = _pick(packages)
    payload = {city["cityID"]: _thin(city) for group in groups for city in group["cities"]}
    page = _PAGE % {
        "country": country,
        "summary": json.dumps(_summary(packages, slices), ensure_ascii=False),
        "groups": json.dumps([{**group, "cities": [city["cityID"] for city in group["cities"]]}
                              for group in groups], ensure_ascii=False),
        "renderer": RENDERER_JS,
        "data": base64.b64encode(gzip.compress(
            json.dumps(payload, separators=(",", ":")).encode("utf-8"), 6)).decode("ascii"),
    }
    out = root / out_dir / f"review-{country.lower()}.html"
    out.write_text(page, encoding="utf-8")
    return out


def _thin(city: dict) -> dict:
    """只留**这一页画得出来**的东西。两刀，都按默认取景来：

    1. 这个尺度上已经淡出的路档整档不带（`ROAD_LOD`）；
    2. 完全落在取景之外的线与面不带——包装的是整个画框（上海 72 km），
       页面只画中间那一眼（19 km），框外那些一笔都画不到。

    不裁的是 `coastline`：海面是沿**画框**边界闭合出来的（S6），少一段就围不上，
    图上会多出一条横穿的直线。这一层本来也小。
    """
    view = city["defaultView"]
    box = (view["cx"] - view["w"] / 2, view["cy"] - view["h"] / 2,
           view["cx"] + view["w"] / 2, view["cy"] + view["h"] / 2)
    meters_per_pixel = view["w"] / CANVAS_WIDTH
    roads = []
    for tier, (fade_in, _) in enumerate(ROAD_LOD):
        if fade_in is not None and meters_per_pixel >= fade_in:
            break
        roads.append(_visible_lines(city["roads"][str(tier + 1)], box))
    return {"name": city["name"], "nameLocal": city["nameLocal"],
            "frame": city["frame"], "defaultView": view,
            "roads": roads,
            "rail": _visible_lines(city["rail"], box),
            "water": _visible_blocks(city["water"], box),
            "waterLines": [line for line in city["waterLines"] if _touches(line["p"], box)],
            "green": _visible_blocks(city["green"], box),
            "coastline": city.get("coastline", [])}


def _touches(flat: list[int], box) -> bool:
    """这条折线（增量编码）的外接框与取景框相交吗。只判外接框：这一页要的是
    「别把画不到的东西带上」，不是精确裁剪——留几条擦边的比多写一份裁剪实现划算。"""
    x = y = 0
    min_x = min_y = float("inf")
    max_x = max_y = float("-inf")
    for i in range(0, len(flat) - 1, 2):
        x += flat[i]
        y += flat[i + 1]
        min_x, max_x = min(min_x, x), max(max_x, x)
        min_y, max_y = min(min_y, y), max(max_y, y)
    return not (max_x < box[0] or min_x > box[2] or max_y < box[1] or min_y > box[3])


def _visible_lines(lines: list, box) -> list:
    return [line for line in lines if _touches(line, box)]


def _visible_blocks(blocks: list, box) -> list:
    return [block for block in blocks if _touches(block["o"], box)]


def _lines(city: dict) -> int:
    return sum(len(city["roads"][tier]) for tier in city["roads"])


def _summary(packages, slices) -> dict:
    tails: dict[str, int] = {}
    for city, _ in packages:
        tails[city["name"][-1]] = tails.get(city["name"][-1], 0) + 1
    sizes = sorted(size for _, size in packages)
    return {
        "cities": len(packages),
        "bytes": sum(sizes),
        "median": sizes[len(sizes) // 2],
        # 按体积取，不是 `max((name, size))`——那样比的是名字，最大的会变成
        # 「名字排最后的那座」（实测拿到了一个生僻字打头的城）
        "largest": max(packages, key=lambda pair: pair[1])[0]["name"],
        "tails": sorted(tails.items(), key=lambda item: -item[1]),
        "emptyish": [[city["name"], _lines(city)]
                     for city, _ in sorted(packages, key=lambda p: _lines(p[0]))[:12]],
        "slices": slices,
    }


def _slice_check(extract_dir: Path) -> dict:
    """零字节（或明显残缺）的切片。判据与 `pbf.extract` 的缓存判据是同一条 1 KB。"""
    if not extract_dir.exists():
        return {"total": 0, "bad": []}
    files = sorted(extract_dir.glob("*.osm.pbf"))
    return {"total": len(files),
            "bad": [[path.name, path.stat().st_size] for path in files if path.stat().st_size < 1024]}


def _pick(packages) -> list[dict]:
    """四组：最大的、画得最空的、随机一批、以及名字末字各来一个。

    「最空的」是这一页的重点——画框落错地方的城就长这样。随机那组是对照：
    如果随机的十张都正常而最空的十张都空，问题就在那十座身上，不是面上的。
    """
    by_size = sorted(packages, key=lambda p: -p[1])
    by_lines = sorted(packages, key=lambda p: _lines(p[0]))
    taken: set[str] = set()

    def take(pairs, count):
        out = []
        for city, _ in pairs:
            if city["cityID"] in taken:
                continue
            taken.add(city["cityID"])
            out.append(city)
            if len(out) == count:
                break
        return out

    tail_first = []
    seen_tails: set[str] = set()
    for city, size in by_size:
        if city["name"][-1] not in seen_tails:
            seen_tails.add(city["name"][-1])
            tail_first.append((city, size))

    step = max(1, len(by_size) // SAMPLE)
    return [
        {"title": "画得最空的十座", "note": "画框落错地方的城就长这样：默认取景里几乎没有路。"
                                       "这一组是这一页的重点。", "cities": take(by_lines, 10)},
        {"title": "最大的十座", "cities": take(by_size, 10),
         "note": "包最大的十座，通常就是这一国的大城。名字与画框对不对，先看这一组。"},
        {"title": "各类名字各一座", "cities": take(tail_first, 6),
         "note": "名单里出现过的每种末字取一座——非城市混进来时，会在这一组里现形。"},
        {"title": "随机抽样", "cities": take(by_size[::step], SAMPLE - 26),
         "note": "按包的大小等距抽，作面上的对照。"},
    ]


# 画一座城的那段 JS。抽出来是因为**不止取证页要画城**：判断名单口径、看小城长什么样，
# 用的必须是同一套画法，否则比的是两个渲染器而不是两批数据。
# 与 App 的 `CityMapRenderer` 同源：素白样式、四档路的 LOD、S6 的海面闭合。
RENDERER_JS = r"""
/* ---------- 素白（`CityMapStyle.plain`，2026-09-07 拍板 33 那一版） ---------- */
const S = {
  paper:"#FDFCFA", road:"#D3DAD0", roadW:[5,3,1.6,.8], minPx:[1.1,.85,.6,.4], fade:[1,.82,.62,.45],
  water:"#BDD3E2", waterLine:"#ACC0CE", green:"#EFF2E6", rail:"#D3DAD0",
};
// 四档都要在：`_thin` 按取景决定带几档，小城的取景细到末梢路也画得出来
const LOD = [{on:Infinity,full:Infinity},{on:90,full:45},{on:30,full:12},{on:8,full:3}];
const alphaOf = (tier, mpp) => {
  const b = LOD[tier];
  if (!isFinite(b.on)) return 1;
  return mpp >= b.on ? 0 : mpp <= b.full ? 1 : (b.on - mpp) / (b.on - b.full);
};

/* ---------- 解码与成路（与包的编码同一份约定） ---------- */
function decode(flat){ const p=[]; let x=0,y=0;
  for(let i=0;i+1<flat.length;i+=2){ x+=flat[i]; y+=flat[i+1]; p.push([x,y]); } return p; }
function linesPath(lines){ const path=new Path2D();
  for(const l of lines){ const p=decode(l); if(!p.length) continue;
    path.moveTo(p[0][0],p[0][1]); for(let i=1;i<p.length;i++) path.lineTo(p[i][0],p[i][1]); }
  return path; }
function signedArea(p){ let s=0; for(let i=0;i<p.length;i++){ const a=p[i],b=p[(i+1)%p.length];
  s+=a[0]*b[1]-b[0]*a[1]; } return s/2; }
function blocksPath(blocks){ const path=new Path2D();
  const ring=(flat,ccw)=>{ let p=decode(flat); if(p.length<3) return;
    if((signedArea(p)>0)!==ccw) p=p.slice().reverse();
    path.moveTo(p[0][0],p[0][1]); for(let i=1;i<p.length;i++) path.lineTo(p[i][0],p[i][1]);
    path.closePath(); };
  for(const b of blocks){ ring(b.o,true); for(const h of b.i) ring(h,false); } return path; }

/* ---------- 岸线：接链 → 去重 → 沿画框顺时针闭合（S6 那份，一字未改） ---------- */
const key=(p)=>p[0]+","+p[1];
const same=(a,b)=>a[0]===b[0]&&a[1]===b[1];
function assemble(segs){
  const byStart=new Map();
  segs.forEach((s,i)=>{ const k=key(s[0]); if(!byStart.has(k)) byStart.set(k,[]); byStart.get(k).push(i); });
  const endKeys=new Set(segs.map(s=>key(s[s.length-1])));
  const used=new Array(segs.length).fill(false), chains=[];
  const grow=(i)=>{ let c=segs[i].slice(); used[i]=true;
    for(;;){ const cand=(byStart.get(key(c[c.length-1]))||[]).filter(j=>!used[j]);
      if(!cand.length) break; const j=cand[0]; used[j]=true; c=c.concat(segs[j].slice(1));
      if(same(c[0],c[c.length-1])) break; } return c; };
  segs.forEach((s,i)=>{ if(!used[i]&&!endKeys.has(key(s[0]))) chains.push(grow(i)); });
  segs.forEach((s,i)=>{ if(!used[i]) chains.push(grow(i)); });
  return chains;
}
function dedupe(chains){
  const best=new Map();
  for(const c of chains){ const closed=same(c[0],c[c.length-1]);
    const k=closed?"R"+c.map(key).sort().join(";"):"O"+key(c[0])+">"+key(c[c.length-1]);
    const prev=best.get(k); if(!prev||prev.length<c.length) best.set(k,c); }
  return [...best.values()];
}
function coastGeometry(segs,W,H){
  const chains=dedupe(assemble(segs.map(decode)));
  const hw=W/2, hh=H/2, P=2*(W+H), EPS=1.5;
  const onEdge=(p)=>Math.abs(Math.abs(p[0])-hw)<=EPS||Math.abs(Math.abs(p[1])-hh)<=EPS;
  const param=(p)=>{ const [x,y]=p;
    if(Math.abs(x+hw)<=EPS&&!(Math.abs(y-hh)<=EPS)) return y+hh;
    if(Math.abs(y-hh)<=EPS&&!(Math.abs(x-hw)<=EPS)) return H+(x+hw);
    if(Math.abs(x-hw)<=EPS&&!(Math.abs(y+hh)<=EPS)) return H+W+(hh-y);
    return (2*H+W+(hw-x))%P; };
  const corners=[{t:H,p:[-hw,hh]},{t:H+W,p:[hw,hh]},{t:2*H+W,p:[hw,-hh]},{t:0,p:[-hw,-hh]}];
  const closed=chains.filter(c=>same(c[0],c[c.length-1]));
  const open=chains.filter(c=>!same(c[0],c[c.length-1])&&onEdge(c[0])&&onEdge(c[c.length-1]));
  const polys=[], used=new Array(open.length).fill(false);
  const starts=open.map((c,i)=>({t:param(c[0]),i}));
  for(let s=0;s<open.length;s++){
    if(used[s]) continue;
    let poly=[], cur=s, guard=0;
    for(;;){
      if(guard++>open.length+2) break;
      used[cur]=true; poly=poly.concat(open[cur]);
      const te=param(open[cur][open[cur].length-1]);
      let best=null,bestD=Infinity;
      for(const st of starts){ let d=((st.t-te)%P+P)%P; if(d<1e-9&&st.i===cur) d=P;
        if(d<bestD){ bestD=d; best=st; } }
      if(!best) break;
      const passed=corners.map(c=>({p:c.p,d:((c.t-te)%P+P)%P}))
        .filter(c=>c.d>1e-9&&c.d<bestD).sort((a,b)=>a.d-b.d);
      for(const c of passed) poly.push(c.p);
      if(best.i===s) break;
      cur=best.i;
    }
    if(poly.length>2) polys.push(poly);
  }
  if(open.length===0&&closed.some(c=>signedArea(c)>0))
    polys.push([[-hw,-hh],[-hw,hh],[hw,hh],[hw,-hh]].reverse());
  const sea=new Path2D();
  const addRing=(pts,cw)=>{ let r=pts;
    if(cw!==null&&(signedArea(r)>0)===cw) r=r.slice().reverse();
    sea.moveTo(r[0][0],r[0][1]); for(let i=1;i<r.length;i++) sea.lineTo(r[i][0],r[i][1]); sea.closePath(); };
  for(const p of polys) addRing(p,true);
  for(const c of closed) addRing(c,null);
  return sea;
}

/* ---------- 画一座城 ---------- */
function draw(canvas, city){
  const W=canvas.width, H=canvas.height, v=city.defaultView;
  const ctx=canvas.getContext("2d");
  const s=W/v.w, mpp=v.w/W;
  ctx.fillStyle=S.paper; ctx.fillRect(0,0,W,H);
  ctx.save();
  ctx.setTransform(s,0,0,-s,W/2-v.cx*s,H/2+v.cy*s);
  if(city.coastline.length){ ctx.fillStyle=S.water; ctx.fill(coastGeometry(city.coastline,city.frame[0],city.frame[1]),"nonzero"); }
  ctx.fillStyle=S.green; ctx.fill(blocksPath(city.green),"nonzero");
  ctx.fillStyle=S.water; ctx.fill(blocksPath(city.water),"nonzero");
  ctx.strokeStyle=S.waterLine; ctx.lineCap="round";
  for(const l of city.waterLines){ ctx.lineWidth=Math.max(l.w,1.2/s);
    ctx.stroke(linesPath([l.p])); }
  ctx.strokeStyle=S.rail; ctx.lineWidth=Math.max(2.2,0.8/s); ctx.stroke(linesPath(city.rail));
  for(let t=city.roads.length-1;t>=0;t--){
    const a=alphaOf(t,mpp); if(a<=0) continue;
    ctx.globalAlpha=a*S.fade[t]; ctx.strokeStyle=S.road;
    ctx.lineWidth=Math.max(S.roadW[t],S.minPx[t]/s);
    ctx.stroke(linesPath(city.roads[t]));
  }
  ctx.globalAlpha=1; ctx.restore();
}
"""


_PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>散步城市图 · %(country)s 这一轮的取证</title>
<style>
  :root { --paper:#FAF9F6; --ink:#2E3A4D; --mute:#8B8F96; --line:#E2E0DA; --warn:#B4562F; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--paper); color:var(--ink);
         font:14px/1.6 -apple-system,"PingFang SC","Helvetica Neue",sans-serif;
         -webkit-font-smoothing:antialiased; }
  header { padding:28px 32px 18px; border-bottom:1px solid var(--line); }
  h1 { margin:0 0 6px; font-size:19px; font-weight:600; }
  header p { margin:0; color:var(--mute); font-size:13px; max-width:64em; }
  main { padding:22px 32px 56px; }
  h2 { font-size:15px; margin:34px 0 4px; }
  h2:first-child { margin-top:0; }
  .note { color:var(--mute); font-size:12.5px; margin:0 0 14px; max-width:64em; }
  table { border-collapse:collapse; font-size:13px; margin:0 0 8px; }
  td, th { text-align:left; padding:3px 18px 3px 0; }
  th { color:var(--mute); font-weight:500; }
  .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(240px,1fr)); gap:20px; max-width:1280px; }
  figure { margin:0; }
  .wrap { border:1px solid var(--line); border-radius:10px; overflow:hidden; background:#fff; }
  canvas { display:block; width:100%%; aspect-ratio:4/5; }
  figcaption { padding:8px 2px 0; }
  figcaption b { font-weight:600; font-size:13.5px; }
  figcaption span { display:block; color:var(--mute); font-size:12px; }
  .thin { color:var(--warn); }
  code { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12px; }
</style>
</head>
<body>
<header>
  <h1>散步城市图 · %(country)s 这一轮的取证</h1>
  <p>三件要核的事在下面：名单里有没有非城市、有没有城的画框落在离城区十几公里外、
     有没有零字节的切片。图按**默认取景**画（用户打开城市图看到的第一眼），
     素白样式，含岸线闭合，与 App 同一套画法。</p>
</header>
<main>
  <h2>一、这一轮出了什么</h2>
  <div id="summary"></div>
  <div id="sections"></div>
</main>
<script id="payload" type="application/base64">%(data)s</script>
<script>
const SUMMARY = %(summary)s;
const GROUPS = %(groups)s;
%(renderer)s

/* ---------- 页面 ---------- */
const km=(m)=>(m/1000).toFixed(m<10000?1:0);
function summaryHTML(){
  const s=SUMMARY, bad=s.slices.bad;
  const tails=s.tails.map(([t,n])=>`${t}×${n}`).join("、");
  return `<table>
    <tr><th>城</th><td>${s.cities} 座，共 ${(s.bytes/1048576).toFixed(1)} MB，单个包中位 ${(s.median/1024).toFixed(0)} KB，最大是 ${s.largest}</td></tr>
    <tr><th>名字末字</th><td>${tails}</td></tr>
    <tr><th>切片</th><td>${s.slices.total} 个，${bad.length?`<span class="thin">残缺 ${bad.length} 个：${bad.map(b=>b[0]).join("、")}</span>`:"没有小于 1 KB 的"}</td></tr>
    <tr><th>路最少的城</th><td>${s.emptyish.map(([n,c])=>`${n} ${c}`).join("、")}（名字后面是画框里的路条数）</td></tr>
  </table>`;
}

(async function(){
  const raw=Uint8Array.from(atob(document.getElementById("payload").textContent.trim()),c=>c.charCodeAt(0));
  const stream=new Blob([raw]).stream().pipeThrough(new DecompressionStream("gzip"));
  const DATA=JSON.parse(await new Response(stream).text());
  document.getElementById("summary").innerHTML=summaryHTML();
  const host=document.getElementById("sections");
  for(const g of GROUPS){
    const h=document.createElement("h2"); h.textContent="· "+g.title;
    const p=document.createElement("p"); p.className="note"; p.textContent=g.note||"";
    const grid=document.createElement("div"); grid.className="grid";
    for(const id of g.cities){
      const city=DATA[id]; if(!city) continue;
      const fig=document.createElement("figure");
      const wrap=document.createElement("div"); wrap.className="wrap";
      const canvas=document.createElement("canvas");
      canvas.width=560; canvas.height=700;
      wrap.append(canvas);
      const cap=document.createElement("figcaption");
      const lines=city.roads.reduce((n,t)=>n+t.length,0);
      cap.innerHTML=`<b>${city.name}</b><span>${city.nameLocal!==city.name?city.nameLocal+" · ":""}`
        +`画框 ${km(city.frame[0])}×${km(city.frame[1])} km · 默认 ${km(city.defaultView.w)} km`
        +` · <span class="${lines<200?"thin":""}">路 ${lines} 条</span></span>`;
      fig.append(wrap,cap); grid.append(fig);
      draw(canvas,city);
    }
    host.append(h,p,grid);
  }
})();
</script>
</body>
</html>
"""
