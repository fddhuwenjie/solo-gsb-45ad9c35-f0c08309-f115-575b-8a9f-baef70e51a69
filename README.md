# 木构件修复验收 API

仅用 **Python 标准库**（`http.server` + `sqlite3`）实现的木构件修复验收服务。
登记固定支承点、承重构件（含最低试载值）、榫卯连接、临时支撑与替换件；
按递增工序号提交拆卸 / 安装 / 试载 / 卸撑 / **支承点停用检修·重新启用**回执；
每个工序重建当前连接图并做结构校验；
支持整单工序的施工预案（预演 + 服务端密钥签发的凭据确认，写事务内验签并复核）；
卸撑完成后可固化不可改写的验收快照。

## 启动

```bash
docker compose up --build
```

服务端口：**8080**（宿主机）→ 容器 8080。SQLite 数据持久化在命名卷 `timber-data`（容器内 `/data/app.db`）。

本地无 Docker 时也可直接运行：`python3 app.py`（监听 8080，数据在 `./data/app.db`；
可用环境变量 `APP_PORT`、`APP_DB` 覆盖）。

## 验收规则

- **工序递增**：`POST /steps` 的 `seq` 必须等于上一条已接受回执 +1，跳号整步拒绝。
- **固定支承点停用检修 / 重新启用**：施工期间可用工序 `deactivate_support` /
  `activate_support` 暂时停用某个固定支承点检修、完工后按工序启用。
  - 停用与启用是**普通工序**：纳入 `/steps` 与施工预案（预演逐步可见、不落库；
    凭据随停启后的状态变化失效；确认失败整单回滚、成功只可提交一次）。
  - 停用后该点**不提供传力路径**：它从连接图的支承根与在位节点集合中剔除，
    挂在它身上的连接与临时支撑沿路不生效；也**不得以它为端点新安装**连接或
    临时支撑（409 `support_out_of_service`）。
  - 停用工序提交后仍按施工中可达性校验：若某在位构件无法经**其他在用支承点**
    获得施工期支承，按原有 409 `unreachable` 语义指出**首个受影响构件**，整步
    不留改动（版本、回执、支承点状态均不变）。
  - 启用后按**当前连接**重新检查路径（挂在该点上、仍在位的连接自动重新参与）。
  - **每次停启都递增连接状态版本**：旧试载一律失效；卸撑与验收快照只认当前
    版本的合格试载，历史快照不改。
  - 检修状态持久化在 SQLite（`supports.in_service`），**旧库启动自动补列、
    既有支承点默认在用**；`GET /state` 的 `supports` 字段保持原样（全部已登记
    支承点 id），另以 `supports_in_service` / `supports_in_maintenance` 显示
    检修状态；`GET /supports` 每行含布尔字段 `in_service`。
- **连接在位判定**：榫卯连接/临时支撑只有**自身在位且两端构件均实际在位**
  （在用固定支承点恒在位，停用检修的支承点不在位）才参与施工与永久连通性判断。
  为尚未安装的替换件预登记的默认在位榫卯只是"允许端点组合"，在替换件实际安装前
  沿路不生效；挂在停用检修支承点身上的连接/支撑同样沿路不生效。该规则在重建
  连接图时实时计算，数据库中已登记的同类记录同样遵循，无需迁移。
- **施工中可达性**：每个改变连接图的工序提交后、以及试载时重建连接图——每个在位
  承重构件必须能经**在位连接或临时支撑**抵达某个固定支承点，否则整步回滚。
- **卸撑**：卸除临时支撑后，每个在位承重构件必须**仅凭永久连接**抵达支承点，且每个构件
  都有在**当前连接状态版本**下通过、载荷不低于其 `min_load` 的试载记录。
- **连接状态版本**：永久连接图每次变化（装/拆连接、拆/装构件、支承点停用/启用）
  版本 +1；试载记录当时版本，图一变旧试载即失效。临时支撑的搭设/卸除不影响版本。
