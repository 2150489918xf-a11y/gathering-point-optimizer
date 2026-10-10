"""
路网时间矩阵模块（两种模式）
=============================
模式 A —— OSRM 服务：若本地有 OSRM 服务且可达，调它的 /table 接口拿真实行车时间。
模式 B —— 本地路网：用 OSMnx 在线拉取指定区域路网，用 Dijkstra 算最短时间。

两种模式都返回"真实路网时间"而非直线距离，这是结果可信度的关键。
优先 A，不可用时自动降级到 B。
"""

import numpy as np


# =============== 模式 A：OSRM 服务 ===============
def is_osrm_available(base, timeout=5):
    import requests
    try:
        r = requests.get(
            f"{base}/route/v1/driving/116.2,39.3;115.9,39.3",
            params={"overview": "false"}, timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False


def osrm_table(coords_a, coords_b, base="http://localhost:5000",
               chunk=80, retries=3):
    """
    调 OSRM /table 服务算时间矩阵（分钟）。

    OSRM 的输入顺序是 lon,lat —— 注意别和 (lat, lon) 搞反，
    这是用 OSRM 最常见的错。
    """
    import requests
    import time

    na, nb = len(coords_a), len(coords_b)
    M = np.full((na, nb), np.inf)

    sess = requests.Session()

    for i0 in range(0, na, chunk):
        i1 = min(i0 + chunk, na)
        for j0 in range(0, nb, chunk):
            j1 = min(j0 + chunk, nb)

            pts = [f"{c[1]},{c[0]}" for c in coords_a[i0:i1]] + \
                  [f"{c[1]},{c[0]}" for c in coords_b[j0:j1]]
            coords_str = ";".join(pts)
            srcs = ";".join(str(k) for k in range(i1 - i0))

            url = f"{base}/table/v1/driving/{coords_str}"
            params = {"sources": srcs, "annotations": "duration"}

            for attempt in range(retries):
                try:
                    r = sess.get(url, params=params, timeout=90)
                    if r.status_code != 200:
                        time.sleep(1 + attempt); continue
                    data = r.json()
                    if data.get("code") != "Ok":
                        time.sleep(1 + attempt); continue
                    for ii, row in enumerate(data["durations"]):
                        for jj, t in enumerate(row):
                            if t is not None:
                                M[i0 + ii, j0 + jj] = t / 60.0   #秒→分钟
                    break
                except Exception:
                    time.sleep(1 + attempt)
    return M


# =============== 模式 B：本地路网 Dijkstra ===============
def load_local_graph(place, avg_speed_kmh=30.0):
    """
    获取区域路网并加上通行时间权重。

    优先用已缓存的 Overpass 原始数据（离线、快）；没有再尝试在线拉。
    返回 (graph, avg_speed_kmh, source)
    """
    import os
    import json
    CACHE = os.environ.get("ROAD_CACHE", "/work/cache")

    def add_time(G):
        Gu = G.to_undirected(as_view=False)
        for u, v, d in Gu.edges(data=True):
            km = d.get("length", 500) / 1000.0
            d["travel_time"] = km / avg_speed_kmh * 60.0     # 分钟
        return Gu

    # --- 通道 1：本地缓存 ---
    raw = os.path.join(CACHE, "raw.json")
    if os.path.exists(raw):
        try:
            from road_fetch import raw_to_graph
            G = raw_to_graph(raw)
            if G.number_of_edges() > 100:
                print(f"[路网] 使用本地缓存 {G.number_of_nodes()} 节点 / "
                      f"{G.number_of_edges()} 路段", flush=True)
                return add_time(G), avg_speed_kmh, "本地缓存路网"
        except Exception as e:
            print(f"[路网] 缓存解析失败：{type(e).__name__}", flush=True)

    # --- 通道 2：在线拉取 ---
    import osmnx as ox
    ox.settings.use_cache = True
    ox.settings.log_console = False
    ox.settings.requests_timeout = 300

    for ep in ("https://overpass.private.coffee/api/interpreter",
               "https://overpass-api.de/api/interpreter",
               "https://overpass.kumi.systems/api/interpreter"):
        try:
            print(f"[路网] 在线拉取 {ep.split('/')[2]} ...", flush=True)
            ox.settings.overpass_url = ep
            G = ox.graph_from_place(place, network_type="drive",
                                    simplify=True, retain_all=False)
            print(f"[路网] 完成 {G.number_of_nodes()} 节点", flush=True)
            return add_time(G), avg_speed_kmh, f"OSMnx@{ep.split('/')[2]}"
        except Exception as e:
            print(f"[路网] {ep.split('/')[2]} 失败: {type(e).__name__}", flush=True)

    raise RuntimeError("所有路网获取通道都失败")


def dijkstra_matrix(coords_a, coords_b, G, cutoff_min=90.0):
    """
    从每个候选点跑一次 Dijkstra，拿到它到所有需求点的最短通行时间（分钟）。

    图的节点可以是：
      · (lon, lat) 元组  —— raw_to_graph 生成的图
      · OSMnx 节点 ID    —— OSMnx 生成的图
    两种都支持，靠node_coords 判断。

    参数
    ----
    coords_a : [(lat, lon), ...]   需求点
    coords_b : [(lat, lon), ...]   候选点（Dijkstra 的源）

    返回 [n_a, n_b] 单位分钟，不可达填 inf
    """
    import networkx as nx

    # 判断图的节点类型
    sample = next(iter(G.nodes()))
    is_tuple_nodes = isinstance(sample, tuple)

    if is_tuple_nodes:
        # 节点是 (lon, lat)。用 numpy 向量化找最近邻，
        # 线性扫14529 节点 × 160 坐标在纯 Python 里要几十秒，向量化后是毫秒级。
        node_list = list(G.nodes)
        arr = np.asarray(node_list, dtype=float)          # [N, 2] -> (lon, lat)
        lon_arr, lat_arr = arr[:, 0], arr[:, 1]

        def nearest_many(coords):
            """批量最近邻。coords 是 [(lat, lon), ...]。"""
            out = []
            for lat, lon in coords:
                d2 = (lon_arr - lon) ** 2 + (lat_arr - lat) ** 2
                out.append(node_list[int(np.argmin(d2))])
            return out
    else:
        import osmnx as ox

        def nearest_many(coords):
            return [ox.nearest_node(G, lon, lat) for lat, lon in coords]

    print(f"[匹配节点] 共 {len(coords_a) + len(coords_b)} 个坐标...", flush=True)
    a_nodes = nearest_many(coords_a)
    b_nodes = nearest_many(coords_b)

    M = np.full((len(coords_a), len(coords_b)), np.inf)

    # 注意：不要用 to_undirected(as_view=True) —— 惰性视图在后续
    # 反复索引时会出现 "IndexError: list index out of range"，
    # 调试极难定位。这里统一转成普通无向图。
    Gu = G.to_undirected(as_view=False) if G.is_directed() else G

    for j, src in enumerate(b_nodes):
        try:
            times = nx.single_source_dijkstra_path_length(
                Gu, src, cutoff=cutoff_min, weight="travel_time")
        except Exception as e:
            print(f"  [warn] 候选点 {j} Dijkstra 失败: {type(e).__name__}",
                  flush=True)
            continue
        for i, ni in enumerate(a_nodes):
            if ni in times:
                M[i, j] = times[ni]
        if (j + 1) % 10 == 0:
            print(f"  Dijkstra {j+1}/{len(b_nodes)}", flush=True)

    return M


# =============== 降级：直线距离 ===============
def haversine_matrix(coords_a, coords_b, detour_factor=2.2):
    """
    直线距离（公里）×绕行系数。
    detour_factor≈2.2 是山区/城市道路的经验值。
    口径不如路网可靠，输出结果里会明确标注。
    """
    a = np.radians(np.asarray(coords_a, dtype=float))
    b = np.radians(np.asarray(coords_b, dtype=float))

    dlat = a[:, None, 0] - b[None, :, 0]
    dlon = a[:, None, 1] - b[None, :, 1]
    h = np.sin(dlat / 2) ** 2 + np.cos(a[:, None, 0]) * np.cos(b[None, :, 0]) \
        * np.sin(dlon / 2) ** 2
    return 6371.0 * 2 * np.arcsin(np.sqrt(h)) * detour_factor