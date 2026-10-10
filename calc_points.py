"""
从人口分布计算集合点
=====================
问题：一片区域的人口已知，要在几个位置设集合点，才能让所有人
      （a）走得尽量近，（b）单个集合点不超容量。

算法：容量约束 p-median（Capacity-Constrained P-Median）
      目标  min  Σ_ij  人口_i × 时间_ij × x_ij
      约束  每个集合点分配人数 ≤ 其容量上限

距离：三种口径，按优先级自动降级，并在输出中明确标注
      1. OSRM 服务（最准，需外部服务）
      2. 本地路网 Dijkstra（准，OSMnx 在线拉路网）← 默认
      3. 直线距离 × 绕行系数（近似，仅供流程验证）

运行：
    docker run --rm -v "$(pwd)/output:/work/output" gathering-solver
"""

import os
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

import route_matrix

# ================= 配置 =================
PLACE = "Jiujiang, Jiangxi, China"   # OSMnx 拉路网用的区域名
N_SHELTERS = 10                       # 设几个集合点
SHELTER_CAPACITY = 12000             # 每个集合点容量上限（人）
MAX_TRAVEL_MIN = 60                   # 可接受的最长出行时间（分钟）
N_DEMAND = 120# 需求点数量
N_SITES = 40                          # 候选集合点数量
SEED = 42
AVG_SPEED_KMH = 30.0                  # 平均车速假设
POP_SOURCE = os.environ.get("POP_SOURCE", "sim")   # "sim" | "osm" | "proxy"

# 区域经纬度范围（九江市区）
LON_MIN, LON_MAX = 115.85, 116.10
LAT_MIN, LAT_MAX = 29.58, 29.75

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.environ.get("OUTPUT_DIR", os.path.join(HERE, "output"))
os.makedirs(OUT, exist_ok=True)

OSRM_BASE = os.environ.get("OSRM_BASE", "http://localhost:5000")
COST_LABEL = ""          # 本次运行实际用的成本口径，绘图时展示


def log(m):
    print(f"[{m}]", flush=True)


# ================= 求解器自动探测 =================
def get_solver():
    """
    构造一个可用的 LP 求解器**实例**。

    ⚠️ spopt 的 solve(solver=...) 要的是实例不是字符串名。
    写solve(solver="CBC") 会报：
        AttributeError: 'str' object has no attribute 'actualSolve'
    另外 puLP 3.x 的名字是 PULP_CBC_CMD 而不是 CBC，别写死。
    """
    import pulp

    try:
        avail = pulp.listSolvers(onlyAvailable=True)
    except Exception:
        avail = []
    log(f"可用求解器：{avail}")

    # 名称 -> 构造函数
    candidates = [
        ("PULP_CBC_CMD", lambda: pulp.PULP_CBC_CMD(msg=False)),
        ("HiGHS_CMD", lambda: pulp.HiGHS_CMD(msg=False)),
        ("GLPK_CMD", lambda: pulp.GLPK_CMD(msg=False)),
        ("SCIP_CMD", lambda: pulp.SCIP_CMD(msg=False)),
        ("GUROBI_CMD", lambda: pulp.GUROBI_CMD(msg=False)),
        ("CPLEX_CMD", lambda: pulp.CPLEX_CMD(msg=False)),
    ]

    for name, factory in candidates:
        if name in avail:
            try:
                log(f"使用求解器：{name}")
                return factory()
            except Exception as e:
                log(f"  {name} 实例化失败：{e}")

    raise RuntimeError(
        f"没有可用的 LP 求解器。已探测到：{avail}。"
        "可在 Dockerfile 里加装，例如 apt-get install -y glpk-utils")


def solve_with(solver, model):
    """用指定求解器实例求解，失败返回 None。"""
    try:
        return model.solve(solver=solver)
    except Exception as e:
        log(f"求解失败：{type(e).__name__}: {e}")
        return None