- **改接限制**：安装连接只允许使用事先登记的端点组合（`POST /connections` 登记的 id）。
- **整步拒绝**：跳号、端点不在位、可达性或卸撑条件不满足时返回 409，
  响应含 `first_affected_component`（首个受影响构件），已有回执与状态不变。
- **施工预案（预演 / 确认）**：`POST /plan/preview` 按现有单步字段提交从末条回执后
  连续编号的工序清单（`steps` 数组，各项字段与 `POST /steps` 相同；`seq` 可省略，
  由服务端从末条回执后连续补齐，显式给出则必须吻合，跳号整单拒绝）。预演在同一
  事务内逐步套用现有传力路径与试载规则——后一步能看到前一步带来的支撑、连接版本
  与试载变化；**仅整单预演成功后**才返回每步预计结果及确认凭据 `token`
  （`planv2-` 前缀的 HMAC-SHA256，由持久保存在 SQLite 中的服务端密钥保护，密钥
  永不下发、客户端无法自行算出；旧库首次启动自动生成密钥），失败返回原有错误并附
  `failed_step` 指出出错工序；无论成败预演都不留下改动。`POST /plan/confirm`
  携带原清单与 `token`，在一次写事务内先验签再复核整单：伪造凭据、旧式无密钥摘要
  （旧版 `plan-` 前缀的裸 SHA256）、清单改动、预演后登记/工序/试载状态变化或凭据
  重复使用，一律 409 `plan_stale` 拒绝且不写入任何内容；校验通过后全部成功才保存
  并返回按序回执，任一步失败指出该步并回滚整单新增的工序、连接版本、临时支撑状态
  及试载（凭据消耗登记一并回滚，故可凭原凭据修正后重试；已成功落库的凭据不可再用），
  旧回执与验收快照不变。密钥随数据卷持久化，服务重启后状态未变的预案仍可确认。
- **快照**：所有临时支撑卸除且校验通过后可 `POST /snapshots` 固化；快照只增不改，
  后续修复工序不会改写旧快照。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/supports` | 登记固定支承点 `{id, name}` |
| POST | `/components` | 登记承重构件 `{id, name, min_load}`（初始在位） |
| POST | `/replacements` | 登记替换件 `{id, name, min_load}`（初始不在位） |
| POST | `/connections` | 登记榫卯连接 `{id, a, b, in_place?}`（默认 true；改接预留组合传 false） |
| POST | `/temp-supports` | 登记临时支撑 `{id, a, b, in_place?}`（默认 false） |
| POST | `/steps` | 提交工序回执（见下；含支承点停用/启用） |
| POST | `/plan/preview` | 预演工序清单：整单成功后签发服务端密钥保护的凭据（不留改动） |
| POST | `/plan/confirm` | 凭据确认：写事务内验签并复核整单，凭据整单仅可用一次 |
| GET | `/steps`、`/steps/{seq}` | 查询各工序回执与结果 |
| GET | `/state` | 当前连接图、各构件可达性与最近试载 |
| POST | `/snapshots` | 卸撑完成后固化验收快照 |
| GET | `/snapshots`、`/snapshots/{id}` | 查询快照 |
| GET | `/supports` `/components` `/replacements` `/connections` `/temp-supports` | 登记簿查询 |
| GET | `/health` | 健康检查 |

### 工序回执格式（`POST /steps`）

```jsonc
// 搭设临时支撑
{"seq": 1, "action": "install", "target": "temp_support", "id": "T1"}
// 拆卸构件（联动卸下依附的在位连接与临时支撑）
{"seq": 2, "action": "disassemble", "target": "component", "id": "C2"}
// 拆卸单条连接
{"seq": 2, "action": "disassemble", "target": "connection", "id": "J2"}
// 安装替换件，并同步安装其已登记连接（端点组合须事先登记）
{"seq": 3, "action": "install", "target": "replacement", "id": "R2", "connections": ["J3", "J4"]}
// 试载：记录载荷，是否通过 = load >= 构件 min_load
{"seq": 4, "action": "test_load", "component": "C1", "load": 12}
// 卸撑
{"seq": 6, "action": "remove_support", "id": "T1"}
// 固定支承点停用检修（停用后该点不参与传力，须保证各构件仍可达其他在用支承点）
{"seq": 7, "action": "deactivate_support", "id": "S1"}
// 检修完成后重新启用（按当前连接复验路径；停启均换版，旧试载失效）
{"seq": 8, "action": "activate_support", "id": "S1"}
```

## 可执行调用示例

启动服务后执行完整演示（登记 → 三类拒绝 → 正常工序 → 卸撑 → 快照 → 查询）：

```bash
./demo.sh
```

或逐条手动执行：

```bash
B=http://localhost:8080

