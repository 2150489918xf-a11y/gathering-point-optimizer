"""
路网获取：三级降级
==================
容器内 HTTPS 常被中间网络设备干扰（实测 overpass-api.de 报 SSL EOF，
清华源 403，但宿主机 curl 正常）。所以这里准备三条通道：

  通道 1 · 容器内 OSMnx 直连Overpass（最快，环境干净时可用）
  通道 2 · 宿主 curl 拉数据 → 容器读文件（绕过容器 TLS 问题）
  通道 3 · 离线缓存（之前跑成功过就复用）

三条都失败时返回 None，主流程降级为直线距离并明确标注。
"""

import json
import os
import subprocess

CACHE_DIR = os.environ.get("ROAD_CACHE", "/work/cache")
GRAPH_FILE = os.path.join(CACHE_DIR, "jiujiang_graph.json.gz")


def _overpass_query(bbox):
    """
    按 bbox 拉取机动车路网。
    bbox = (left, bottom, right, top) = (西, 南, 东, 北)
    """
    left, bottom, right, top = bbox
    return f"""
[out:json][timeout:180];
(
  way["highway"~"^(motorway|trunk|primary|secondary|tertiary|"
      "unclassified|residential|motorway_link|trunk_link|primary_link|"
      "secondary_link|tertiary_link|living_street|service)$"](
      {bottom},{left},{top},{right});
);
out geom;
"""


def try_container_osmnx(bbox, log=print):
    """通道 1：容器内直接用 OSMnx 拉。"""
    try:
        import osmnx as ox

        ox.settings.use_cache = True
        ox.settings.log_console = False
        ox.settings.requests_timeout = 300
        for ep in ("https://overpass-api.de/api/interpreter",
                   "https://overpass.kumi.systems/api/interpreter",
                   "https://overpass.private.coffee/api/interpreter"):
            try:
                ox.settings.overpass_url = ep
                G = ox.graph_from_bbox(
                    bbox=(bbox[1], bbox[3], bbox[0], bbox[2]),  # (S,N,W,E)
                    network_type="drive", simplify=True)
                log(f"  通道1 OK: {G.number_of_nodes()} 节点 "
                    f"({ep.split('/')[2]})")
                return G
            except Exception as e:
                log(f"  通道1 {ep.split('/')[2]} 失败: {type(e).__name__}")
        return None
    except ImportError:
        log("  通道1 跳过: osmnx 未装")
        return None


def try_host_curl(bbox, log=print, host=None):
    """
    通道 2：借宿主机的手curl 拉。
    Docker 里没有"宿主机"概念，所以用 docker run 起一个精简容器执行 curl，
    结果写到挂载目录。
    """
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
    except Exception:
        pass

    q = _overpass_query(bbox)
    qfile = os.path.join(CACHE_DIR, "query.overpassql")
    ofile = os.path.join(CACHE_DIR, "raw_overpass.json")
    host = host or os.environ.get("HOST_PWD", "")

    if not host:
        log("  通道2 跳过: 未提供宿主挂载路径")
        return None

    try:
        with open(qfile, "w", encoding="utf-8") as f:
            f.write(q)

        for ep in ("https://overpass-api.de/api/interpreter",
                   "https://overpass.kumi.systems/api/interpreter",
                   "https://overpass.private.coffee/api/interpreter"):
            cmd = ["docker", "run", "--rm", "-v", f"{CACHE_DIR}:/d",
                   "curlimages/curl:latest", "-s", "--max-time", "300",
                   "--data-binary", f"@/d/query.overpassql", ep]
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=330)
                out = r.stdout.decode("utf-8", "ignore")
                if '"elements"' in out and len(out) > 500:
                    with open(ofile, "w", encoding="utf-8") as f:
                        f.write(out)
                    log(f"  通道2 OK: 拉取 {len(out)//1024} KB ({ep.split('/')[2]})")
                    return ofile
                log(f"  通道2 {ep.split('/')[2]} 返回无效数据")
            except Exception as e:
                log(f"  通道2 {ep.split('/')[2]} 失败: {type(e).__name__}")
        return None
    except Exception as e:
        log(f"  通道2 异常: {e}")
        return None


def raw_to_graph(raw_json):
    """把 Overpass 原始 JSON 转成 networkx 图（手写解析，不依赖 osmnx）。"""
    import networkx as nx
    from shapely.geometry import LineString

    with open(raw_json, "r", encoding="utf-8") as f:
        data = json.load(f)

    G = nx.Graph()
    for el in data.get("elements", []):
        if el.get("type") != "way":
            continue
        geom = el.get("geometry")
        if not geom or len(geom) < 2:
            continue
        pts = [(p["lon"], p["lat"]) for p in geom]
        length = LineString(pts).length
        # 经纬度近似转米（该纬度下）
        length_m = length * 111320 * np.cos(np.radians(pts[0][1]))

        for i in range(len(pts) - 1):
            a, b = pts[i], pts[i + 1]
            G.add_edge(a, b, length=length_m / (len(pts) - 1))

    return G


import numpy as np  # 放在这里供 raw_to_graph 使用