# ================= 1. 自检：验证算法行为正确 =================
def self_check():
    """
    用能手算验证的算例确认求解器行为正确。
    目的是排除"代码跑通但算错"这种最坑的情况。

    算例 1 —— 人口权重是否生效？
        P0(1000人) →[A:1, B:10]
        P1( 100人) →[A:10, B:1]
        只开1 个点，容量足够。
        选 A：1000×1 + 100×10 = 2000
        选 B：1000×10 + 100×1 = 10100
        → 必须选 A。若选 B 说明模型没把人口当权重，结论全错。

    算例 2 —— 容量约束是否生效？
        3 需求点(各100人) → 2 候选 A(容量250) / B(容量250)
        P0 →[A:1, B:9]
        P1 →[A:9, B:1]
        P2 →[A:9, B:1]
        容量 250 = 最多收 2 人。3 人分 2 点，必有 1 点收 2 人。
        最优：A 收 P0（1），B 收 P1+P2（1+1）→ 总距离 3
        若容量不生效，可能把 3 人全塞给 B（1+1+9）→ 总距离 11
    """
    log("运行自检 ...")
    try:
        import pulp
        from spopt.locate import PMedian
    except Exception as e:
        log(f"跳过自检：依赖未装({e})")
        return

    solver = get_solver()
    all_ok = True

    # ---------- 算例 1：人口权重 ----------
    cost1 = np.array([[1., 10.], [10., 1.]])
    ai1 = np.array([1000., 100.])
    r1 = PMedian.from_cost_matrix(
        cost1, ai1, p_facilities=1,
        facility_capacities=np.array([5000.]), name="w").solve(solver=solver)

    chosen1 = [f for f, c in enumerate(r1.fac2cli) if len(c)]
    ok1 = chosen1 == [0]
    all_ok &= ok1
    log(f"  算例1 人口权重: 选中 {chosen1}，期望 [0]（权重必须生效）"
        f" → {'通过' if ok1 else '失败'}")

    # ---------- 算例 2：容量约束 ----------
    cost2 = np.array([[1., 9.], [9., 1.], [9., 1.]])
    ai2 = np.array([100., 100., 100.])
    r2 = PMedian.from_cost_matrix(
        cost2, ai2, p_facilities=2,
        facility_capacities=np.array([250., 250.]), name="c").solve(solver=solver)

    groups2 = {f: [int(c) for c in cli]
               for f, cli in enumerate(r2.fac2cli) if len(cli)}
    total2 = sum(cost2[c][f] for f, v in groups2.items() for c in v)
    ok2_cap = all(len(v) * 100 <= 250 for v in groups2.values())
    ok2_opt = abs(total2 - 3.0) < 1e-6
    all_ok &= (ok2_cap and ok2_opt)

    log(f"  算例2 容量约束: 分组 {groups2}")
    log(f"       承载 { {f: len(v)*100 for f,v in groups2.items()} }"
        f"，上限 250")
    log(f"       总距离 {total2}，期望 3.0")
    log(f"       容量 {'通过' if ok2_cap else '失败'}"
        f" · 最优性 {'通过' if ok2_opt else '失败'}")

    log("自检通过 —— 算法行为正确" if all_ok
        else "自检未通过，结果不可信，先排查")
    return all_ok


# ================= 2. 人口分布 =================
DENSITY_CACHE = None      # 网格人口密度（人/km²），供绘图用
_GRAPH_CACHE = None       # 路网图缓存，避免重复解析 1.5 万条路段