# 1) 登记
curl -X POST $B/supports      -d '{"id":"S1","name":"北柱础"}'
curl -X POST $B/supports      -d '{"id":"S2","name":"南台基"}'
curl -X POST $B/components    -d '{"id":"C1","name":"前金柱","min_load":10}'
curl -X POST $B/components    -d '{"id":"C2","name":"后金柱","min_load":8}'
curl -X POST $B/replacements  -d '{"id":"R2","name":"替换后金柱","min_load":8}'
curl -X POST $B/connections   -d '{"id":"J1","a":"S1","b":"C1"}'
curl -X POST $B/connections   -d '{"id":"J2","a":"C1","b":"C2"}'
curl -X POST $B/connections   -d '{"id":"J3","a":"C1","b":"R2","in_place":false}'
curl -X POST $B/connections   -d '{"id":"J4","a":"S2","b":"R2","in_place":false}'
curl -X POST $B/temp-supports -d '{"id":"T1","a":"S2","b":"C1"}'

# 2) 拒绝示例
curl -X POST $B/steps -d '{"seq":5,"action":"install","target":"temp_support","id":"T1"}'
#   → 409 sequence_gap（工序跳号，期望 1）
curl -X POST $B/steps -d '{"seq":1,"action":"install","target":"connection","id":"J3"}'
#   → 409 endpoint_not_in_place，first_affected_component=R2（端点不在位）
curl -X POST $B/steps -d '{"seq":1,"action":"disassemble","target":"connection","id":"J1"}'
#   → 409 unreachable，first_affected_component=C1（拆后 C1 失去支承路径）

# 3) 正常工序
curl -X POST $B/steps -d '{"seq":1,"action":"install","target":"temp_support","id":"T1"}'
curl -X POST $B/steps -d '{"seq":2,"action":"disassemble","target":"component","id":"C2"}'
curl -X POST $B/steps -d '{"seq":3,"action":"install","target":"replacement","id":"R2","connections":["J3","J4"]}'

# 4) 卸撑条件不满足
curl -X POST $B/steps -d '{"seq":4,"action":"remove_support","id":"T1"}'
#   → 409 test_load_missing，first_affected_component=C1（当前连接状态下无合格试载）

# 5) 试载并卸撑
curl -X POST $B/steps -d '{"seq":4,"action":"test_load","component":"C1","load":12}'
curl -X POST $B/steps -d '{"seq":5,"action":"test_load","component":"R2","load":9.5}'
curl -X POST $B/steps -d '{"seq":6,"action":"remove_support","id":"T1"}'

# 6) 固化验收快照（此后继续修复不会改写快照 1）
curl -X POST $B/snapshots -d '{}'

