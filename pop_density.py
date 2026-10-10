"""
从 OSM 建筑数据估算人口密度
============================
用途：给集合点算法提供**真实感**的人口分布，替代按公式生成的代理数据。

方法（dasymetric mapping 的简化版）：
  1. 从 OSM 拉区域内所有建筑轮廓
  2. 按建筑类型分配「每平方米住几个人」的权重
     （住宅高、商业低；这是各国人口普查的通用经验值）
  3. 把每个网格内的建筑面积 × 权重 = 该网格的估算人口

⚠️ 精度说明：
  这是**估算**，不是官方数据。建筑轮廓的完整度取决于 OSM 当地
  贡献度，九江市区尚可，郊区可能稀疏。适合判断空间分布，
  不适合做精确人口统计。精度要求高请用 WorldPop 100m 栅格。

对比：WorldPop 需要下 5GB 且服务器不支持分段下载，
      本方案只需几MB 建筑轮廓，几分钟内完成。
"""

import json
import math
import os

# 每平方米建筑面积的估算居住人数（人/㎡）。
# 参考各国人口普查的建成区居住密度经验值，取保守值。
# 数值越大 = 同样建筑面积住更多人
DENSITY_BY_TYPE = {
    #住宅类
    "house": 0.030,
    "residential": 0.030,
    "apartments": 0.032,
    "dormitory": 0.035,
    "detached": 0.022,
    "semidetached_house": 0.026,
    "terrace": 0.028,
    "bungalow": 0.024,
    "hut": 0.020,
    # 混合用途
    "mixed": 0.018,
    "commercial_residential": 0.016,
    # 商业/公共（人少但不是零）
    "commercial": 0.006,
    "retail": 0.005,
    "office": 0.005,
    "industrial": 0.002,
    "warehouse": 0.0015,
    "hotel": 0.010,
    "hospital": 0.006,
    "school": 0.008,
    "university": 0.008,
    "college": 0.008,
    "kindergarten": 0.010,
    "church": 0.006,
    "temple": 0.004,
    "civic": 0.005,
    "government": 0.005,
    "public": 0.006,
    # 其他
    "garage": 0.0005,
    "garages": 0.0005,
    "shed": 0.0005,
    "farm": 0.001,
    "greenhouse": 0.0002,
}
DEFAULT_DENSITY = 0.008      # 未分类建筑的兜底值


def _building_density(building):
    """按建筑 tags 决定每平方米的居住人数。"""
    tags = building.get("tags", {})
    btype = (tags.get("building") or "").lower()

    # building:part 之类
    if btype in ("yes", ""):
        # 住宅类常用 landuse 或 building:use 标记
        if tags.get("landuse") in ("residential",):
            return DENSITY_BY_TYPE["residential"]
        if tags.get("residential") == "yes":
            return DENSITY_BY_TYPE["residential"]
        return DEFAULT_DENSITY

    # building=flats 之类的别名
    alias = {"flats": "apartments", "apartment": "apartments",
             "residential_apartment": "apartments"}
    btype = alias.get(btype, btype)

    return DENSITY_BY_TYPE.get(btype, DEFAULT_DENSITY)


def _area_of_geom(geom):
    """
    估算建筑物的建筑面积（平方米）。

    用鞋带公式算多边形面积（经纬度平面近似）。
    对真实建筑轮廓足够用 —— 误差来自地球曲率，
    在 100 米尺度上不到 0.01%。
    """
    if not geom or len(geom) < 3:
        return 0.0

    # 取局部中心作为投影原点，避免大数值相减丢精度
    lat0 = sum(p["lat"] for p in geom) / len(geom)
    m_per_deg_lat = 111320.0
    m_per_deg_lon = 111320.0 * math.cos(math.radians(lat0))

    pts = [(p["lon"] * m_per_deg_lon, p["lat"] * m_per_deg_lat) for p in geom]

    area = 0.0
    for i in range(len(pts)):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % len(pts)]
        area += x1 * y2 - x2 * y1
    return abs(area) / 2.0