def load_osm_population(n=N_DEMAND):
    """
    从 OSM 建筑数据估算人口分布（dasymetric mapping 简化版）。

    思路：建筑轮廓 → 按建筑类型算建筑面积 → 乘每平米居住人数
          → 归并到网格 → 取人口最多的 n 个网格作为需求点

    精度说明：这是**估算**。建筑轮廓完整度取决于 OSM 当地贡献度。
    用于判断空间分布够用，做精确统计请换 WorldPop 100m 栅格。
    """
    global DENSITY_CACHE

    import pop_density as PD

    bld_file = os.path.join(os.environ.get("ROAD_CACHE", "/work/cache"),
                            "buildings.json")

    log("获取 OSM 建筑数据...")
    PD.fetch_buildings(
        (LAT_MIN - 0.02, LON_MIN - 0.07, LAT_MAX + 0.04, LON_MAX + 0.05),
        bld_file)

    cells = PD.estimate_population(
        bld_file,
        bounds=(LON_MIN, LAT_MIN, LON_MAX, LAT_MAX),
        n_grid_lat=28, n_grid_lon=30)

    if not cells:
        log("OSM 建筑数据为空，回退到代理模型")
        return None

    # 取人口最多的 n 个网格作为需求点
    cells.sort(key=lambda r: r["population"], reverse=True)
    top = cells[:n]

    # 记录网格密度（人/km²）供绘图
    import math
    cell_h_deg = (LAT_MAX - LAT_MIN) / 28
    cell_w_deg = (LON_MAX - LON_MIN) / 30
    mlat = 110.57 * cell_h_deg
    mlon = 111.32 * math.cos(math.radians((LAT_MIN + LAT_MAX) / 2)) * cell_w_deg
    area_km2 = max(mlat * mlon / 1e6, 1e-6)
    DENSITY_CACHE = [
        {"lat": c["lat"], "lon": c["lon"], "density": c["population"] / area_km2}
        for c in cells if c["population"] > 0
    ]

    df = pd.DataFrame([
        {"lat": c["lat"], "lon": c["lon"], "population": c["population"]}
        for c in top
    ])
    log(f"OSM 人口估算：{len(df)} 个需求点，"
        f"总人口 {df.population.sum():,}（建筑密度推算）")
    return df


def build_population(n=N_DEMAND, seed=SEED):
    """
    人口分布。数据源由 POP_SOURCE 决定：

    "sim"    基于真实路网的多中心模拟（默认，形态可信但非真实人口）
    "osm"    OSM 建筑密度估算（需要网络，可能被镜像限流）
    "proxy"  简单指数衰减（最粗糙）
    """
    global DENSITY_CACHE, POP_SOURCE

    if POP_SOURCE in ("sim", "osm"):
        try:
            # 先拿路网，图和人口都用它
            global _GRAPH_CACHE
            if _GRAPH_CACHE is None:
                _GRAPH_CACHE, spd, src = route_matrix.load_local_graph(
                    PLACE, AVG_SPEED_KMH)
            G = _GRAPH_CACHE

            if POP_SOURCE == "osm":
                df = load_osm_population(n)
            else:
                df = None

            if df is None or len(df) < 10:
                import pop_sim
                log("生成基于路网的模拟人口分布...")
                cells = pop_sim.make_population_grid(
                    G,
                    bounds=(LON_MIN, LAT_MIN, LON_MAX, LAT_MAX))
                DENSITY_CACHE = [
                    {"lat": c["lat"], "lon": c["lon"], "density": c["density"]}
                    for c in cells
                ]
                pts = pop_sim.cells_to_demand(cells, n)
                df = pd.DataFrame(pts)
                log(f"模拟人口：{len(df)} 个需求点，"
                    f"总人口 {df.population.sum():,}"
                    f"（模拟数据，非真实人口）")
                return df

            return df

        except Exception as e:
            log(f"模拟/OSM 人口生成失败（{type(e).__name__}: {e}）"
                f"，回退到粗糙代理模型")
            DENSITY_CACHE = None

    # ---- 粗糙代理 ----
    log("使用粗糙代理人口模型（离中心越近越密）——非真实人口")
    rng = np.random.default_rng(seed)
    lats = rng.uniform(LAT_MIN, LAT_MAX, n)
    lons = rng.uniform(LON_MIN, LON_MAX, n)
    clat, clon = (LAT_MIN + LAT_MAX) / 2, (LON_MIN + LON_MAX) / 2
    dist_km = np.sqrt(((lats - clat) * 111) ** 2 +
                      ((lons - clon) * 111 * np.cos(np.radians(clat))) ** 2)
    density = np.exp(-dist_km / 5.0) * 1600 + 100
    population = np.round(density).astype(int)
    df = pd.DataFrame({"lat": lats, "lon": lons, "population": population})
    log(f"人口分布：{len(df)} 个需求点，总人口 {df.population.sum():,}")
    return df