# 7) 查询
curl $B/steps        # 各工序回执与结果
curl $B/steps/4      # 单条工序
curl $B/state        # 当前连接图与各构件状态
curl $B/snapshots/1  # 验收快照
```

## 施工预案：预演、伪造拒绝与确认

整单工序先预演、后确认：预演在同一事务内逐步套用现有传力路径与试载规则
（后一步能看到前一步带来的支撑、连接版本与试载变化），**仅整单预演成功后**
才签发确认凭据；凭据为服务端持久密钥保护的 HMAC（`planv2-` 前缀），绑定
清单与预演当时的登记、工序、试载状态，密钥永不下发，不经过成功预演不可能
得到有效凭据。确认在一次写事务内先验签再复核整单：伪造凭据、旧式无密钥
摘要、清单改动、预演后状态变化或凭据重用一律 409 拒绝；全部成功才保存
按序回执，任一步失败整单回滚。

一键执行（含断言，请在空库上跑，例如 `docker compose up --build` 后的全新卷）：

```bash
./demo_plan.sh
```

或逐条手动执行（空库；清单存入 `STEPS` 变量以便预演、确认复用同一份）：

```bash
B=http://localhost:8080

# 0) 登记
curl -X POST $B/supports      -d '{"id":"PS1","name":"西柱础"}'
curl -X POST $B/supports      -d '{"id":"PS2","name":"东台基"}'
curl -X POST $B/components    -d '{"id":"PC1","name":"西金柱","min_load":10}'
curl -X POST $B/components    -d '{"id":"PC2","name":"东金柱","min_load":8}'
curl -X POST $B/replacements  -d '{"id":"PR2","name":"替换东金柱","min_load":8}'
curl -X POST $B/connections   -d '{"id":"PJ1","a":"PS1","b":"PC1"}'
curl -X POST $B/connections   -d '{"id":"PJ2","a":"PC1","b":"PC2"}'
curl -X POST $B/connections   -d '{"id":"PJ3","a":"PC1","b":"PR2","in_place":false}'
curl -X POST $B/connections   -d '{"id":"PJ4","a":"PS2","b":"PR2","in_place":false}'
curl -X POST $B/temp-supports -d '{"id":"PT1","a":"PS2","b":"PC1"}'

# 1) 失败预演：卸撑前未试载 → 409 指出出错工序，不签发凭据，预演不留下改动
curl -X POST $B/plan/preview -d '{"steps":[
  {"action":"install","target":"temp_support","id":"PT1"},
  {"action":"remove_support","id":"PT1"}
]}'
#   → 409 test_load_missing，first_affected_component=PC1，
#     failed_step={"index":1,"seq":2,"action":"remove_support"}
curl $B/steps    # 仍为 []：预演不留下任何回执与状态变化

# 2) 不预演直接确认（含自行伪造的凭据）→ 一律 409 plan_stale，不写入回执
#    先定义一份与末条回执连续编号的清单（各字段与 POST /steps 相同）
STEPS='[
  {"seq":1,"action":"install","target":"temp_support","id":"PT1"},
  {"seq":2,"action":"disassemble","target":"component","id":"PC2"},
  {"seq":3,"action":"install","target":"replacement","id":"PR2","connections":["PJ3","PJ4"]},
  {"seq":4,"action":"test_load","component":"PC1","load":12},
  {"seq":5,"action":"test_load","component":"PR2","load":9.5},
  {"seq":6,"action":"remove_support","id":"PT1"}
]'
#    2a. 随便捏造一个 planv2- 凭据（无服务端密钥，HMAC 不可能碰巧相符）
curl -X POST $B/plan/confirm -d "{\"steps\":$STEPS,\"token\":\"planv2-$(printf '%064d' 0)\"}"
#   → 409 plan_stale
#    2b. 旧式无密钥摘要（旧版本实现把清单+状态做裸 SHA256 当凭据，客户端可自行
#        算出）；现版本必须持有服务端密钥，旧式 plan- 摘要无法通过 HMAC 校验
curl -X POST $B/plan/confirm -d "{\"steps\":$STEPS,\"token\":\"plan-$(printf '%064d' 0)\"}"
#   → 409 plan_stale
curl $B/steps    # 仍为 []：伪造/旧式凭据均未写入任何工序

