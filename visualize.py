"""
可视化输出
==========
把集合点计算结果渲染成两种形式：
  1. 拆分的多张 PNG（可单独引用/插入报告）
  2. 一个自包含的交互式 HTML（双击即可在浏览器打开，无需起服务）

设计要点：
- 画 15680 条路网线段必须用 LineCollection 批量加，循环 plot() 会极慢
- Docker 挂载的 Windows 目录不支持覆盖写，保存前必须先删目标
"""

import math
import os
import shutil
import tempfile

PALETTE = ["#D85A30", "#1D9E75", "#185FA5", "#BA7517", "#7F77DD", "#D4537E"]

# 人口密度图的网格范围（需与 calc_points.py 的取点范围一致）
LON_MIN, LON_MAX = 115.85, 116.10
LAT_MIN, LAT_MAX = 29.58, 29.75


def _save_fig(fig, out_dir, filename, dpi=130):
    """保存图片。先写 /tmp，再删目标，最后拷贝（绕过挂载盘覆盖写限制）。"""
    tmp = os.path.join(tempfile.gettempdir(), filename)
    fig.savefig(tmp, bbox_inches="tight", facecolor="white", dpi=dpi)
    import matplotlib.pyplot as plt
    plt.close(fig)

    out = os.path.join(out_dir, filename)
    try:
        if os.path.exists(out):
            os.remove(out)
        shutil.copyfile(tmp, out)
        return out
    except Exception as e:
        print(f"    保存 {filename} 失败：{type(e).__name__}: {e}")
        return None


def _road_segments(G):
    """把路网图拆成 (线段列表, 线宽列表)。"""
    if G is None or G.number_of_edges() == 0:
        return [], []
    segs, lens = [], []
    for u, v, d in G.edges(data=True):
        segs.append(((u[0], u[1]), (v[0], v[1])))
        lens.append(d.get("length", 100))
    mx = max(lens) or 1
    widths = [0.35 + 1.5 * (L / mx) for L in lens]
    return segs, widths


def _draw_roads(ax, G, color="#D3D1C7", alpha=0.8, zorder=1):
    """把路网画成底图。必须批量 add_collection，否则 1.5 万条线会卡死。"""
    segs, widths = _road_segments(G)
    if not segs:
        return
    from matplotlib.collections import LineCollection
    ax.add_collection(LineCollection(segs, linewidths=widths,
                                     colors=color, alpha=alpha, zorder=zorder))
    ax.autoscale_view()


def _style(ax, title, T, xlabel=None, ylabel=None):
    ax.set_title(title, pad=12, **T(14, "medium"))
    ax.set_aspect("equal")
    ax.grid(alpha=0.16, ls="--", lw=0.5)
    ax.set_axisbelow(True)
    if xlabel:
        ax.set_xlabel(xlabel, **T(10.5))
    if ylabel:
        ax.set_ylabel(ylabel, **T(10.5))
    for s in ax.spines.values():
        s.set_color("#D3D1C7")


