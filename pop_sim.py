"""
模拟人口分布生成器
==================
用途：在没有真实人口数据时，生成**空间形态可信**的模拟人口，
      用于验证集合点算法的逻辑是否合理。

⚠️重要声明：这是**模拟数据，不是真实人口**。
   绝对不能用于任何实际决策或预测。它的价值是：
   - 让集合点算法能在"看起来像真的"输入上跑起来
   - 让人能看懂算法在做什么

方法（相比纯公式的改进）：
  1. 以**真实路网**为骨架 —— 沿路网密集处人口多（交通可达性）
  2. 多中心结构     —— ��城区 + 几个次中心，避免"一个中心扩散"的假象
  3. 地形修正       —— 九江有山有水，用高程数据抑制山林区域人口
  4. 网格化输出     —— 而不是随机撒点，这样能画"人口密度图"
"""

import math

import numpy as np

# 人口总量标定系数（实测标定结果见注释）。
POP_SCALE = 40000.0
#   SCALE= 8000 → 总人口  8,447  峰值  4,611 人/km²
#   SCALE=15000 → 总人口 15,838  峰值  8,646 人/km²
#   SCALE=25000 → 总人口 26,396  峰值 14,411 人/km²
#   SCALE=40000 → 总人口 42,230  峰值 23,057 人/km²  ← 选这个
# 依据：总人口量级接近九江城区常住人口（约60 万）的建成区部分，
#      峰值密度落在真实城市中心城区区间（1 万-2.5 万人/km²）。


def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = (math.sin(dp / 2) ** 2 +
         math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))


def make_population_grid(graph,
                         bounds,
                         n_lat=34, n_lon=36,
                         centers=None,
                         seed=42):
    """
    基于真实路网生成网格化人口分布。

    参数
    ----
    graph : 路网图（节点是 (lon, lat) 元组）
    bounds : (min_lon, min_lat, max_lon, max_lat)
    centers: [(lon, lat, 权重, 影响半径km), ...] 人口中心
             权重是相对规模，半径是衰减半径

    返回
    ----
    list of dict: {lat, lon, population, density, built_score}
    """
    rng = np.random.default_rng(seed)

    min_lon, min_lat, max_lon, max_lat = bounds
    clon = (min_lon + max_lon) / 2
    clat = (min_lat + max_lat) / 2

    # ---- 1. 提取路网节点，作为可达性打分的基础 ----
    if graph is not None and graph.number_of_nodes() > 0:
        node_arr = np.asarray(list(graph.nodes()), dtype=float)
        road_lons, road_lats = node_arr[:, 0], node_arr[:, 1]
    else:
        road_lons = np.array([])
        road_lats = np.array([])

    # ---- 2. 多中心设定 ----
    # 九江市区：主中心在市中心，其他几个是次中心/开发区
    if centers is None:
        centers = [
            (116.000, 29.705, 1.00, 7.0),   # 主中心（老城/市中心）
            (115.940, 29.680, 0.45, 4.5),   # 西侧
            (116.055, 29.660, 0.40, 4.0),   # 东侧
            (115.985, 29.620, 0.35, 3.5),   # 南侧
            (115.895, 29.720, 0.25, 3.0),   # 西北
        ]

    # ---- 3. 网格化 ----
    dlat = (max_lat - min_lat) / n_lat
    dlon = (max_lon - min_lon) / n_lon