# 3) 成功预演：仅整单成功后签发 planv2- 凭据；清单从末条回执后连续编号
#    （seq 可省略由服务端补齐；显式给出须吻合，跳号 409 sequence_gap）
curl -X POST $B/plan/preview -d "{\"steps\":$STEPS}"
#   → 200 {"ok":true,"token":"planv2-…","base_seq":0,
#          "steps":[每步 seq/action/payload/预计 result],
#          "resulting":{"last_seq":6,"version":2}}
#    试载预计落在 v2：前两步装/拆已把连接状态版本从 v0 递进
#    保存凭据（需 python3；也可手工复制响应中的 token）：
TOKEN=$(curl -s -X POST $B/plan/preview -d "{\"steps\":$STEPS}" \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')

# 4) 过期拒绝：预演后登记（或工序）发生变化，原凭据失效
curl -X POST $B/temp-supports -d '{"id":"PT2","a":"PS1","b":"PC2"}'
curl -X POST $B/plan/confirm -d "{\"steps\":$STEPS,\"token\":\"$TOKEN\"}"
#   → 409 plan_stale：凭据无效或与清单/当前登记、工序状态不符，请重新预演
#   不写入任何工序；改动清单内容而沿用旧凭据同样 409

# 5) 重新预演取得新凭据后确认：写事务内验签并复核整单
TOKEN=$(curl -s -X POST $B/plan/preview -d "{\"steps\":$STEPS}" \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')
curl -X POST $B/plan/confirm -d "{\"steps\":$STEPS,\"token\":\"$TOKEN\"}"
#   → 201 {"ok":true,"receipts":[seq 1..6 按序回执，含 created_at]}
#   任一步失败则指出 failed_step 并回滚整单新增的工序、连接版本、
#   临时支撑状态及试载（凭据消耗一并回滚，可凭同一凭据修正后重试），
#   旧回执与验收快照不变

# 6) 凭据整单仅可用一次：状态已随首次确认改变，且凭据已登记消耗，重用即 409
curl -X POST $B/plan/confirm -d "{\"steps\":$STEPS,\"token\":\"$TOKEN\"}"
#   → 409 plan_stale，不重复写入回执

# 7) 密钥随 SQLite 持久化：重启服务（docker compose restart）后，对"状态未变"
#    的新预案重新预演取得凭据仍可确认；重启前已成功使用的凭据不可再用。
#    确认后流程如常：卸撑完成可固化快照，/steps 单步从 seq 7 继续
curl -X POST $B/snapshots -d '{}'
curl -X POST $B/steps -d '{"seq":7,"action":"test_load","component":"PC1","load":11}'
```

## 故障与修复示例：未安装替换件不得充当传力路径

登记固定支承点 `S`、在位承重梁 `C`、**未安装**替换件 `R`，以及 `S—R`、`R—C`
两条默认在位榫卯（只是预留端点组合）。再搭设 `S—C` 临时支撑并完成 `C` 的
合格试载：此时 `C` 唯一的"永久路径"穿过未安装的 `R`，卸撑和快照都必须拒绝；
待 `R` 按原工序安装接入、重新试载合格后，才允许卸撑与验收。

一键执行（含 409 断言，请在空库上跑，例如 `docker compose up --build` 后的全新卷）：

```bash
./demo_uninstalled_replacement.sh
```

逐条手动执行：

```bash
B=http://localhost:8080

# 1) 登记：S、C 在位；R 未安装；S—R、R—C 为默认在位榫卯
curl -X POST $B/supports     -d '{"id":"S","name":"柱础"}'
curl -X POST $B/components   -d '{"id":"C","name":"承重梁","min_load":10}'
curl -X POST $B/replacements -d '{"id":"R","name":"替换件","min_load":10}'
curl -X POST $B/connections  -d '{"id":"JSR","a":"S","b":"R"}'
curl -X POST $B/connections  -d '{"id":"JRC","a":"R","b":"C"}'

# 2) /state：两条榫卯搭在未安装的 R 上而沿路失效
#    C 的 reachable=false、permanent_reachable=false（R 行 in_place=false，可达性为 null）
curl $B/state

# 3) 搭设 S—C 临时支撑并完成 C 的合格试载
curl -X POST $B/temp-supports -d '{"id":"T","a":"S","b":"C"}'
curl -X POST $B/steps -d '{"seq":1,"action":"install","target":"temp_support","id":"T"}'
curl -X POST $B/steps -d '{"seq":2,"action":"test_load","component":"C","load":12}'

# 4) 故障：卸撑后 C 的唯一永久路径穿过未安装的 R → 409，首个受影响构件 C
curl -X POST $B/steps -d '{"seq":3,"action":"remove_support","id":"T"}'
#   → 409 support_unreachable
#     {"error":"support_unreachable",
#      "message":"卸撑后构件 C 无法仅凭永久连接抵达固定支承点，整步拒绝",
#      "first_affected_component":"C"}
#   整步回滚：无 seq 3 回执，last_seq 仍为 2，临时支撑 T 仍在位

# 5) 支撑尚在位时不能固化快照
curl -X POST $B/snapshots -d '{}'
#   → 409 shoring_incomplete，temp_support=T

#    对照：若任何临时支撑都不存在（如未搭设或另一个空库），同样不得固化虚假路径
#   → 409 support_unreachable，first_affected_component=C

# 6) 修复：按原工序安装 R；默认在位的 JSR/JRC 随 R 在位自动接入，永久路径打通
curl -X POST $B/steps -d '{"seq":3,"action":"install","target":"replacement","id":"R"}'

# 7) 连接状态版本因安装变为 v1，旧试载失效；对 C、R 重新合格试载
curl -X POST $B/steps -d '{"seq":4,"action":"test_load","component":"C","load":11}'
curl -X POST $B/steps -d '{"seq":5,"action":"test_load","component":"R","load":10}'

# 8) 合格试载支持卸撑与验收
curl -X POST $B/steps      -d '{"seq":6,"action":"remove_support","id":"T"}'   # 201
curl -X POST $B/snapshots  -d '{}'                                             # 201 快照 1
curl $B/snapshots/1
#   快照中 C、R 均带 v1 合格试载，connections_in_place 含 JSR、JRC
#   此后继续修复只增不改，快照 1 保持原样
```

## 固定支承点检修：停用受阻、绕行停用与启用后复验

施工期间可暂时停用某个固定支承点检修，完工后按工序启用。停启是普通工序
（`deactivate_support` / `activate_support`，只有 `id` 字段），同样遵守工序
递增、整步回滚与预案预演/确认规则；停用后该点不提供传力路径、其上连接/支撑
沿路失效，也不得以它为端点新装连接或支撑；每次停启都换版，旧试载失效。

一键执行（含 34 条断言，请在空库上跑，例如 `docker compose up --build` 后的全新卷）：

```bash
./demo_support_maintenance.sh
```

逐条手动执行：

```bash
B=http://localhost:8080

# 0) 登记：两支承点、两根梁；MC1 同时连 MS1/MS2，MC2 只连 MS1；
#    MJ4 是预登记的改接组合（MC2—MS2），供绕行时安装
curl -X POST $B/supports      -d '{"id":"MS1","name":"北柱础"}'
curl -X POST $B/supports      -d '{"id":"MS2","name":"南台基"}'
curl -X POST $B/components    -d '{"id":"MC1","name":"主梁","min_load":10}'
curl -X POST $B/components    -d '{"id":"MC2","name":"次梁","min_load":8}'
curl -X POST $B/connections   -d '{"id":"MJ1","a":"MS1","b":"MC1"}'
curl -X POST $B/connections   -d '{"id":"MJ2","a":"MS2","b":"MC1"}'
curl -X POST $B/connections   -d '{"id":"MJ3","a":"MS1","b":"MC2"}'
curl -X POST $B/connections   -d '{"id":"MJ4","a":"MC2","b":"MS2","in_place":false}'
curl -X POST $B/steps -d '{"seq":1,"action":"test_load","component":"MC1","load":11}'

# 1) 停用受阻：MC2 只能经 MS1 获得施工期支承 → 409 指出首个受影响构件 MC2，
#    整步不留改动（无回执、版本仍为 v0、MS1 仍在用）
curl -X POST $B/steps -d '{"seq":2,"action":"deactivate_support","id":"MS1"}'
#   → 409 unreachable
#     {"error":"unreachable",
#      "message":"本工序后构件 MC2 无法经在位连接或临时支撑抵达固定支承点，整步拒绝",
#      "first_affected_component":"MC2"}
curl $B/state
#   → "version":0、"last_seq":1、"supports_in_maintenance":[]

# 2) 借助其他支承点绕行：先按原工序把 MC2 改接到 MS2（安装已登记组合 MJ4），
#    再停用 MS1；此时 MC1/MC2 均经 MS2 获得支承
curl -X POST $B/steps -d '{"seq":2,"action":"install","target":"connection","id":"MJ4"}'
#   → 201，版本 → v1
curl -X POST $B/steps -d '{"seq":3,"action":"deactivate_support","id":"MS1"}'
#   → 201 result={"support":"MS1","in_service":false,"maintenance":true,"version":2}
curl $B/state
#   → "supports":["MS1","MS2"]（原字段保留，为全部已登记支承点）
#     "supports_in_service":["MS2"]
#     "supports_in_maintenance":[{"id":"MS1","name":"北柱础","in_service":false}]
#     MJ1/MJ3 虽仍在登记簿在位，但端点 MS1 已停用，沿路不生效

# 3) 停用期间不得以 MS1 为端点新安装连接或临时支撑
curl -X POST $B/temp-supports -d '{"id":"MT9","a":"MS1","b":"MC1"}'
curl -X POST $B/steps -d '{"seq":4,"action":"install","target":"temp_support","id":"MT9"}'
#   → 409 support_out_of_service，first_affected_component=MS1，整步拒绝
curl -X POST $B/steps -d '{"seq":4,"action":"deactivate_support","id":"MS1"}'
#   → 409（重复停用）

# 4) 每次停启旧试载失效：seq 1 的 v0 试载不被卸撑/快照承认
curl -X POST $B/snapshots -d '{}'
#   → 409 test_load_missing，first_affected_component=MC1（当前版本 v2 无合格试载）

# 5) 启用后复验：按当前连接重新检查路径，并再换一版；之后须在新版本重新试载
curl -X POST $B/steps -d '{"seq":4,"action":"activate_support","id":"MS1"}'
#   → 201 result={"support":"MS1","in_service":true,"maintenance":false,"version":3}
#     MJ1/MJ3 随 MS1 启用自动重新参与路径
curl -X POST $B/steps -d '{"seq":5,"action":"test_load","component":"MC1","load":12}'
curl -X POST $B/steps -d '{"seq":6,"action":"test_load","component":"MC2","load":9}'
curl -X POST $B/snapshots -d '{}'          # → 201 快照 1（v3）

# 6) 历史快照不改：之后再次停用（v4）不影响快照 1
curl -X POST $B/steps -d '{"seq":7,"action":"deactivate_support","id":"MS1"}'
curl $B/snapshots/1
#   → 仍为 "version":3、"last_seq":6、"supports":["MS1","MS2"]、
#     "supports_in_maintenance":[]
```

停启同样纳入施工预案：预演清单中可直接包含 `deactivate_support` /
`activate_support`，逐步套用路径与换版规则（后一步能看到前一步的停启影响），
失败附 `failed_step` 且不落库；预演后任何一次停启改变状态，原凭据即 409
`plan_stale`；确认在写事务内验签并复核整单，失败整单回滚、成功仅可提交一次：

```bash
# 当前 MS1 停用中（见上，演示流程此时 last_seq=9）。预演“再停用唯一在用根
# MS2”必失败、不落库（seq 由服务端从末条回执后连续补齐）
curl -X POST $B/plan/preview -d '{"steps":[
  {"action":"deactivate_support","id":"MS2"}
]}'
#   → 409 unreachable，first_affected_component=MC1，
#     failed_step={"index":0,"seq":10,"action":"deactivate_support"}