def render_all(dem, shelters, assignments, G, out_dir, capacity,
               density_cells=None):
    """
    生成全部拆分图片 + 总览图。

    返回生成的文件路径列表。
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.font_manager import FontProperties

    # 中文字体：容器是 Linux，不能写死 Windows 路径
    fp = None
    for p in ("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
              "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
              "C:/Windows/Fonts/msyh.ttc"):
        if os.path.exists(p):
            try:
                fp = FontProperties(fname=p)
                break
            except Exception:
                pass

    def T(size, weight="normal"):
        d = {"fontsize": size, "fontweight": weight}
        if fp is not None:
            d["fontproperties"] = fp
        return d

    cen = {int(r.shelter_id): (r.lon, r.lat) for r in shelters.itertuples()}
    order = list(cen.keys())
    color_of = {s: PALETTE[order.index(int(s)) % len(PALETTE)] for s in order}
    ids = shelters.shelter_id.tolist()
    pop = shelters.assigned_population.tolist()

    n_road = (f"{G.number_of_nodes():,} 路口节点 / {G.number_of_edges():,} 路段"
              if G else "无路网")
    total_km = (sum(d.get("length", 0) for _, _, d in G.edges(data=True)) / 1000
                if G else 0)

    def color_at(sid):
        return color_of.get(int(sid), "#888780")

    made = []

    # ---------- 1. 集合点选址与分配 ----------
    fig, ax = plt.subplots(figsize=(13.5, 10), dpi=130)
    _draw_roads(ax, G, color="#DEDBD2", alpha=0.95)
    for sid, grp in assignments.groupby("shelter_id"):
        sx, sy = cen.get(int(sid), (np.nan, np.nan))
        if np.isnan(sx):
            continue
        c = color_at(sid)
        ax.scatter(grp.lon, grp.lat, s=np.clip(grp.population / 5, 8, 480),
                   alpha=0.5, color=c, edgecolors="none", zorder=3)
        for lx, ly in zip(grp.lon, grp.lat):
            ax.plot([lx, sx], [ly, sy], color=c, lw=0.7, alpha=0.42, zorder=4)
    for i, r in enumerate(shelters.itertuples()):
        c = PALETTE[i % len(PALETTE)]
        ax.scatter(r.lon, r.lat, s=760, marker="*", color=c,
                   edgecolors="white", linewidths=2.2, zorder=8)
        ax.annotate(f"G{int(r.shelter_id)}\n{int(r.assigned_population):,}人",
                    (r.lon, r.lat), xytext=(14, 13), textcoords="offset points",
                    color=c, zorder=9,
                    bbox=dict(boxstyle="round,pad=0.36", fc="white",
                              ec=c, lw=1.2, alpha=0.96), **T(9.5, "medium"))
    _style(ax, f"集合点选址与人口分配    路网底图：{n_road}", T)
    made.append(_save_fig(fig, out_dir, "01_集合点选址与分配.png"))

    # ---------- 2. 路网本体 ----------
    fig, ax = plt.subplots(figsize=(10.5, 10), dpi=130)
    _draw_roads(ax, G, color="#A8A69D", alpha=0.95)
    _style(ax, f"九江市机动车路网（总长 {total_km:.0f} km）", T, "经度", "纬度")
    made.append(_save_fig(fig, out_dir, "02_九江市路网.png"))

    # ---------- 3. 人口密度（网格热力） ----------
    fig, ax = plt.subplots(figsize=(11, 9.5), dpi=130)
    _draw_roads(ax, G, color="#E3E0D8", alpha=0.55)

    if density_cells is not None and len(density_cells):
        from matplotlib.colors import LinearSegmentedColormap
        from matplotlib.patches import Rectangle
        cm = LinearSegmentedColormap.from_list(
            "dens", ["#FFF7EC", "#FEE8C8", "#FDBB84", "#E34A33",
                     "#B30000", "#7A0000"])

        vmax = max(c["density"] for c in density_cells) or 1
        dlon = abs(LON_MAX - LON_MIN) / 30
        dlat = abs(LAT_MAX - LAT_MIN) / 28
        mlon = math.cos(math.radians((LAT_MIN + LAT_MAX) / 2)) * 111.32
        mlat = 110.57

        for c in density_cells:
            w_deg = dlon
            h_deg = dlat
            ax.add_patch(Rectangle(
                (c["lon"] - w_deg / 2, c["lat"] - h_deg / 2),
                w_deg, h_deg,
                facecolor=cm(min(c["density"] / vmax, 1.0)),
                edgecolor="none", alpha=0.9, zorder=3))
        ax.set_xlim(LON_MIN, LON_MAX)
        ax.set_ylim(LAT_MIN, LAT_MAX)
        ax.set_title(f"人口密度分布（深红 = 人多）\n"
                     f"OSM 建筑密度估算　峰值 {vmax:,.0f} 人/km²",
                     pad=12, **T(13.5, "medium"))
    else:
        ax.set_title("人口密度分布（无网格数据）", pad=12, **T(13.5, "medium"))
    ax.set_aspect("equal")
    ax.grid(alpha=0.12, ls="--", lw=0.5)
    for s in ax.spines.values():
        s.set_color("#D3D1C7")
    made.append(_save_fig(fig, out_dir, "03_人口密度分布.png"))

    # ---------- 3b. 人口需求点（散点大小=人数） ----------
    fig, ax = plt.subplots(figsize=(10.5, 10), dpi=130)
    _draw_roads(ax, G, color="#E8E5DD", alpha=0.7)
    sc = ax.scatter(dem.lon, dem.lat, s=np.clip(dem.population / 4, 6, 700),
                    alpha=0.55, c=dem.population, cmap="YlOrRd",
                    edgecolors="none", zorder=5)
    cb = fig.colorbar(sc, ax=ax, fraction=0.032, pad=0.02)
    cb.set_label("人口", **T(10.5))
    cb.ax.tick_params(labelsize=9)
    _style(ax, f"人口需求点（{len(dem)} 个人群点，"
               f"总人口 {int(dem.population.sum()):,}）", T)
    made.append(_save_fig(fig, out_dir, "03b_人口需求点.png"))

    # ---------- 4. 承载 vs 容量 ----------
    fig, ax = plt.subplots(figsize=(11, 6.5), dpi=130)
    bars = ax.bar([f"G{i}" for i in ids], pop,
                  color=[color_at(i) for i in ids],
                  edgecolor="white", linewidth=1.6)
    ax.axhline(capacity, color="#E24B4A", ls="--", lw=1.9,
               label=f"容量上限 {capacity:,}")
    for r, v in zip(bars, pop):
        ax.text(r.get_x() + r.get_width() / 2, v + capacity * 0.01,
                f"{int(v):,}\n{v/capacity*100:.0f}%",
                ha="center", va="bottom", **T(9.5))
    ax.set_title("各集合点承载人口 vs 容量上限", pad=12, **T(14, "medium"))
    ax.set_ylabel("分配人口", **T(10.5))
    ax.set_ylim(0, capacity * 1.42)
    ax.legend(fontsize=10, prop=fp)
    ax.grid(alpha=0.18, ls="--", lw=0.6)
    ax.set_axisbelow(True)
    for s in ax.spines.values():
        s.set_color("#D3D1C7")
    made.append(_save_fig(fig, out_dir, "04_承载人口vs容量.png"))

    # ---------- 5. 出行时间分布 ----------
    BIGV = 180
    reach = assignments[assignments.travel_min < BIGV]
    sids = sorted(reach.shelter_id.unique())
    if sids:
        fig, ax = plt.subplots(figsize=(11, 6.5), dpi=130)
        data = [reach[reach.shelter_id == s].travel_min.values for s in sids]
        # matplotlib 3.9 起 boxplot 的 labels 参数改名 tick_labels。
        # 先探测当前版本支持哪个，避免 TypeError。
        import inspect
        _params = inspect.signature(ax.boxplot).parameters
        _kw = {"tick_labels" if "tick_labels" in _params else "labels":
               [f"G{s}" for s in sids]}
        bp = ax.boxplot(data, patch_artist=True, showfliers=False, **_kw)
        for patch, s in zip(bp["boxes"], sids):
            patch.set_facecolor(color_at(s))
            patch.set_alpha(0.55)
        for med in bp["medians"]:
            med.set_color("#2C2C2A")
            med.set_linewidth(2)
        ax.axhline(60, color="#E24B4A", ls="--", lw=1.8,
                   label="可接受上限 60 分钟")
        ax.set_title("各集合点服务范围内的人口出行时间分布",
                     pad=12, **T(14, "medium"))
        ax.set_ylabel("出行时间（分钟）", **T(10.5))
        ax.legend(fontsize=10, prop=fp)
        ax.grid(alpha=0.18, ls="--", lw=0.6, axis="y")
        ax.set_axisbelow(True)
        for s in ax.spines.values():
            s.set_color("#D3D1C7")
        made.append(_save_fig(fig, out_dir, "05_出行时间分布.png"))

    # ---------- 6. 总览（多子图） ----------
    fig = plt.figure(figsize=(17, 11.5), dpi=130)
    gs = fig.add_gridspec(2, 2, height_ratios=[1.45, 1],
                          hspace=0.22, wspace=0.16)
    axa = fig.add_subplot(gs[0, :])
    _draw_roads(axa, G, color="#DEDBD2", alpha=0.95)
    for sid, grp in assignments.groupby("shelter_id"):
        sx, sy = cen.get(int(sid), (np.nan, np.nan))
        if np.isnan(sx):
            continue
        c = color_at(sid)
        axa.scatter(grp.lon, grp.lat, s=np.clip(grp.population / 5, 8, 480),
                    alpha=0.5, color=c, edgecolors="none", zorder=3)
        for lx, ly in zip(grp.lon, grp.lat):
            axa.plot([lx, sx], [ly, sy], color=c, lw=0.7, alpha=0.42, zorder=4)
    for i, r in enumerate(shelters.itertuples()):
        c = PALETTE[i % len(PALETTE)]
        axa.scatter(r.lon, r.lat, s=760, marker="*", color=c,
                    edgecolors="white", linewidths=2.2, zorder=8)
        axa.annotate(f"G{int(r.shelter_id)}\n{int(r.assigned_population):,}人",
                     (r.lon, r.lat), xytext=(14, 13), textcoords="offset points",
                     color=c, zorder=9,
                     bbox=dict(boxstyle="round,pad=0.36", fc="white",
                               ec=c, lw=1.2, alpha=0.96), **T(9.5, "medium"))
    _style(axa, f"集合点选址与人口分配    路网底图：{n_road}", T)

    axb = fig.add_subplot(gs[1, 0])
    _draw_roads(axb, G, color="#B4B2A9", alpha=0.9)
    _style(axb, f"九江市路网（总长 {total_km:.0f} km）", T, "经度", "纬度")

    axc = fig.add_subplot(gs[1, 1])
    bars = axc.bar([f"G{i}" for i in ids], pop,
                   color=[color_at(i) for i in ids],
                   edgecolor="white", linewidth=1.6)
    axc.axhline(capacity, color="#E24B4A", ls="--", lw=1.9,
                label=f"容量上限 {capacity:,}")
    for r, v in zip(bars, pop):
        axc.text(r.get_x() + r.get_width() / 2, v + capacity * 0.01,
                 f"{int(v):,}", ha="center", **T(9.5))
    axc.set_title("各集合点承载人口 vs 容量上限", pad=12, **T(12.5, "medium"))
    axc.set_ylim(0, capacity * 1.42)
    axc.legend(fontsize=9.5, prop=fp)
    axc.grid(alpha=0.16, ls="--", lw=0.5)
    axc.set_axisbelow(True)

    made.append(_save_fig(fig, out_dir, "00_总览.png"))

    return made