# ================= 3. 候选集合点 =================
def build_candidate_sites(dem, n=N_SITES, seed=SEED):
    """
    候选集合点备选池。

    ⚠️ 当前自动挑「局部人口密度高」的备选。
       真实场景应替换为实际可用场地坐标（学校/医院/广场/停车场）。
    """
    rng = np.random.default_rng(seed + 1)

    coords = np.column_stack([dem.lat.values, dem.lon.values])
    weights = dem.population.to_numpy(dtype=float)

    sites, tries = [], 0
    while len(sites) < n and tries < n * 60:
        tries += 1
        lat = rng.uniform(LAT_MIN, LAT_MAX)
        lon = rng.uniform(LON_MIN, LON_MAX)
        d = np.sqrt(((coords[:, 0] - lat) * 111) ** 2 +
                    ((coords[:, 1] - lon) * 111 * np.cos(np.radians(lat))) ** 2)
        local = weights[d < 3.0].sum()
        if local > 0:
            sites.append({"lat": lat, "lon": lon,
                          "local_demand": int(local)})

    df = pd.DataFrame(sites).sort_values("local_demand", ascending=False)
    log(f"候选集合点：{len(df)} 个备选")
    return df.reset_index(drop=True)


# ================= 4. 时间矩阵 =================
def build_cost_matrix(dem, sites):
    """
    算每个人群点到每个候选点的出行时间（分钟）。
    优先 OSRM → 本地路网 → 直线距离，逐级降级并标注口径。
    返回 (成本矩阵, 口径标签, 路网图或None)
    """
    A = list(zip(dem.lat, dem.lon))
    B = list(zip(sites.lat, sites.lon))

    # --- 模式 A：OSRM ---
    if route_matrix.is_osrm_available(OSRM_BASE):
        log(f"检测到 OSRM({OSRM_BASE})，使用真实路网行车时间")
        M = route_matrix.osrm_table(A, B, base=OSRM_BASE)
        if not np.all(np.isinf(M)):
            return M, "OSRM 路网时间", None
        log("OSRM 异常，尝试本地路网")

    # --- 模式 B：本地路网 Dijkstra ---
    G = None
    try:
        log("使用本地路网 + Dijkstra")
        global _GRAPH_CACHE
        if _GRAPH_CACHE is None:
            _GRAPH_CACHE, spd, src = route_matrix.load_local_graph(
                PLACE, AVG_SPEED_KMH)
        else:
            spd = AVG_SPEED_KMH      # 缓存命中时也要给 spd 赋值
        G = _GRAPH_CACHE
        M = route_matrix.dijkstra_matrix(
            A, B, G, cutoff_min=MAX_TRAVEL_MIN * 3)
        if not np.all(np.isinf(M)):
            return M, f"本地路网 Dijkstra @{spd:.0f}km/h", G
        log("路网覆盖不足，降级为直线距离")
    except Exception as e:
        import traceback
        log(f"路网拉取失败（{type(e).__name__}: {str(e)[:120]}），降级为直线距离")
        for line in traceback.format_exc().splitlines()[-5:]:
            log("诊断| " + line.strip())

    # --- 模式 C：直线距离 ---
    log("⚠降级为直线距离×绕行系数，结果仅供流程验证")
    return route_matrix.haversine_matrix(A, B), "直线距离(近似)", G