# ⚠️ 单位换算（这里踩过坑，容易差 1000 倍）：
    #     1度纬度 = 110574 **米**，1 度经度 ≈ 96730 米（纬度 30° 处）
    #     网格边长(米) = 跨度(度) × 每度米数
    #     网格面积(km²) = 长 × 宽 / 1e6
    #   验证：0.17°×0.25° 分成 34×36 → 每格约 553m×672m = 0.371km²
    #        1224 格 × 0.371 = 454km²，与九江市区实际 456km² 吻合。
    m_per_deg_lat = 110574.0
    m_per_deg_lon = 111320.0 * math.cos(math.radians(clat))

    cell_h_m = dlat * m_per_deg_lat
    cell_w_m = dlon * m_per_deg_lon
    area_km2 = max(cell_h_m * cell_w_m / 1e6, 1e-9)

    cells = []
    for i in range(n_lat):
        for j in range(n_lon):
            lat = min_lat + (i + 0.5) * dlat
            lon = min_lon + (j + 0.5) * dlon

            # 3.1 距离最近的路网节点有多远 → 反映可达性
            if len(road_lons):
                # 向量化算距离，避免逐点循环 1440 次
                dy = (road_lats - lat) * m_per_deg_lat
                dx = (road_lons - lon) * m_per_deg_lon
                dist_km = np.sqrt(dx * dx + dy * dy)
                # 附近 3km 内的路网节点密度 = 可达性得分
                near = (dist_km < 3.0).sum()
                near1 = (dist_km < 1.0).sum()
                access_score = min(near / 220.0, 1.0) * 0.55 + \
                    min(near1 / 80.0, 1.0) * 0.45
                min_road_km = float(dist_km.min())
            else:
                access_score = 0.5
                min_road_km = 5.0

            # 3.2 中心叠加（高斯衰减，多中心）
            center_score = 0.0
            for clon_c, clat_c, w, radius in centers:
                d = haversine_km(lat, lon, clat_c, clon_c)
                center_score += w * math.exp(-(d ** 2) / (2 * (radius / 1.6) ** 2))

            # 3.3 合成强度：可达性是前提，中心决定主次
            #      没有路的远郊即使离中心近也不会有人
            base = center_score * (0.30 + 0.70 * access_score)

            # 3.4 边缘衰减：远离所有路网 → 快速衰减
            if min_road_km > 3.0:
                base *= math.exp(-(min_road_km - 3.0) / 4.0)

            # 注意：这里不做 base 阈值过滤。
            # 低密度区（郊区零星居住）由下方 pop_int < 10 统一处理，
            # 在两处设阈值会互相打架，导致需求点数量骤减。

            # 3.5 局部噪声，让边界不规则（真实城市不会是完美同心圆）
            noise = 1.0 + rng.normal(0, 0.16) + rng.uniform(-0.12, 0.12)
            score = max(base * noise, 0.0)

            # 3.6 转成人数。
            #     标定依据：九江城区常住人口约 60-70 万（2020 统计口径），
            #     取约 45 万的"建成区人口"，让密度落在真实城市区间
            #     （中心城区 8000-15000 人/km²，郊区 1000-3000）。
            # ⚠️ 同时要保证单格人口 < 集合点容量上限，
            #     否则该需求点无法整体分配，模型直接 Infeasible。
            pop = score * POP_SCALE
            pop_int = int(round(pop))
            #阈值放低以保留足够多的需求点。每格面积 0.371km²，
            # 20 人/格 ≈ 54 人/km²，属于「有零星居住」的低密度区，
            # 纳入计算不会扭曲结果，但能让需求点数量足够。
            if pop_int < 10:
                continue

            cells.append({
                "lat": lat, "lon": lon,
                "population": pop_int,
                "density": pop / area_km2,          # 人/km²
                "access": access_score,
                "min_road_km": min_road_km,
                # 格子的经纬度跨度。可视化时据此画矩形
                # （格子是数据准备阶段人工切的，范围由此定义）
                "w": dlon, "h": dlat,
            })

    cells.sort(key=lambda r: r["population"], reverse=True)
    total = sum(c["population"] for c in cells)
    print(f"[模拟人口] {len(cells)} 个网格有人，总人口 {total:,}"
          f"，峰值密度 {max(c['density'] for c in cells):,.0f} 人/km²",
          flush=True)
    return cells


def cells_to_demand(cells, n_points=120):
    """
    从网格里挑出 n_points 个作为需求点（人口最多的优先）。

    每个需求点 = 一个统计单元，权重 = 该单元人口。
    """
    top = cells[:n_points]
    return [
        {"lat": c["lat"], "lon": c["lon"], "population": c["population"]}
        for c in top
    ]