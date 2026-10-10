# 从人口分布计算集合点

用**容量约束 p-median** 从人口分布算出集合点位置与人口分配。
全流程 Docker 化，本地不装任何 Python 包。

## 跑起来

```bash
cd gathering_points

# 1. 构建镜像（首次 3-5 分钟，换清华源后约 2 分钟）
docker build -t gathering-solver .

# 2. 运行
MSYS_NO_PATHCONV=1 docker run --rm -v "$(pwd):/work" \
  -e OUTPUT_DIR=/work/output -e ROAD_CACHE=/work/cache \
  -w /work gathering-solver
```

> ⚠️ `MSYS_NO_PATHCONV=1` 必须加。Git Bash 会把 `/work` 转成
> Windows 路径导致挂载失败，这是本项目最常见的坑。

## 输出

| 文件 | 内容 |
|---|---|
| `output/gathering_points.csv` | 每个集合点：坐标、承载人口、容量利用率、平均/最长出行时间 |
| `output/assignments.csv` | 每个人群点分配到哪个集合点、耗时多少分钟 |
| `output/gathering_points.png` | 路网底图 + 分配连线 + 承载分析 |

## 算法

$$\min \sum_{i}\sum_j a_i \cdot d_{ij} \cdot x_{ij}
\qquad \text{s.t.}\ \sum_i a_i x_{ij} \le c_j$$

- $a_i$ = 需求点 i 的人口（**必须作为权重**，否则算的是平均距离而非总压力）
- $d_{ij}$ = 出行时间（**路网时间，不是直线距离**）
- $c_j$ = 集合点 j 的容量上限

库：`pysal/spopt` 的 `PMedian.from_cost_matrix(facility_capacities=...)`，
底层 PuLP/CBC 求解。

## 三级距离降级

每次输出都会**明确标注用的哪一级口径**，避免把近似结果误当精确结果：

1. **OSRM 服务** → 标注「OSRM 路网时间」（需自建服务）
2. **本地路网 Dijkstra** → 标注「本地路网 Dijkstra」（默认）
3. **直线距离 ×2.2** → 标注「直线距离(近似)」（仅流程验证）

## 配置

改 `calc_points.py` 顶部：

```python
PLACE = "Jiujiang, Jiangxi, China"   # 区域（决定拉哪块路网）
LON_MIN, LON_MAX = 115.85, 116.10    # 取点经度范围
LAT_MIN, LAT_MAX = 29.58, 29.75# 取点纬度范围
N_SHELTERS = 8# 设几个集合点
SHELTER_CAPACITY = 12000             # 每个集合点容量上限
N_DEMAND = 120                       # 需求点数量
N_SITES = 40                         # 候选点数量
AVG_SPEED_KMH = 30.0                 # 平均车速假设
```

⚠️ **容量总和必须 ≥ 总人口**，否则数学上无解。脚本启动时会自动校验并给出建议值。

## 换成真实数据

代码里有两处**代理数据**，需替换：

1. **人口分布** → `build_population()`
   当前是「离市中心越近人越密」的公式。函数内留了 WorldPop 栅格的
   读取代码注释，照抄替换即可。

2. **候选集合点** → `build_candidate_sites()`
   当前自动挑「局部人口密度高」的备选。真实场景应替换为
   实际可用场地坐标（学校、医院、广场、停车场）。

## 自检

脚本每次启动先跑两个手算可验证的算例：

- 算例 1：验证**人口权重生效**（权重不生效 → 结论全错）
- 算例 2：验证**容量约束生效**（超载 → 结论无意义）

任一失败即中止。这类「跑通但算错」的失败最难察觉，自检是唯一的防线。

## 文件

- `calc_points.py` — 主流程（自检 → 人口 → 候选点 → 时间矩阵 → 求解 → 输出 → 绘图）
- `route_matrix.py` — 路网获取与时间矩阵（OSRM / Dijkstra / 直线三级降级）
- `road_fetch.py` — Overpass 原始数据拉取与解析
- `Dockerfile` — 含清华 pip/apt 源 + 中文字体
- `cache/raw.json` — 九江路网原始数据（14529 节点 / 15680 路段 / 1316 km）