# 预演“启用 MS1 + 当前版本重新试载”，成功后取得 planv2- 凭据
STEPS='[
  {"action":"activate_support","id":"MS1"},
  {"action":"test_load","component":"MC1","load":14},
  {"action":"test_load","component":"MC2","load":11}
]'
TOKEN=$(curl -s -X POST $B/plan/preview -d "{\"steps\":$STEPS}" \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')

# 预演后直接执行一次启用（停启改变状态）→ 原凭据失效
curl -X POST $B/steps -d '{"seq":10,"action":"activate_support","id":"MS1"}'
curl -X POST $B/plan/confirm -d "{\"steps\":$STEPS,\"token\":\"$TOKEN\"}"
#   → 409 plan_stale；需按新状态重新预演后再确认（成功仅可提交一次）
```

旧库兼容：升级后首次启动自动为 `supports` 补列 `in_service`，既有支承点默认
在用，历史连接状态版本与既有快照保持不变；`GET /supports` 返回行新增
`in_service` 布尔字段，`GET /state` 在保留 `supports` 原字段的同时给出
`supports_in_service` 与 `supports_in_maintenance`。

## 错误响应

统一 JSON：`{"error": <code>, "message": ..., ...}`。工序拒绝一律 409 且状态不变：

| code | 含义 |
|---|---|
| `sequence_gap` | 工序跳号（附 `expected`/`got`） |
| `not_registered` | 连接/支撑/构件未事先登记 |
| `endpoint_not_in_place` | 端点不在位（附 `first_affected_component`） |
| `support_out_of_service` | 固定支承点已停用检修：重复停用，或以其为端点新安装连接/临时支撑 |
| `not_in_place` / `already_in_place` | 拆卸/安装对象状态不符（含重复启用在用支承点） |
| `unreachable` | 工序后（含试载时）构件无法经在位连接或临时支撑抵达固定支承点 |
| `support_unreachable` | 卸撑后构件无法仅凭永久连接抵达支承点 |
| `test_load_missing` | 构件缺少当前连接状态下通过的合格试载 |
| `shoring_incomplete` | 临时支撑未卸完，不能固化快照 |
| `plan_stale` | 未经过成功预演、凭据系伪造或旧式无密钥摘要、预演后登记/工序/试载状态已变化、清单与凭据不符，或凭据已使用过，需重新预演 |

预案相关响应补充：预演/确认中某步失败时，响应沿用该步原有错误码与状态码，
并附 `failed_step`（`index` 为清单内 0 基序号，`seq` 为工序号，`action` 为动作）。

## 项目结构

```
app.py              # 服务全部实现（标准库 HTTP + SQLite）
Dockerfile          # python:3.12-slim 单容器镜像
docker-compose.yml  # 单条命令启动，卷持久化
demo.sh             # 端到端演示脚本
demo_uninstalled_replacement.sh  # 未安装替换件虚假路径：故障复现与修复演示
demo_support_maintenance.sh      # 支承点检修：停用受阻/绕行停用/启用后复验演示
demo_plan.sh        # 施工预案演示：预演 → 过期拒绝 → 一次写事务确认
README.md
```
