"""
生成自包含的交互式 HTML
=====================
不依赖任何服务端，也不需要 Flask/FastAPI —— 把数据直接内嵌进HTML，
双击用浏览器打开即可。这是「能不能浏览器直接看」的答案：能，
而且零部署。

实现要点：
- 路网用 SVG 画（1.5 万条线的场景，Canvas/SVG 都够快）
- 用 CSS transform 做缩放平移，性能比重新渲染好
- 点击集合点高亮它负责的人口点
"""

import json
import os

PALETTE = ["#D85A30", "#1D9E75", "#185FA5", "#BA7517", "#7F77DD", "#D4537E"]


def build(dem, shelters, assignments, G, out_dir, capacity,
          place="九江市", cost_label="", grid=None):
    """生成单文件 HTML，返回路径。grid 是密度网格（含 w/h 格子跨度）。"""

    # ---- 路网几何 ----
    # 坐标压到 5 位小数（约 1 米精度，足够市级尺度）。
    # 这一步能把 HTML 从 ~3MB 压到 ~1MB以内。
    segs = []
    if G is not None and G.number_of_edges() > 0:
        for u, v, d in G.edges(data=True):
            segs.append([round(u[0], 5), round(u[1], 5),
                         round(v[0], 5), round(v[1], 5),
                         round(d.get("length", 100))])
    total_km = (sum(s[4] for s in segs) / 1000) if segs else 0
    mx_len = max((s[4] for s in segs), default=1)

    # ---- 集合点 ----
    order = [int(r.shelter_id) for r in shelters.itertuples()]
    shelters_l = [
        {
            "id": int(r.shelter_id),
            "lat": float(r.lat), "lon": float(r.lon),
            "pop": int(r.assigned_population),
            "usage": float(r.usage_pct),
            "avg": float(r.avg_travel_min) if r.avg_travel_min == r.avg_travel_min else 0,
            "max": float(r.max_travel_min) if r.max_travel_min == r.max_travel_min else 0,
            "served": int(r.served_points),
            "color": PALETTE[i % len(PALETTE)],
        }
        for i, r in enumerate(shelters.itertuples())
    ]

    # ---- 需求点 + 分配 ----
    dmap = {}
    for r in dem.itertuples():
        dmap[(round(float(r.lat), 6), round(float(r.lon), 6))] = int(r.population)

    points_l = []
    for r in assignments.itertuples():
        pts = [round(float(r.lon), 6), round(float(r.lat), 6)]
        pop = dmap.get((pts[1], pts[0]), int(r.population))
        points_l.append([pts[0], pts[1], pop, int(r.shelter_id),
                         float(r.travel_min)])

    # ---- 路径边缘 ----
    lons = [s[0] for s in segs] + [s[2] for s in segs] + \
           [p["lon"] for p in shelters_l] + [p[0] for p in points_l]
    lats = [s[1] for s in segs] + [s[3] for s in segs] + \
           [p["lat"] for p in shelters_l] + [p[1] for p in points_l]
    bbox = {"minLon": min(lons), "maxLon": max(lons),
            "minLat": min(lats), "maxLat": max(lats)}

    total_pop = int(dem.population.sum())
    reach = assignments[assignments.travel_min < 180]
    avg_t = float(reach.travel_min.mean()) if len(reach) else 0
    max_t = float(reach.travel_min.max()) if len(reach) else 0

    payload = {
        "roads": segs, "shelters": shelters_l, "points": points_l,
        "bbox": bbox, "capacity": capacity, "palette": PALETTE,
        # 密度网格：每个格子中心 + 跨度 + 密度，网页画成可开关的矩形层
        "grid": {
            "cells": [
                {"lon": c["lon"], "lat": c["lat"],
                 "density": round(float(c["density"]), 1),
                 "w": c.get("w", 0.007), "h": c.get("h", 0.005)}
                for c in (grid or [])
            ],
        },
        "meta": {
            "place": place, "totalPop": total_pop,
            "nPoints": len(dem), "nShelters": len(shelters_l),
            "roadNodes": G.number_of_nodes() if G else 0,
            "roadEdges": G.number_of_edges() if G else 0,
            "roadKm": round(total_km, 1), "maxLen": mx_len,
            "avgTravel": round(avg_t, 1), "maxTravel": round(max_t, 1),
            "costLabel": cost_label,
            "unreachable": int(len(assignments) - len(reach)),
            "unreachablePop": int(assignments[assignments.travel_min >= 180]
                                  .population.sum()),
        },
    }

    html = _TEMPLATE.replace("__DATA__", json.dumps(payload, ensure_ascii=False))
    html = html.replace("__TITLE__", f"{place} 人口聚集点分析")

    path = os.path.join(out_dir, "index.html")
    tmp = os.path.join(out_dir, "_tmp_index.html")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(html)
    if os.path.exists(path):
        os.remove(path)
    os.replace(tmp, path)
    return path