def estimate_population(buildings_json,
                        bounds,
                        n_grid_lat=28, n_grid_lon=30,
                        min_pop=1):
    """
    从建筑数据生成网格化人口分布。

    参数
    ----
    buildings_json : str   Overpass 返回的 JSON 路径
    bounds         : tuple (min_lon, min_lat, max_lon, max_lat)
    n_grid_lat/lon : int   网格行/列数

    返回
    ----
    list of dict: {lat, lon, population, built_area_m2, n_buildings}
    """
    min_lon, min_lat, max_lon, max_lat = bounds

    with open(buildings_json, "r", encoding="utf-8") as f:
        data = json.load(f)

    elements = data.get("elements", [])
    print(f"[人口估算] 读到 {len(elements):,} 个建筑要素", flush=True)

    # 网格索引
    dlat = (max_lat - min_lat) / n_grid_lat
    dlon = (max_lon - min_lon) / n_grid_lon

    cells = {}

    for el in elements:
        geom = el.get("geometry")
        if not geom or len(geom) < 3:
            continue

        # 超出范围直接跳过
        clon = sum(p["lon"] for p in geom) / len(geom)
        clat = sum(p["lat"] for p in geom) / len(geom)
        if not (min_lon <= clon <= max_lon and min_lat <= clat <= max_lat):
            continue

        area_m2 = _area_of_geom(geom)
        if area_m2 <= 0:
            continue

        dens = _building_density(el)
        pop = area_m2 * dens

        gi = int((clat - min_lat) / dlat)
        gj = int((clon - min_lon) / dlon)
        gi = max(0, min(n_grid_lat - 1, gi))
        gj = max(0, min(n_grid_lon - 1, gj))

        key = (gi, gj)
        c = cells.setdefault(key, {"built": 0.0, "n": 0, "pop": 0.0})
        c["built"] += area_m2
        c["n"] += 1
        c["pop"] += pop

    # 组装结果
    out = []
    for (gi, gj), c in cells.items():
        lat = min_lat + (gi + 0.5) * dlat
        lon = min_lon + (gj + 0.5) * dlon
        p = int(round(c["pop"]))
        if p < min_pop:
            continue
        out.append({
            "lat": lat, "lon": lon,
            "population": p,
            "built_area_m2": c["built"],
            "n_buildings": c["n"],
        })

    # 按人口降序，取前 N 个作为需求点
    out.sort(key=lambda r: r["population"], reverse=True)
    total = sum(r["population"] for r in out)
    print(f"[人口估算] {len(out)} 个网格有建筑，总估算人口 {total:,}", flush=True)
    return out


def fetch_buildings(bbox, cache_path, timeout=300):
    """
    拉取建筑数据并存到 cache，失败则复用已有缓存。

    bbox = (min_lat, min_lon, max_lat, max_lon)

    ⚠️ Overpass 对单次查询有资源上限，一次拉整个城区的建筑会返回 500。
       所以这里**自动分块**（默认 3×3），逐块拉再合并。
       分块失败时保留已成功的块，不前功尽弃。
    """
    if os.path.exists(cache_path) and os.path.getsize(cache_path) > 10000:
        print(f"[建筑数据] 使用已有缓存 {cache_path}", flush=True)
        return cache_path

    import requests
    import time

    min_lat, min_lon, max_lat, max_lon = bbox

    # 分 3×3 块
    N = 3
    dlat = (max_lat - min_lat) / N
    dlon = (max_lon - min_lon) / N

    endpoints = [
        "https://overpass.private.coffee/api/interpreter",
        "https://overpass-api.de/api/interpreter",
        "https://overpass.kumi.systems/api/interpreter",
    ]

    merged = {"elements": []}
    got = 0
    total_blocks = N * N
    failed = []

    # 多轮重试：Overpass 镜像不稳定，单轮失败率很高
    for rnd in range(3):
        if rnd:
            print(f"[建筑数据] 第 {rnd+1} 轮重试失败块 "
                  f"{len(failed)} 个 ...", flush=True)
            time.sleep(12)
        still = []
        for (bi, bj) in failed:
            a_lat = min_lat + bi * dlat
            b_lat = a_lat + dlat
            a_lon = min_lon + bj * dlon
            b_lon = a_lon + dlon

            query = (f'[out:json][timeout:180];\n'
                     f'way["building"]({a_lat:.4f},{a_lon:.4f},'
                     f'{b_lat:.4f},{b_lon:.4f});\n'
                     f'out geom;\n')

            ok = False
            for ep in endpoints:
                host = ep.split("/")[2]
                try:
                    r = requests.post(ep, data={"data": query},
                                      timeout=timeout,
                                      headers={"User-Agent": "Mozilla/5.0"})
                    if r.status_code == 200 and '"elements"' in r.text:
                        els = json.loads(r.text).get("elements", [])
                        merged["elements"].extend(els)
                        got += 1
                        print(f"  块({bi},{bj}) {host}: +{len(els)} 要素",
                              flush=True)
                        ok = True
                        break
                except Exception:
                    pass
                time.sleep(2)
            if not ok:
                still.append((bi, bj))
            else:
                time.sleep(2)

        failed = still
        if not failed:
            break

    if failed:
        print(f"[建筑数据] 警告：{len(failed)}/{total_blocks} 块未取到 "
              f"{failed}，密度数据会不完整", flush=True)

    if not merged["elements"]:
        raise RuntimeError("所有分块都拉取失败，无法获取建筑数据")

    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    tmp = cache_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False)
    os.replace(tmp, cache_path)

    print(f"[建筑数据] 完成 {got}/{total_blocks} 块，"
          f"共 {len(merged['elements']):,} 要素", flush=True)
    return cache_path