# ================= 5. 求解 =================
def solve(dem, sites, cost, n_shelters, capacity):
    from spopt.locate import PMedian

    ai = dem.population.to_numpy(dtype=float)
    cj = np.full(len(sites), float(capacity))

    # 不可达的点：给一个大成本而不是 inf。
    # inf 会让 LP 求解器直接判定无解；BIG 只让模型尽量避开，
    # 实在避不开时仍能给可行解（这部分人实际需单独规划）。
    BIG = MAX_TRAVEL_MIN * 3
    unreachable = ~np.isfinite(cost)
    bad_rows = unreachable.all(axis=1) if unreachable.size else np.zeros(0, bool)

    if unreachable.any():
        log(f"[成本矩阵] {unreachable.sum()} 个组合不可达（路网到不了），"
            f"其中 {int(bad_rows.sum())} 个人群点所有候选点都到不了")
        if bad_rows.any():
            log(f"[成本矩阵] ⚠ {int(bad_rows.sum())} 个需求点被路网孤立，"
                f"其人口 {int(ai[bad_rows].sum()):,} 需另行规划，"
                f"本次结果不含这部分")
    cost = np.where(unreachable, BIG, cost)

    log(f"求解：{n_shelters} 个集合点 / 容量上限 {capacity:,}")
    model = PMedian.from_cost_matrix(
        cost, ai, p_facilities=n_shelters,
        facility_capacities=cj, name="gathering-points")

    solver = get_solver()
    res = solve_with(solver, model)
    if not res:
        raise RuntimeError("求解失败")

    n_used = len([1 for c in res.fac2cli if len(c)])
    log(f"求解完成，启用 {n_used} 个集合点")
    return res


# ================= 6. 输出 =================
def export(dem, sites, res, cost, method):
    ai = dem.population.to_numpy(dtype=float)
    BIG = MAX_TRAVEL_MIN * 5

    rows, arows = [], []
    for fac, cli in enumerate(res.fac2cli):
        if not len(cli):
            continue
        cli = list(cli)
        pop = ai[cli].sum()
        t = cost[cli, fac]
        t = t[t < BIG]
        rows.append({
            "shelter_id": int(fac),
            "lat": round(sites.iloc[fac].lat, 6),
            "lon": round(sites.iloc[fac].lon, 6),
            "assigned_population": int(pop),
            "capacity": int(SHELTER_CAPACITY),
            "usage_pct": round(pop / SHELTER_CAPACITY * 100, 1),
            "avg_travel_min": round(float(t.mean()), 1) if len(t) else None,
            "max_travel_min": round(float(t.max()), 1) if len(t) else None,
            "served_points": len(cli),
        })
        for ci in cli:
            arows.append({
                "lat": round(dem.iloc[ci].lat, 6),
                "lon": round(dem.iloc[ci].lon, 6),
                "population": int(ai[ci]),
                "shelter_id": int(fac),
                "travel_min": round(float(cost[ci, fac]), 1),
            })

    shelters = pd.DataFrame(rows).sort_values(
        "assigned_population", ascending=False).reset_index(drop=True)
    assignments = pd.DataFrame(arows)

    f1 = os.path.join(OUT, "gathering_points.csv")
    f2 = os.path.join(OUT, "assignments.csv")
    def _write_csv(df, path):
        """挂载目录不支持覆盖写，先删再写。"""
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass
        df.to_csv(path, index=False, encoding="utf-8-sig")

    _write_csv(shelters, f1)
    _write_csv(assignments, f2)

    print("\n" + "=" * 74)
    print(f"集合点计算结果    成本口径：{method}")
    print("=" * 74)
    print(shelters.to_string(index=False))
    print("-" * 74)
    total = int(ai.sum())
    assigned = shelters.assigned_population.sum()
    print(f"总人口         : {total:,}")
    print(f"已分配         : {int(assigned):,} ({assigned/total*100:.1f}%)")

    if len(assignments):
        # 只统计真实可达的分配。BIG 代表「路网到不了」，需单独规划，
        # 混进统计会让平均值变成 inf —— 这类假数据必须隔离出来。
        BIGV = MAX_TRAVEL_MIN * 3
        reach = assignments[assignments.travel_min < BIGV]
        unreach = assignments[assignments.travel_min >= BIGV]

        if len(reach):
            print(f"平均出行时间   : {reach.travel_min.mean():.1f} 分钟"
                  f"（可达的 {len(reach)} 个点）")
            print(f"最长出行时间   : {reach.travel_min.max():.1f} 分钟")
            print(f"超 {MAX_TRAVEL_MIN} 分钟     : "
                  f"{int((reach.travel_min > MAX_TRAVEL_MIN).sum())} / {len(reach)}")
        if len(unreach):
            print(f"⚠ 路网不可达   : {len(unreach)} 个点 / "
                  f"{int(unreach.population.sum()):,} 人需另行规划"
                  f"（路网数据缺失，非算法问题）")
    print("=" * 74)
    print(f"\n集合点-> {f1}")
    print(f"分配明细 -> {f2}")
    return shelters, assignments