_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family:"Microsoft YaHei","PingFang SC",-apple-system,sans-serif;
       background:#F1EFE8; color:#2C2C2A; }
header { padding:16px 22px; background:#fff; border-bottom:1px solid #D3D1C7;
         display:flex; align-items:center; gap:22px; flex-wrap:wrap; }
h1 { font-size:17px; font-weight:500; }
.stats { display:flex; gap:20px; font-size:12.5px; color:#5F5E5A; flex-wrap:wrap; }
.stat b { color:#2C2C2A; font-weight:500; font-size:14px; }
#wrap { display:flex; height:calc(100vh - 62px); }
#map { flex:1; position:relative; overflow:hidden; background:#FAF9F6;
       cursor:grab; }
#map.drag { cursor:grabbing; }
svg { width:100%; height:100%; display:block; }
#side { width:298px; background:#fff; border-left:1px solid #D3D1C7;
        overflow-y:auto; }
#side h2 { font-size:13px; font-weight:500; padding:14px 16px 8px;
           color:#5F5E5A; }
.item { padding:11px 16px; border-bottom:1px solid #F1EFE8; cursor:pointer;
        display:flex; align-items:flex-start; gap:10px; }
.item:hover { background:#FAF9F6; }
.item.on { background:#F5F4EF; }
.dot { width:11px; height:11px; border-radius:50%; margin-top:3px;
       flex-shrink:0; }
.item .t { flex:1; }
.item .nm { font-size:13.5px; font-weight:500; margin-bottom:3px; }
.item .dt { font-size:11.5px; color:#5F5E5A; line-height:1.6; }
.bar { height:5px; background:#F1EFE8; border-radius:3px; margin-top:5px;
       overflow:hidden; }
.bar i { display:block; height:100%; border-radius:3px; }
.ctrl { padding:12px 16px; border-bottom:1px solid #F1EFE8;
        display:flex; gap:8px; flex-wrap:wrap; }
button { font:inherit; font-size:12px; padding:6px 11px; border-radius:6px;
         border:1px solid #D3D1C7; background:#fff; cursor:pointer;
         color:#2C2C2A; }
button:hover { background:#F5F4EF; }
button.on { background:#2C2C2A; color:#fff; border-color:#2C2C2A; }
label.chk { font-size:12px; display:flex; align-items:center; gap:5px;
            cursor:pointer; color:#5F5E5A; }
.hint { position:absolute; left:14px; bottom:12px; font-size:11.5px;
        color:#888780; background:rgba(255,255,255,.9); padding:5px 10px;
        border-radius:5px; }
.legend { position:absolute; right:14px; top:14px; background:rgba(255,255,255,.94);
          padding:10px 13px; border-radius:7px; font-size:11.5px;
          border:1px solid #D3D1C7; }
.legend div { display:flex; align-items:center; gap:6px; margin:3px 0; }
.warn { padding:10px 16px; background:#FCEBEB; border-left:3px solid #E24B4A;
        font-size:11.5px; color:#791F1F; line-height:1.6; }
</style>
</head>
<body>
<header>
  <h1>__TITLE__</h1>
  <div class="stats" id="stats"></div>
</header>
<div id="wrap">
  <div id="map">
    <svg id="svg"><g id="g"></g></svg>
    <div class="legend">
      <div><svg width="13" height="13"><circle cx="6.5" cy="6.5" r="5"
        fill="#D85A30"/></svg> 集合点（大小=承载人数）</div>
      <div><svg width="13" height="13"><circle cx="6.5" cy="6.5" r="4"
        fill="#85B7EB"/></svg> 人口需求点</div>
      <div><svg width="13" height="13"><line x1="1" y1="6.5" x2="12" y2="6.5"
        stroke="#D85A30" stroke-width="1.6"/></svg> 分配连线</div>
      <div><svg width="13" height="13"><line x1="1" y1="6.5" x2="12" y2="6.5"
        stroke="#D3D1C7" stroke-width="1.2"/></svg> 路网</div>
      <div><svg width="13" height="13"><rect x="1" y="1" width="11" height="11"
        fill="#E34A33" fill-opacity="0.72"/></svg> 密度网格（悬停看数值）</div>
    </div>
    <div class="hint">滚轮缩放 · 拖拽平移 · 悬停格子看密度 · 点击集合点或右侧列表高亮</div>
  </div>
  <div id="side">
    <div class="ctrl">
      <button id="btnAll" class="on">全部</button>
      <button id="btnReset">重置视图</button>
      <label class="chk"><input type="checkbox" id="cbRoad" checked>路网</label>
      <label class="chk"><input type="checkbox" id="cbGrid">密度网格</label>
      <label class="chk"><input type="checkbox" id="cbLink" checked>连线</label>
    </div>
    <h2>集合点列表</h2>
    <div id="list"></div>
  </div>
</div>

<script>
const D = __DATA__;
const svg = document.getElementById('svg');
const g = document.getElementById('g');
const B = D.bbox, CAP = D.capacity;

// ---- 投影：经纬度 -> SVG 坐标 ----
const W = 1000, H = 1000;
const spanX = (B.maxLon - B.minLon) || 1;
const spanY = (B.maxLat - B.minLat) || 1;
// 保持纵横比
const scale = Math.min(W / spanX, H / spanY);
const offX = (W - spanX * scale) / 2;
const offY = (H - spanY * scale) / 2;
function px(lon){ return offX + (lon - B.minLon) * scale; }
function py(lat){ return offY + (B.maxLat - lat) * scale; }

let content = document.createElementNS('http://www.w3.org/2000/svg','g');
g.appendChild(content);

function el(tag, attrs){
  const e = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for(const k in attrs) e.setAttribute(k, attrs[k]);
  return e;
}

// ---- 路网 ----
let roadG = el('g', {id:'roads'});
content.appendChild(roadG);
const frag = document.createDocumentFragment();
D.roads.forEach(s=>{
  frag.appendChild(el('line',{x1:px(s[0]),y1:py(s[1]),x2:px(s[2]),y2:py(s[3]),
    stroke:'#D3D1C7','stroke-width':(0.4+1.2*s[4]/D.meta.maxLen).toFixed(2),
    'stroke-opacity':0.85}));
});
roadG.appendChild(frag);

// ---- 密度网格（可开关。格子是人工切的统计单元，颜色=人/km²） ----
let gridG = el('g', {id:'grid'});
content.appendChild(gridG);
function densColor(t){
  // 与 PNG 版一致的色标：浅米 -> 橙 -> 深红
  const stops=[[0,'#FFF7EC'],[0.2,'#FEE8C8'],[0.45,'#FDBB84'],
               [0.68,'#E34A33'],[0.86,'#B30000'],[1,'#7A0000']];
  if(t<=0) return stops[0][1];
  for(let i=1;i<stops.length;i++){
    if(t<=stops[i][0]){
      const [t0,c0]=stops[i-1],[t1,c1]=stops[i];
      const f=(t-t0)/(t1-t0);
      const A=parseInt(c0.slice(1),16),B=parseInt(c1.slice(1),16);
      const ch=k=>Math.round(((A>>k)&0xff)+((((B>>k)&0xff)-((A>>k)&0xff))*f));
      return 'rgb('+ch(16)+','+ch(8)+','+ch(0)+')';
    }
  }
  return stops[stops.length-1][1];
}
if(D.grid && D.grid.cells.length){
  const mx = Math.max(...D.grid.cells.map(c=>c.density)) || 1;
  const gfrag = document.createDocumentFragment();
  D.grid.cells.forEach(c=>{
    const x1=px(c.lon-c.w/2), y1=py(c.lat+c.h/2);
    const x2=px(c.lon+c.w/2), y2=py(c.lat-c.h/2);
    const r = el('rect',{
      x:Math.min(x1,x2), y:Math.min(y1,y2),
      width:Math.abs(x2-x1), height:Math.abs(y2-y1),
      fill:densColor(c.density/mx), 'fill-opacity':0.72,
      stroke:'none'});
    const tt = el('title');
    tt.textContent = '密度 ' + Math.round(c.density).toLocaleString() + ' 人/km²';
    r.appendChild(tt);
    gfrag.appendChild(r);
  });
  gridG.appendChild(gfrag);
}

// ---- 连线 ----
let linkG = el('g', {id:'links'});
content.appendChild(linkG);
const smap = {}; D.shelters.forEach(s=>smap[s.id]=s);
D.points.forEach(p=>{
  const s = smap[p[3]]; if(!s) return;
  linkG.appendChild(el('line',{x1:px(p[0]),y1:py(p[1]),x2:px(s.lon),y2:py(s.lat),
    stroke:s.color,'stroke-width':0.55,'stroke-opacity':0.32}));
});

// ---- 需求点 ----
let ptG = el('g', {id:'pts'});
content.appendChild(ptG);
const pmax = Math.max(...D.points.map(p=>p[2])) || 1;
D.points.forEach(p=>{
  const s = smap[p[3]];
  const r = 2 + 9*Math.sqrt(p[2]/pmax);
  const c = el('circle',{cx:px(p[0]),cy:py(p[1]),r:r.toFixed(1),
    fill:s?s.color:'#85B7EB','fill-opacity':0.5, 'data-sid':p[3]});
  ptG.appendChild(c);
});

// ---- 集合点 ----
let shG = el('g', {id:'shs'});
content.appendChild(shG);
const smax = Math.max(...D.shelters.map(s=>s.pop)) || 1;
D.shelters.forEach(s=>{
  const r = 9 + 15*Math.sqrt(s.pop/smax);
  const g2 = el('g', {'data-id':s.id, style:'cursor:pointer'});
  g2.appendChild(el('circle',{cx:px(s.lon),cy:py(s.lat),r:r,
    fill:s.color,'fill-opacity':0.22, stroke:s.color,'stroke-width':2.2,
    'stroke-opacity':0.5}));
  g2.appendChild(el('circle',{cx:px(s.lon),cy:py(s.lat),r:r*0.42,
    fill:s.color, stroke:'#fff','stroke-width':2}));
  const t = el('text',{x:px(s.lon), y:py(s.lat)-r-7,
    'text-anchor':'middle','font-size':12,'font-weight':'500',
    fill:s.color, stroke:'#fff','stroke-width':3,'paint-order':'stroke',
    'font-family':'Microsoft YaHei, sans-serif'});
  t.textContent = 'G'+s.id;
  g2.appendChild(t);
  g2.addEventListener('click', ev=>{ ev.stopPropagation(); pick(s.id); });
  shG.appendChild(g2);
});

// ---- 侧栏 ----
const list = document.getElementById('list');
D.shelters.forEach(s=>{
  const d = document.createElement('div');
  d.className='item'; d.dataset.id=s.id;
  d.innerHTML = '<div class="dot" style="background:'+s.color+'"></div>'+
    '<div class="t"><div class="nm">G'+s.id+'　'+s.pop.toLocaleString()+' 人</div>'+
    '<div class="dt">容量利用率 '+s.usage.toFixed(1)+'%　服务 '+s.served+' 个人群点<br>'+
    '平均 '+s.avg.toFixed(1)+' 分 · 最长 '+s.max.toFixed(1)+' 分</div>'+
    '<div class="bar"><i style="width:'+Math.min(s.usage,100)+'%;background:'+
    s.color+'"></i></div></div>';
  d.addEventListener('click',()=>pick(s.id));
  list.appendChild(d);
});

// ---- 顶部统计 ----
const m = D.meta;
document.getElementById('stats').innerHTML =
  '<span class="stat">成本口径 <b>'+m.costLabel+'</b></span>'+
  '<span class="stat">总人口 <b>'+m.totalPop.toLocaleString()+'</b></span>'+
  '<span class="stat">集合点 <b>'+m.nShelters+'</b> 个</span>'+
  '<span class="stat">平均出行 <b>'+m.avgTravel+'</b> 分钟</span>'+
  '<span class="stat">最长出行 <b>'+m.maxTravel+'</b> 分钟</span>'+
  '<span class="stat">路网 <b>'+m.roadEdges.toLocaleString()+'</b> 路段 / '+
  m.roadKm+' km</span>';

if(m.unreachable>0){
  const w=document.createElement('div');
  w.className='warn';
  w.textContent='⚠ 有 '+m.unreachable+' 个人群点被路网孤立（'+ 
    m.unreachablePop.toLocaleString()+' 人），路网数据缺失非算法问题，需另行规划。';
  document.getElementById('side').insertBefore(w, list);
}

// ---- 高亮 ----
let cur = null;
function pick(id){
  cur = (cur===id) ? null : id;
  document.querySelectorAll('.item').forEach(e=>
    e.classList.toggle('on', +e.dataset.id===cur));
  ptG.querySelectorAll('circle').forEach(c=>{
    const on = (cur===null) || (+c.dataset.sid===cur);
    c.setAttribute('fill-opacity', on ? (cur===null?0.5:0.92) : 0.07);
  });
  linkG.querySelectorAll('line').forEach((l,i)=>{
    const p = D.points[i];
    const on = (cur===null) || (p[3]===cur);
    l.setAttribute('stroke-opacity', on ? 0.32 : 0.03);
    l.setAttribute('stroke-width', on ? (cur===null?0.55:0.9) : 0.3);
  });
  shG.querySelectorAll('g').forEach(g2=>{
    const on = (cur===null) || (+g2.dataset.id===cur);
    g2.setAttribute('opacity', on ? 1 : 0.22);
  });
}
svg.addEventListener('click',()=>pick(null));

// ---- 缩放平移 ----
let vb = {s:1, tx:0, ty:0};
function apply(){
  content.setAttribute('transform',
    'translate('+vb.tx+','+vb.ty+') scale('+vb.s+')');
}
svg.addEventListener('wheel', e=>{
  e.preventDefault();
  const r = svg.getBoundingClientRect();
  const mx = (e.clientX-r.left)/r.width*W, my=(e.clientY-r.top)/r.height*H;
  const f = e.deltaY<0 ? 1.16 : 1/1.16;
  const ns = Math.max(0.6, Math.min(14, vb.s*f));
  vb.tx = mx-(mx-vb.tx)*(ns/vb.s);
  vb.ty = my-(my-vb.ty)*(ns/vb.s);
  vb.s = ns; apply();
}, {passive:false});

let drag=null;
svg.addEventListener('mousedown', e=>{
  drag={x:e.clientX,y:e.clientY,tx:vb.tx,ty:vb.ty};
  document.getElementById('map').classList.add('drag');
});
window.addEventListener('mousemove', e=>{
  if(!drag) return;
  const r = svg.getBoundingClientRect();
  vb.tx = drag.tx + (e.clientX-drag.x)*W/r.width;
  vb.ty = drag.ty + (e.clientY-drag.y)*H/r.height;
  apply();
});
window.addEventListener('mouseup', ()=>{
  drag=null; document.getElementById('map').classList.remove('drag');
});

document.getElementById('btnReset').addEventListener('click',()=>{
  vb={s:1,tx:0,ty:0}; apply(); pick(null);
});
document.getElementById('btnAll').addEventListener('click',()=>pick(null));
document.getElementById('cbRoad').addEventListener('change',e=>{
  roadG.setAttribute('display', e.target.checked?'':'none');});
const _cbGrid=document.getElementById('cbGrid');
if(D.grid && D.grid.cells.length){
  _cbGrid.addEventListener('change',e=>{
    gridG.setAttribute('display', e.target.checked?'':'none');});
}else{
  // 无网格数据时隐藏开关，避免一个点了没反应的假按钮
  _cbGrid.parentElement.style.display='none';
}
document.getElementById('cbLink').addEventListener('change',e=>{
  linkG.setAttribute('display', e.target.checked?'':'none');});

apply();
</script>
</body>
</html>
"""