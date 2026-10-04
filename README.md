# 微孔板配液规划服务（Microplate Liquid-Handling Planner）

面向实验室微孔板配液的 HTTP 服务。输入母液、稀释液、移液器限制与各目标孔的
终体积/终浓度，服务输出一份**可执行的转移序列**（每步来源、目的、体积），
并给出每个孔精确达成的最终浓度。体积一律为整数纳升（nL），浓度一律为有理数
（精确运算，无浮点误差）。

- 技术栈：Python 3.12 · FastAPI · PostgreSQL（SQLAlchemy）
- 允许中间液容器逐级稀释；容器间转移不成环；目标孔不作为来源
- 每步转移满足移液器单次最小/最大体积与步进；来源不透支、目的不溢出
- 混合按溶质质量守恒精确计算；所有目标体积与浓度**精确**达成
- 无解时指出受阻目标与约束，**不返回部分计划**
- 相同请求号 + 相同内容重试返回原冻结计划；同号异内容返回 409 冲突
- 非法数值或非法容器引用一律明确拒绝（400）

## 快速启动

```bash
docker compose up --build
```

启动后：

| 项 | 地址 |
| --- | --- |
| 服务地址 | `http://localhost:8000` |
| 交互式 API 文档 | `http://localhost:8000/docs` |
| 健康检查 | `http://localhost:8000/healthz` |
| PostgreSQL | `localhost:5432`（用户/密码/库名均为 `planner`） |

应用镜像构建时安装依赖（`requirements.txt`），应用启动时自动等待数据库并
建表（`plans` 表），无需手工迁移。

可执行调用示例（服务运行后）：

```bash
bash examples/curl_examples.sh
```

## 配置项（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql+psycopg2://planner:planner@localhost:5432/planner` | SQLAlchemy 连接串；Compose 中指向 `db` 服务 |
| `DB_CONNECT_RETRIES` | `30` | 启动时等待数据库的重试次数 |
| `DB_CONNECT_DELAY_SECONDS` | `1` | 每次重试间隔（秒） |

Compose 默认把服务映射到宿主机 `8000` 端口，数据库映射到 `5432` 端口，
可在 `docker-compose.yml` 的 `ports` 中调整。

## API

### `POST /v1/plans`

请求体：

```json
{
  "request_id": "demo-direct-001",
  "stock":    {"concentration": "1", "volume_nl": 100000},
  "diluent":  {"volume_nl": 1000000},
  "pipette":  {"min_volume_nl": 1000, "max_volume_nl": 10000, "step_nl": 100},
  "well_capacity_nl": 50000,
  "intermediates": [{"id": "C1", "capacity_nl": 200000}],
  "targets": [
    {"well_id": "A1", "volume_nl": 20000, "concentration": "1/2"},
    {"well_id": "A2", "volume_nl": 16000, "concentration": "1/4", "via": "C1"}
  ]
}
```

字段约定：

- **浓度**：整数、十进制（`0.25`）或分数（`"1/4"`）字符串均可，内部按
  有理数精确处理；浓度只需单位一致（比值决定稀释关系）。
- **体积**：全部为整数纳升。`pipette` 的每次转移须为 `step_nl` 的整数倍
  且落在 `[min_volume_nl, max_volume_nl]` 内；更大的总量由服务自动拆成
  多步合法转移。
- `well_capacity_nl` 为全局孔容量；目标孔可用 `capacity_nl` 单独覆盖。
- `intermediates` 为有限个有容量上限的中间液容器（可缺省为空）。
- 目标孔的 `via` 可指定必须经由某个中间液容器；引用不存在的容器会被
  400 拒绝。

**求解成功**（HTTP 200，`status: "solved"`）：

```json
{
  "request_id": "demo-direct-001",
  "status": "solved",
  "transfers": [
    {"step": 1, "source": "stock",   "destination": "well:A1", "volume_nl": 10000},
    {"step": 2, "source": "diluent", "destination": "well:A1", "volume_nl": 10000}
  ],
  "intermediates": [
    {"id": "C1", "concentration": "1/200", "prepared_volume_nl": 200000,
     "used_volume_nl": 1000, "leftover_volume_nl": 199000}
  ],
  "wells": [
    {"well_id": "A1", "final_volume_nl": 20000, "final_concentration": "1/2"}
  ],
  "usage": {"stock_used_nl": 11000, "stock_remaining_nl": 89000,
            "diluent_used_nl": 209000, "diluent_remaining_nl": 791000}
}
```

`transfers` 按执行顺序排列；`source`/`destination` 取值为 `stock`、
`diluent`、`intermediate:<容器id>`、`well:<孔id>`。每份计划返回前都会由
内置校验器按请求约束完整仿真复核（移液限制、不透支、不溢出、不成环、
目标精确达成）。

**无解**（HTTP 200，`status: "infeasible"`）：不返回任何部分计划，只给出
受阻目标与约束：

```json
{
  "request_id": "demo-infeasible-001",
  "status": "infeasible",
  "blocked_targets": [
    {"well_id": "A1", "reasons": ["target concentration 1/5 exceeds stock concentration 1/10"]},
    {"well_id": "A2", "reasons": ["required diluent volume 500 nL is not pipettable ..."]}
  ],
  "constraints": []
}
```

**幂等语义**：以 `request_id` + 规范化内容哈希为幂等键。

- 相同 `request_id` + 相同内容（含等价表示，如 `0.5` 与 `"1/2"`、目标顺序
  不同）重试 → 返回原冻结计划（含无解结论），HTTP 200；
- 相同 `request_id` + 不同内容 → HTTP 409 `request_id_conflict`；
- 非法数值/引用 → HTTP 400 `invalid_request` 并附具体原因。

### `GET /v1/plans/{request_id}`

返回该请求号冻结的响应（与 POST 结果一致）；不存在返回 404。

### `GET /healthz`

数据库连通性检查。

## 本地开发

```bash
pip install -r requirements-dev.txt
DATABASE_URL=sqlite:///./dev.db uvicorn app.main:app --reload --port 8000
python3 -m pytest tests/ -q
```

## 设计说明与边界

- **精确性**：体积为整数 nL，浓度为 `Fraction` 有理数；中间液浓度、孔终
  浓度均按溶质质量守恒精确计算，校验器逐步仿真复核后才返回计划。
- **求解策略**：可直接两源混合（母液+稀释液直接入孔）的目标直接规划；
  其余目标按候选稀释倍数分组（精确集合覆盖，容器数最小化），在容器中
  制备 `1/d` 中间液后分液；单步稀释倍数受容器容量限制时自动降级为逐级
  稀释（深度 ≤ 4，且受容器数量限制）。候选枚举与分组搜索是有界的，极端
  参数下可能把实际可解的实例判为无解——此时会明确给出受阻原因。
- **不成环**：计划结构为 母液/稀释液 → 中间液（链式）→ 目标孔，校验器
  对中间液转移图做拓扑检查，目标孔永不为源。