def plot(dem, sites, shelters, assignments, G=None):
    """渲染输出：拆分的多张 PNG + 交互式网页。"""
    import visualize
    import web_view

    log("渲染图片...")
    try:
        visualize.render_all(dem, shelters, assignments, G, OUT,
                             SHELTER_CAPACITY,
                             density_cells=DENSITY_CACHE)
    except Exception as e:
        import traceback
        log(f"  图片渲染失败：{type(e).__name__}: {e}")
        for line in traceback.format_exc().splitlines()[-4:]:
            log("  诊断| " + line.strip())

    log("生成交互式网页...")
    try:
        path = web_view.build(dem, shelters, assignments, G, OUT,
                              SHELTER_CAPACITY,
                              place=PLACE.split(",")[0],
                              cost_label=COST_LABEL)
        log(f"  网页已生成 -> {path}")
        log("  用浏览器打开该文件即可交互查看（无需启动任何服务）")
    except Exception as e:
        import traceback
        log(f"  网页生成失败：{type(e).__name__}: {e}")
        for line in traceback.format_exc().splitlines()[-4:]:
            log("  诊断| " + line.strip())



def validate_config(dem, sites, n_shelters, capacity):
    """
    求解前的可行性校验。

    容量约束 p-median 有个硬性前提：所有点容量之和必须≥ 总人口，
    否则数学上无解（spopt 会抛 SpecificationError，但报错很不直观）。
    这里提前算给人看。
    """
    total_pop = int(dem.population.sum())
    total_cap = n_shelters * capacity

    log(f"[校验] 总人口 {total_pop:,} / 总容量 {total_cap:,} "
        f"({n_shelters} × {capacity:,})")

    if total_cap < total_pop:
        log("[校验] ⚠ 容量不足，当前配置数学上无解")
        need = -(-total_pop // capacity)          # 向上取整
        log(f"      →容量不变至少需要 {need} 个集合点")
        log(f"      → 或保持 {n_shelters} 个点时，每点容量需≥ "
            f"{-(-total_pop // n_shelters):,}")
        return False

    # ⚠️ 另一个 Infeasible 的隐形来源：单个需求点人口 > 单点容量。
    #    p-median 要求一个需求点整体分配给一个设施，拆不开。
    max_demand = int(dem.population.max())
    if max_demand > capacity:
        log(f"[校验] ⚠ 最大需求点人口 {max_demand:,} > 单点容量 {capacity:,}")
        log(f"      →该需求点无法整体分配，模型必然 Infeasible")
        log(f"      →解决：提高容量到≥ {max_demand:,}，或细化需求网格")
        return False

    log(f"[校验] ✓ 可行（容量余量 "
        f"{(total_cap - total_pop):,}，"
        f"{'仅够' if total_cap < total_pop * 1.3 else '较充裕'}）")
    return True


def main():
    global COST_LABEL

    self_check()

    global _GRAPH_CACHE
    dem = build_population()
    sites = build_candidate_sites(dem)

    if not validate_config(dem, sites, N_SHELTERS, SHELTER_CAPACITY):
        raise SystemExit("配置不可行，已中止（见上方校验提示）")

    cost, method, G = build_cost_matrix(dem, sites)
    COST_LABEL = method
    res = solve(dem, sites, cost, N_SHELTERS, SHELTER_CAPACITY)
    shelters, assignments = export(dem, sites, res, cost, method)
    try:
        plot(dem, sites, shelters, assignments, G)
        log(f"路网图已绘制（{G.number_of_edges():,} 路段）" if G
            else "无路网图，仅绘制散点")
    except Exception as e:
        import traceback
        log(f"绘图失败：{type(e).__name__}: {e}")
        for line in traceback.format_exc().splitlines()[-4:]:
            log("诊断| " + line.strip())


if __name__ == "__main__":
    main()