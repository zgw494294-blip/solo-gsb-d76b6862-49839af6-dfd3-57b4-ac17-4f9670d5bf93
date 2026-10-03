# 离线对白字幕分轨 HTTP API

用 Python + FastAPI 实现的离线服务：给定 2～3 条屏幕轨道和 1～12 条对白字幕，为每条字幕选择一条轨道，满足：

- **轨道互斥**：字幕占用左闭右开时间区间 `[start_ms, end_ms)`；时间重叠的字幕不能占同一轨；端点相接（前一条的 `end_ms` == 后一条的 `start_ms`）可以复用同一轨。
- **锁定**：字幕可指定 `locked_track`，强制占用该轨道（轨道号从 1 开始）。
- **优化目标**：同一说话人按时间相邻的两条字幕，换轨次数总和最小。
- **并列裁决**：换轨次数相同时，取“按输入顺序排列的轨道号序列”字典序最小的方案。
- 同一说话人的字幕时间不得重叠（属于输入合法性约束，非法返回 422）。
- 分轨结果可作为**场景（scene）**按 `scene_id` 保存为版本化修订：新场景以 `expected_revision: 0` 创建（成功后版本 1），后续修订须带当前版本；版本不符返回 409。数据落在本地 SQLite，容器重启后仍可读取。
- 已保存场景的指定时段可**另存为片段（clip）**：`POST /scenes/{id}/clip` 把 `[from_ms, to_ms)` 内相交字幕裁出并减去 `from_ms`，时间轴从 0 毫秒起，源分轨写入各条 `locked_track` 以保持原画面轨道，片段保存为 revision 1 的独立新场景；裁剪数据全部取自与 `expected_revision` 一致的**同一份完整源快照**——读取快照时先核对版本（不符立即 409，绝不拿错配版本裁剪），提交前在同一事务内再核对一次（读取匹配后源被修订同样 409），目标已占用返回 409、源不存在返回 404、窗口无相交字幕返回 422；失败不创建目标、不改源场景或播放会话。
- 已保存场景可创建**播放会话（session）**：单进程内存态会话复制场景完整快照，游标从 0 毫秒开始推进（只能不减）；播放中可接纳场景的后续修订——已开始（`start_ms` ≤ 游标）的字幕保持原分轨，其余字幕按原最少换轨与并列规则重算。会话不跨重启保留。接纳是完整操作：场景读取、已开始字幕校验、分轨重算与会话替换在会话锁内串行生效，迟到的接纳请求读到的是轮到它生效时的场景快照，不会把会话回写到旧版本。除原 `/advance` 外，另有 `/events` 事件推进：一次推进返回跨过的**全部**字幕进入/退出事件，短字幕即使在两次采样之间开始又结束也不会漏掉。

## 一条命令启动

```bash
docker compose up --build -d
```

服务监听 **8080** 端口，接口地址：`http://localhost:8080/assign`

停止：`docker compose down`

> - 交互式查看日志可去掉 `-d`。交互式文档：`http://localhost:8080/docs`（Swagger UI）。
> - Compose 自动把具名卷 `scenes-data` 挂载到容器内 `/data`：场景数据库（`SCENES_DB_PATH=/data/scenes.db`）在容器重启甚至 `docker compose down && up` 后仍然保留。仅 `docker compose down -v` 删除卷时才会清除场景。

## 请求

`POST /assign`，JSON Body：

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `track_count` | int | 屏幕轨道数，2 或 3 |
| `subtitles` | array | 1～12 条，顺序即输入顺序 |
| `subtitles[].id` | string \| int | 全局唯一，字符串不得为空 |
| `subtitles[].speaker` | string | 非空（不能全为空白） |
| `subtitles[].start_ms` | int | 非负 |
| `subtitles[].end_ms` | int | 非负，且严格大于 `start_ms` |
| `subtitles[].locked_track` | int \| null | 可选，锁定轨道号（≥1 且 ≤ `track_count`） |

## 响应

- 有解：HTTP 200，`feasible: true`，`changes` 为换轨总数，`assignments` 按**输入原顺序**给出每条字幕的轨道。
- 锁定冲突或轨道不足：HTTP 200，`feasible: false`，`changes: 0`，`assignments: []`（空方案）。
- 非法输入（字段缺失/越界、id 重复、同一说话人字幕时间重叠等）：HTTP **422**，带校验错误明细。

## 可执行的 HTTP 调用示例

### 示例 1：有解（含锁定轨道与端点相接）

```bash
curl -s http://localhost:8080/assign \
  -H 'Content-Type: application/json' \
  -d '{
    "track_count": 2,
    "subtitles": [
      {"id": "s1", "speaker": "Alice", "start_ms": 0,    "end_ms": 1000},
      {"id": "s2", "speaker": "Bob",   "start_ms": 800,  "end_ms": 3000, "locked_track": 2},
      {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 2500},
      {"id": "s4", "speaker": "Alice", "start_ms": 2500, "end_ms": 4000}
    ]
  }'
```

响应：

```json
{
  "feasible": true,
  "changes": 0,
  "assignments": [
    {"id": "s1", "track": 1},
    {"id": "s2", "track": 2},
    {"id": "s3", "track": 1},
    {"id": "s4", "track": 1}
  ]
}
```

说明：s1 与 s3 时间相接可同轨，s3 与 s4 同轨；Alice 三条字幕在同一轨，换轨 0 次。

### 示例 2：非零换轨（锁定迫使相邻字幕分轨）

```bash
curl -s http://localhost:8080/assign \
  -H 'Content-Type: application/json' \
  -d '{
    "track_count": 2,
    "subtitles": [
      {"id": "a", "speaker": "A", "start_ms": 0,   "end_ms": 100, "locked_track": 1},
      {"id": "b", "speaker": "A", "start_ms": 100, "end_ms": 200, "locked_track": 2},
      {"id": "c", "speaker": "A", "start_ms": 200, "end_ms": 300}
    ]
  }'
```

响应：

```json
{
  "feasible": true,
  "changes": 1,
  "assignments": [
    {"id": "a", "track": 1},
    {"id": "b", "track": 2},
    {"id": "c", "track": 2}
  ]
}
```

### 示例 3：无解 —— 两条重叠字幕锁定同一轨（空方案）

```bash
curl -s http://localhost:8080/assign \
  -H 'Content-Type: application/json' \
  -d '{
    "track_count": 2,
    "subtitles": [
      {"id": "s1", "speaker": "Alice", "start_ms": 0,    "end_ms": 2000, "locked_track": 1},
      {"id": "s2", "speaker": "Bob",   "start_ms": 1000, "end_ms": 3000, "locked_track": 1}
    ]
  }'
```

响应：

```json
{"feasible": false, "changes": 0, "assignments": []}
```

### 示例 4：无解 —— 3 条字幕同时重叠但只有 2 轨（轨道不足）

```bash
curl -s http://localhost:8080/assign \
  -H 'Content-Type: application/json' \
  -d '{
    "track_count": 2,
    "subtitles": [
      {"id": "a", "speaker": "A", "start_ms": 0,  "end_ms": 100},
      {"id": "b", "speaker": "B", "start_ms": 10, "end_ms": 110},
      {"id": "c", "speaker": "C", "start_ms": 20, "end_ms": 120}
    ]
  }'
```

响应：

```json
{"feasible": false, "changes": 0, "assignments": []}
```

### 示例 5：非法输入返回 422（同一说话人字幕时间重叠）

```bash
curl -s -i http://localhost:8080/assign \
  -H 'Content-Type: application/json' \
  -d '{
    "track_count": 2,
    "subtitles": [
      {"id": "a", "speaker": "A", "start_ms": 0,   "end_ms": 100},
      {"id": "b", "speaker": "A", "start_ms": 50,  "end_ms": 150}
    ]
  }'
```

返回 `HTTP/1.1 422 Unprocessable Entity`，body 为 FastAPI 标准校验错误结构。

## 场景（scene）：可保存的修订

`/assign` 只做一次性计算、不保存任何数据。如果需要把某次分轨结果保存下来、以后读取并在其基础上继续修订，使用场景接口，数据写入容器内本地 SQLite（Docker Compose 已挂载具名卷，**容器重启甚至重新创建后场景仍可读取**）。

- `PUT /scenes/{scene_id}`：创建或修订场景。请求体沿用 `/assign` 的 `track_count` 与 `subtitles`（同样的字段约束、校验、最少换轨与并列规则），另加一个字段：

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `expected_revision` | int | 非负。客户端所依据的当前版本：**新场景填 `0`**；修订已存在场景时必须填写其当前版本 |

- 新场景首次成功后 `revision = 1`；此后每次成功修订 `revision + 1`。
- 合法但无解：返回与 `/assign` **完全相同**的无解响应（`feasible: false`、空方案），**不保存、不改变任何已有版本**。
- 非法输入：**422**，不改变已有版本。
- 版本不符（含同一版本的并发竞争落败方）：**409**，响应体给出 `current_revision`，不改变已有版本。同一版本的并发修订最多只有一次成功，读取结果永远是某次成功提交的完整快照，不会混用两次提交的数据。
- `GET /scenes/{scene_id}`：读取最近一次成功修订保存的完整场景（字幕、分轨、changes、版本）；场景不存在返回 **404**。
- 数据库路径可用环境变量 `SCENES_DB_PATH` 覆盖（Compose 中为 `/data/scenes.db`）。

### 示例 6：创建场景（`expected_revision=0`，成功后版本为 1）

```bash
curl -s -X PUT http://localhost:8080/scenes/scene-001 \
  -H 'Content-Type: application/json' \
  -d '{
    "track_count": 2,
    "expected_revision": 0,
    "subtitles": [
      {"id": "s1", "speaker": "Alice", "start_ms": 0,    "end_ms": 1000},
      {"id": "s2", "speaker": "Bob",   "start_ms": 800,  "end_ms": 3000, "locked_track": 2},
      {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 2500}
    ]
  }'
```

响应（HTTP 200）：

```json
{
  "scene_id": "scene-001",
  "revision": 1,
  "track_count": 2,
  "subtitles": [
    {"id": "s1", "speaker": "Alice", "start_ms": 0,    "end_ms": 1000, "locked_track": null},
    {"id": "s2", "speaker": "Bob",   "start_ms": 800,  "end_ms": 3000, "locked_track": 2},
    {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 2500, "locked_track": null}
  ],
  "changes": 0,
  "assignments": [
    {"id": "s1", "track": 1},
    {"id": "s2", "track": 2},
    {"id": "s3", "track": 1}
  ],
  "feasible": true
}
```

### 示例 7：在当前版本上修订（`expected_revision` 填当前版本，成功后 +1）

```bash
curl -s -X PUT http://localhost:8080/scenes/scene-001 \
  -H 'Content-Type: application/json' \
  -d '{
    "track_count": 2,
    "expected_revision": 1,
    "subtitles": [
      {"id": "s1", "speaker": "Alice", "start_ms": 0,    "end_ms": 1200},
      {"id": "s2", "speaker": "Bob",   "start_ms": 800,  "end_ms": 3000},
      {"id": "s3", "speaker": "Alice", "start_ms": 1200, "end_ms": 2500}
    ]
  }'
```

响应中的 `"revision": 2`。再次修订时需把 `expected_revision` 改为 `2`。

### 示例 8：读取场景

```bash
curl -s http://localhost:8080/scenes/scene-001
```

返回最近一次成功修订的完整快照（结构同示例 7 的成功响应）。场景不存在时返回 `HTTP/1.1 404 Not Found`。

### 示例 9：版本冲突返回 409（不改动已有版本）

假设场景当前版本为 2，却仍按旧版本 1 提交：

```bash
curl -s -i -X PUT http://localhost:8080/scenes/scene-001 \
  -H 'Content-Type: application/json' \
  -d '{
    "track_count": 2,
    "expected_revision": 1,
    "subtitles": [
      {"id": "s1", "speaker": "Alice", "start_ms": 0, "end_ms": 1000}
    ]
  }'
```

```
HTTP/1.1 409 Conflict
```

```json
{
  "detail": {
    "message": "场景版本冲突：expected_revision 与当前版本不一致",
    "scene_id": "scene-001",
    "current_revision": 2
  }
}
```

此时读取场景，`revision` 仍为 `2`，字幕与分轨仍是修订前的内容。客户端应先 `GET /scenes/scene-001` 取得当前版本，再带上新的 `expected_revision` 重试。对一个已存在的场景提交 `expected_revision: 0`（按新场景创建）同样返回 409；同一版本的并发提交也只有一次成功，其余均返回此响应。

> 合法但无解的场景修订不产生 409/200 快照，而是与示例 3/4 的 `/assign` 一样返回 `{"feasible": false, "changes": 0, "assignments": []}`，且不落库、不推进版本。非法输入（缺少 `expected_revision`、`track_count` 越界、说话人字幕重叠等）返回 422，同样不改动已有版本。

## 场景片段（clip）：把指定时段另存为从 0 毫秒起的独立片段

已保存场景可以把其中的一个时段**另存为全新场景**：`POST /scenes/{scene_id}/clip`。片段的时间轴从 **0 毫秒**重新开始，并**保持源场景的画面轨道**——裁剪与创建是原子的，源场景与任何播放会话都不受影响。

请求体字段：

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `target_scene_id` | string | 片段的新场景 id，**非空且尚未被占用**（不能与已有场景同名，包括与源同名） |
| `expected_revision` | int | 非负。所依据的**源场景**当前版本 |
| `from_ms` | int | 裁剪起点（毫秒），非负整数；**布尔值与字符串一律 422 拒绝**（不做隐式转换） |
| `to_ms` | int | 裁剪终点（毫秒），非负整数且严格满足 `from_ms < to_ms`；同样拒绝布尔值与字符串 |

裁剪规则：

- 保留与窗口 **`[from_ms, to_ms)`（左闭右开）相交**的字幕，按**源场景的字幕顺序**保留 `id`、`speaker`；时间裁到窗口内（`max(start, from)`、`min(end, to)`）再整体减去 `from_ms`，片段从 0 毫秒起。端点相接不算相交：`end_ms == from_ms` 或 `start_ms == to_ms` 的字幕被丢弃。
- 源场景为每条字幕求出的轨道写入片段字幕的 **`locked_track`**，目标沿用源的 **`track_count`** 与既有最少换轨、并列规则重新求解——源分轨本身就是互不冲突的合法解，因此片段的 `assignments` 与源画面轨道**逐一相同**；`changes` 按裁剪后的片段重算。
- 片段保存为 **revision 1** 的独立场景，返回完整场景快照。之后可直接用既有 `GET/PUT /scenes/...` 与 `/sessions` 读取、修订、播放，**旧接口一律不变**。

状态码：

- **200**：裁剪成功，返回片段完整场景。
- **404**：源场景不存在。
- **409**：`expected_revision` 与源当前版本不一致（源被并发修订）；或 `target_scene_id` 已被占用。裁剪所用的 `track_count`、字幕与源分轨**全部取自与 `expected_revision` 一致的同一份完整快照**，版本核对分两道：**读取源快照时**先核对 `record.revision == expected_revision`（版本不符立即 409，过期与超前同等待遇，绝不拿版本 1 的数据按版本 2 的请求裁剪）；**提交前**在同一个 `BEGIN IMMEDIATE` 事务内再核对一次，覆盖“读取匹配后、提交前源被修订”的窗口。失败既不会留下目标场景，也不会改动源场景或任何播放会话。
- **422**：`from_ms >= to_ms`、时间为负数/布尔值/字符串/浮点数、`target_scene_id` 为空等非法请求；或窗口内**没有任何相交字幕**。

### 示例 10：裁剪时段并另存（时间归零、保持原轨道）

先创建源场景 `scene-clip-src`（s1 `[0,1000)`、s2 `[800,3000)` 锁轨 2、s3 `[1000,2500)`，分轨 1/2/1）：

```bash
curl -s -X PUT http://localhost:8080/scenes/scene-clip-src \
  -H 'Content-Type: application/json' \
  -d '{
    "track_count": 2,
    "expected_revision": 0,
    "subtitles": [
      {"id": "s1", "speaker": "Alice", "start_ms": 0,    "end_ms": 1000},
      {"id": "s2", "speaker": "Bob",   "start_ms": 800,  "end_ms": 3000, "locked_track": 2},
      {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 2500}
    ]
  }'
```

把窗口 `[900, 3000)` 另存为 `scene-clip-1`（依据 revision 1）：

```bash
curl -s -X POST http://localhost:8080/scenes/scene-clip-src/clip \
  -H 'Content-Type: application/json' \
  -d '{
    "target_scene_id": "scene-clip-1",
    "expected_revision": 1,
    "from_ms": 900,
    "to_ms": 3000
  }'
```

```json
{
  "scene_id": "scene-clip-1",
  "revision": 1,
  "track_count": 2,
  "subtitles": [
    {"id": "s1", "speaker": "Alice", "start_ms": 0,   "end_ms": 100,  "locked_track": 1},
    {"id": "s2", "speaker": "Bob",   "start_ms": 0,   "end_ms": 2100, "locked_track": 2},
    {"id": "s3", "speaker": "Alice", "start_ms": 100, "end_ms": 1600, "locked_track": 1}
  ],
  "changes": 0,
  "assignments": [
    {"id": "s1", "track": 1},
    {"id": "s2", "track": 2},
    {"id": "s3", "track": 1}
  ],
  "feasible": true
}
```

要点：三条字幕都与 `[900,3000)` 相交，时间裁到窗口后减去 900（s1 只剩尾部 `[0,100)`，s2 变 `[0,2100)`，s3 变 `[100,1600)`）；每条的 `locked_track` 都是源分轨（1/2/1），重算后的 `assignments` 与源画面轨道完全一致。源场景 `scene-clip-src` 仍停留在 revision 1、内容不变。

也可以直接读取验证：

```bash
curl -s http://localhost:8080/scenes/scene-clip-1
```

### 示例 11：裁剪冲突与非法窗口（404 / 409 / 422）

**源不存在 → 404**（不会创建目标）：

```bash
curl -s -i -X POST http://localhost:8080/scenes/no-such-scene/clip \
  -H 'Content-Type: application/json' \
  -d '{"target_scene_id": "scene-x", "expected_revision": 0, "from_ms": 0, "to_ms": 1000}'
# HTTP/1.1 404 Not Found
```

**源版本过期 → 409**（对已存在的源提交 `expected_revision: 0` 同样是 409）：

```bash
curl -s -i -X POST http://localhost:8080/scenes/scene-clip-src/clip \
  -H 'Content-Type: application/json' \
  -d '{"target_scene_id": "scene-stale", "expected_revision": 0, "from_ms": 0, "to_ms": 1000}'
```

```
HTTP/1.1 409 Conflict
```

```json
{"detail": {"message": "场景版本冲突：expected_revision 与当前版本不一致",
            "scene_id": "scene-clip-src", "current_revision": 1}}
```

**目标已被占用 → 409**（重复使用示例 10 的目标 id；已有目标不会被覆盖，源也不变）：

```bash
curl -s -i -X POST http://localhost:8080/scenes/scene-clip-src/clip \
  -H 'Content-Type: application/json' \
  -d '{"target_scene_id": "scene-clip-1", "expected_revision": 1, "from_ms": 0, "to_ms": 1000}'
# HTTP/1.1 409 Conflict
# {"detail":{"message":"target_scene_id 已被占用，裁剪目标必须使用新的 scene_id",
#            "scene_id":"scene-clip-1","current_revision":1}}
```

**窗口内没有相交字幕 → 422**（三条字幕都在 5000 之前结束；不留目标、不改源）：

```bash
curl -s -i -X POST http://localhost:8080/scenes/scene-clip-src/clip \
  -H 'Content-Type: application/json' \
  -d '{"target_scene_id": "scene-empty", "expected_revision": 1, "from_ms": 5000, "to_ms": 6000}'
# HTTP/1.1 422 Unprocessable Entity
# {"detail":{"message":"裁剪窗口 [from_ms, to_ms) 内没有与任何字幕相交的时段",
#            "scene_id":"scene-clip-src","from_ms":5000,"to_ms":6000}}
```

其余非法请求同样返回 422（Pydantic 校验，均不落库）：

```bash
# from_ms 不小于 to_ms
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8080/scenes/scene-clip-src/clip \
  -H 'Content-Type: application/json' \
  -d '{"target_scene_id": "scene-bad", "expected_revision": 1, "from_ms": 3000, "to_ms": 900}'
# 422

# 时间字段拒绝布尔值（true 在 JSON 里不是整数 1）和字符串
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8080/scenes/scene-clip-src/clip \
  -H 'Content-Type: application/json' \
  -d '{"target_scene_id": "scene-bad", "expected_revision": 1, "from_ms": true, "to_ms": 1000}'
# 422
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8080/scenes/scene-clip-src/clip \
  -H 'Content-Type: application/json' \
  -d '{"target_scene_id": "scene-bad", "expected_revision": 1, "from_ms": 0, "to_ms": "1000"}'
# 422
```

### 示例 12：裁剪版本冲突——读取即核对，按当前版本重试

裁剪所用的 `track_count`、字幕与源分轨全部取自与 `expected_revision` 一致的**同一份完整源快照**：读取源快照时先核对版本（不符立即 409，绝不拿错配版本裁剪），提交前在同一事务内再核对一次。下面是可直接复制执行的完整脚本。

先创建源场景（revision 1）：

```bash
curl -s -X PUT http://localhost:8080/scenes/scene-clip-conf \
  -H 'Content-Type: application/json' \
  -d '{
    "track_count": 2,
    "expected_revision": 0,
    "subtitles": [
      {"id": "s1", "speaker": "Alice", "start_ms": 0,    "end_ms": 1000},
      {"id": "s2", "speaker": "Bob",   "start_ms": 800,  "end_ms": 3000, "locked_track": 2},
      {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 2500}
    ]
  }' >/dev/null
```

**读取时版本不符 → 409**（请求依据的是尚不存在的 revision 2，源当前为 revision 1；过期版本同理）：

```bash
curl -s -i -X POST http://localhost:8080/scenes/scene-clip-conf/clip \
  -H 'Content-Type: application/json' \
  -d '{"target_scene_id": "scene-clip-conf-out", "expected_revision": 2, "from_ms": 0, "to_ms": 3000}'
```

```
HTTP/1.1 409 Conflict
```

```json
{"detail": {"message": "场景版本冲突：expected_revision 与当前版本不一致",
            "scene_id": "scene-clip-conf", "current_revision": 1}}
```

失败不创建目标（源、播放会话同样不变）：

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8080/scenes/scene-clip-conf-out
# 404
```

客户端应先 `GET /scenes/scene-clip-conf` 取得当前版本再重试。把源修订到 revision 2（s3 延长到 4000，新增 s4），然后带上 `expected_revision: 2` 重试裁剪：

```bash
# 源：revision 1 -> 2
curl -s -X PUT http://localhost:8080/scenes/scene-clip-conf \
  -H 'Content-Type: application/json' \
  -d '{
    "track_count": 2,
    "expected_revision": 1,
    "subtitles": [
      {"id": "s1", "speaker": "Alice", "start_ms": 0,    "end_ms": 1000},
      {"id": "s2", "speaker": "Bob",   "start_ms": 800,  "end_ms": 3000, "locked_track": 2},
      {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 4000},
      {"id": "s4", "speaker": "Carol", "start_ms": 3200, "end_ms": 4000}
    ]
  }' >/dev/null

# 依据 revision 2 裁剪窗口 [0, 3200)：s4 start==to 不相交
curl -s -X POST http://localhost:8080/scenes/scene-clip-conf/clip \
  -H 'Content-Type: application/json' \
  -d '{"target_scene_id": "scene-clip-conf-out", "expected_revision": 2, "from_ms": 0, "to_ms": 3200}'
```

```json
{
  "scene_id": "scene-clip-conf-out",
  "revision": 1,
  "track_count": 2,
  "subtitles": [
    {"id": "s1", "speaker": "Alice", "start_ms": 0,    "end_ms": 1000, "locked_track": 1},
    {"id": "s2", "speaker": "Bob",   "start_ms": 800,  "end_ms": 3000, "locked_track": 2},
    {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 3200, "locked_track": 1}
  ],
  "changes": 0,
  "assignments": [
    {"id": "s1", "track": 1},
    {"id": "s2", "track": 2},
    {"id": "s3", "track": 1}
  ],
  "feasible": true
}
```

片段内容严格来自 revision 2 的快照（含延长后的 s3、且不含 revision 2 才出现的窗口外 s4），目标仍保存为 revision 1。

**提交前源被修订 → 同样 409**：第二道核对覆盖“读取匹配后、提交前”的窗口。并发地同时发起裁剪与源修订时，二者被数据库写锁串行——裁剪先生效则得到与某一完整版本一致的片段（恒为 revision 1），修订先生效则裁剪返回 409 且 `current_revision` 为新版本，绝不会把旧快照的字幕/分轨存成新版本片段：

```bash
# 先把源重建为 revision 1，再并发：裁剪(期望 1) 与 修订(1 -> 2)
curl -s -X PUT http://localhost:8080/scenes/scene-clip-race \
  -H 'Content-Type: application/json' \
  -d '{"track_count": 2, "expected_revision": 0,
       "subtitles": [
         {"id": "s1", "speaker": "Alice", "start_ms": 0, "end_ms": 1000},
         {"id": "s2", "speaker": "Bob", "start_ms": 800, "end_ms": 3000, "locked_track": 2}
       ]}' >/dev/null

curl -s -w '\nclip:%{http_code}\n' -X POST http://localhost:8080/scenes/scene-clip-race/clip \
  -H 'Content-Type: application/json' \
  -d '{"target_scene_id": "scene-clip-race-out", "expected_revision": 1, "from_ms": 0, "to_ms": 3000}' &
curl -s -o /dev/null -X PUT http://localhost:8080/scenes/scene-clip-race \
  -H 'Content-Type: application/json' \
  -d '{"track_count": 2, "expected_revision": 1,
       "subtitles": [{"id": "only", "speaker": "Solo", "start_ms": 0, "end_ms": 50}]}' &
wait
# clip:200 -> 片段含 s1、s2（revision 1 的完整快照）；clip:409 -> current_revision 为 2，无目标残留
```

### 示例 13：播放裁剪出来的片段（沿用既有会话接口）

片段就是普通场景，用原来的 `/sessions` 接口播放即可。对示例 10 的 `scene-clip-1`（s1 `[0,100)`、s2 `[0,2100)`、s3 `[100,1600)`，轨道 1/2/1）创建会话并事件推进到 2100：

```bash
CID=$(curl -s -X POST http://localhost:8080/sessions \
  -H 'Content-Type: application/json' \
  -d '{"scene_id": "scene-clip-1"}' \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["session_id"])')

# 0 毫秒已活动的 s1、s2 由创建响应快照确定；直接事件推进到 2100
curl -s -X POST http://localhost:8080/sessions/$CID/events \
  -H 'Content-Type: application/json' -d '{"time_ms": 2100}'
```

```json
{
  "session_id": "…",
  "scene_id": "scene-clip-1",
  "source_revision": 1,
  "cursor_ms": 2100,
  "events": [
    {"id": "s1", "speaker": "Alice", "track": 1, "time_ms": 100,  "type": "exit"},
    {"id": "s3", "speaker": "Alice", "track": 1, "time_ms": 100,  "type": "enter"},
    {"id": "s3", "speaker": "Alice", "track": 1, "time_ms": 1600, "type": "exit"},
    {"id": "s2", "speaker": "Bob",   "track": 2, "time_ms": 2100, "type": "exit"}
  ],
  "active": []
}
```

片段时间轴从 0 开始：s1 在 100 毫秒退出与 s3 在 100 毫秒进入同刻，按既有规则**先退出后进入**；s1、s2 在 0 毫秒已活动，其进入事件由创建会话的响应快照确定，事件流不补发。片段还可以按 `expected_revision: 1` 用 `PUT /scenes/scene-clip-1` 继续修订（成功后 revision 2），与源场景互不影响。

## 播放会话（session）：播放中接纳后续修订

`/assign` 与 `/scenes` 都是无状态的计算/存储；播放会话则让“正在播放”的场景跟上后续修订：先按 `scene_id` 创建会话（复制场景当时的完整快照，游标从 0 毫秒开始），播放过程中推进游标，场景被修订后让会话接纳最新快照。会话是**单进程内存态**，不写入 SQLite、不跨重启保留；同一会话上的推进与接纳按完整操作串行生效（并发请求不会交错出中间态）。接纳的完整操作包含**场景读取本身**：请求在会话锁内才读取场景快照，因此并发下每个接纳请求应用的都是轮到它生效时的快照，会话的 `source_revision` 不会被迟到的请求回退。

- `POST /sessions`：创建会话。请求体 `{"scene_id": "..."}`；场景不存在返回 404。响应为会话完整状态：`session_id`、`scene_id`、`source_revision`（快照来源的场景版本）、`cursor_ms`（初始 0）、`track_count`、`subtitles`、`assignments`、`changes`。
- `POST /sessions/{session_id}/advance`：推进游标。请求体 `{"time_ms": N}`，`N` 为非负整数且**只能不减**（等于当前游标允许）；负数、非整数或倒退返回 422 且会话不变。响应含 `source_revision`、`cursor_ms` 与 `active`：游标时刻处于 `[start_ms, end_ms)`（左闭右开）区间的字幕及其轨道。
- `POST /sessions/{session_id}/events`：**事件推进**。请求体与推进规则（非负整数、只能不减、422/404、共用同一游标）完全相同；额外返回本次推进跨过的**全部**字幕进入/退出事件，避免短字幕在两次采样之间漏掉：
  - 事件窗口为**（旧游标, time_ms]**（`旧游标 < 事件时刻 ≤ time_ms`）：字幕在 `start_ms` 进入（`type: "enter"`）、在 `end_ms` 退出（`type: "exit"`，区间左闭右开）。一条字幕开始和结束都落在窗口内时，进入与退出成对返回。
  - **0 毫秒已活动的字幕（`start_ms` 为 0）由创建会话响应的快照确定**：初始游标即 0，其进入时刻不在任何推进窗口内，事件流不会补发它的进入——客户端应以创建响应快照（字幕与分轨）自行确定初始活动集合。
  - 每个事件含字幕 `id`、`speaker`、`track`、毫秒时刻 `time_ms` 与类型 `type`（`enter`/`exit`）。事件按 `time_ms` **升序**；同一时刻**先退出后进入**；同类型按**会话字幕顺序**（接纳后即新快照顺序）。
  - 响应还含推进后的 `active`（同 `/advance` 的 `[start_ms, end_ms)` 活动字幕）、`source_revision` 与推进后的 `cursor_ms`。**等时推进**（`time_ms` 等于当前游标）`events` 为空。
  - 使用轮到该请求生效时的会话快照生成事件（接纳后的游标之后推进，事件与轨道来自接纳后的新快照），不读取未被接纳的场景修订；事件生成、游标更新与活动字幕计算在会话锁内与原推进、接纳**完整串行**。
  - 事件只随本次推进生成、即生即返：**不保存事件历史、不提供重放**；重复推进同一时刻得到空事件。
  - 未知会话（含服务重启后）返回 404；非法时间或游标倒退返回 422；任何失败都不改变会话与场景。
- `POST /sessions/{session_id}/accept`：接纳场景当前快照。规则：
  - 轨道数不得变；
  - 会话中 `start_ms` ≤ 游标的字幕（已开始，含已播完），其 `id`、`speaker`、起止时间、`locked_track` 必须不变，并**固定原分轨**；
  - 其余字幕可改、可删、可新增，但都必须在游标之后开始（`start_ms` > 游标）；
  - 按新快照顺序与最少换轨、并列规则重算全部分轨。
  - 场景读取、上述校验、分轨重算与会话替换是同一个完整操作，与同一会话的推进、其他接纳串行生效：并发接纳时，每个请求接纳的都是轮到它生效时的场景快照。
  - 冲突或无解返回 **409** 且会话不变；成功则整体替换会话快照与 `source_revision`（**场景本身不变**），返回会话最新完整状态。
- 会话不存在（含服务重启后）返回 404。

### 示例 14：创建会话并推进游标

沿用示例 6 创建的场景 `scene-001`（revision 1）：

```bash
curl -s -X POST http://localhost:8080/sessions \
  -H 'Content-Type: application/json' \
  -d '{"scene_id": "scene-001"}'
```

```json
{
  "session_id": "9f2c…（略）",
  "scene_id": "scene-001",
  "source_revision": 1,
  "cursor_ms": 0,
  "track_count": 2,
  "changes": 0,
  "subtitles": [ … ],
  "assignments": [
    {"id": "s1", "track": 1},
    {"id": "s2", "track": 2},
    {"id": "s3", "track": 1}
  ]
}
```

推进到 900 毫秒（`$SID` 为上一步返回的 `session_id`）：

```bash
curl -s -X POST http://localhost:8080/sessions/$SID/advance \
  -H 'Content-Type: application/json' \
  -d '{"time_ms": 900}'
```

```json
{
  "session_id": "9f2c…",
  "scene_id": "scene-001",
  "source_revision": 1,
  "cursor_ms": 900,
  "active": [
    {"id": "s1", "speaker": "Alice", "start_ms": 0, "end_ms": 1000, "locked_track": null, "track": 1},
    {"id": "s2", "speaker": "Bob", "start_ms": 800, "end_ms": 3000, "locked_track": 2, "track": 2}
  ]
}
```

`active` 为游标时刻 `[start_ms, end_ms)` 内的字幕及轨道：900 时 s1（0≤900<1000）与 s2（800≤900<3000）在屏，s3（1000 才开始）不在。再次提交更小的 `time_ms` 返回 422。

### 示例 15：事件推进一次跨过整条短字幕（进入与退出都不漏）

新建一个含短字幕的场景与会话：短字幕 `b` 只在 `[500, 600)` 出现，若播放客户端在 400 与 1000 两次采样，用 `/advance` 就完全看不到它；用 `/events` 则在跨过它的这次推进里成对拿到进入与退出。

```bash
# 场景：a[0,1000)，短字幕 b[500,600)
curl -s -X PUT http://localhost:8080/scenes/scene-events \
  -H 'Content-Type: application/json' \
  -d '{
    "track_count": 2,
    "expected_revision": 0,
    "subtitles": [
      {"id": "a", "speaker": "A", "start_ms": 0,   "end_ms": 1000},
      {"id": "b", "speaker": "B", "start_ms": 500, "end_ms": 600}
    ]
  }'

SID=$(curl -s -X POST http://localhost:8080/sessions \
  -H 'Content-Type: application/json' \
  -d '{"scene_id": "scene-events"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["session_id"])')

# 第一次推进到 400：窗口 (0,400] 内无任何进入/退出（a 在 0 毫秒已活动，
# 其进入由上面创建响应的快照确定，事件流不补发）
curl -s -X POST http://localhost:8080/sessions/$SID/events \
  -H 'Content-Type: application/json' -d '{"time_ms": 400}'
```

```json
{
  "session_id": "…",
  "scene_id": "scene-events",
  "source_revision": 1,
  "cursor_ms": 400,
  "events": [],
  "active": [
    {"id": "a", "speaker": "A", "start_ms": 0, "end_ms": 1000, "locked_track": null, "track": 1}
  ]
}
```

```bash
# 第二次直接推进到 1000：窗口 (400,1000] 完整跨过短字幕 b
curl -s -X POST http://localhost:8080/sessions/$SID/events \
  -H 'Content-Type: application/json' -d '{"time_ms": 1000}'
```

```json
{
  "session_id": "…",
  "scene_id": "scene-events",
  "source_revision": 1,
  "cursor_ms": 1000,
  "events": [
    {"id": "b", "speaker": "B", "track": 2, "time_ms": 500,  "type": "enter"},
    {"id": "b", "speaker": "B", "track": 2, "time_ms": 600,  "type": "exit"},
    {"id": "a", "speaker": "A", "track": 1, "time_ms": 1000, "type": "exit"}
  ],
  "active": []
}
```

要点：

- 短字幕 `b` 的进入（500）与退出（600）都落在 (400,1000] 内，成对返回，不会因为两次采样间隔而漏掉；
- 事件按 `time_ms` 升序；同刻先 `exit` 后 `enter`（例如端点相接时前一条的退出排在后一条的进入之前）；同为进入或同为退出时按会话字幕顺序；
- 0 毫秒已活动的 `a` 不在事件流里给进入事件——初始活动集合由创建会话的响应快照（`subtitles` + `assignments`，游标 0）确定；
- 再对同一时刻推进一次为**等时推进**，`events` 为空：事件不保存历史、不提供重放。

```bash
curl -s -X POST http://localhost:8080/sessions/$SID/events \
  -H 'Content-Type: application/json' -d '{"time_ms": 1000}'
# {"session_id":"…","source_revision":1,"cursor_ms":1000,"events":[],"active":[]}
```

### 示例 16：两个推进端点交接（共用游标，原 /advance 响应不变）

`/advance` 与 `/events` 操作同一个会话游标，可以混用交接：客户端平时只需活动快照时用 `/advance`（响应仍是原有的 `source_revision`/`cursor_ms`/`active`，不含 `events`），需要补全跨过的进入/退出时用 `/events`。沿用示例 6 的 `scene-001`（s1 `[0,1000)`、s2 `[800,3000)` 锁轨 2、s3 `[1000,2500)`）：

```bash
SID=$(curl -s -X POST http://localhost:8080/sessions \
  -H 'Content-Type: application/json' \
  -d '{"scene_id": "scene-001"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["session_id"])')

# 原 /advance 先推进到 900（响应保持原样，没有 events 字段）
curl -s -X POST http://localhost:8080/sessions/$SID/advance \
  -H 'Content-Type: application/json' -d '{"time_ms": 900}'
# {"session_id":"…","source_revision":1,"cursor_ms":900,
#  "active":[{"id":"s1",…,"track":1},{"id":"s2",…,"track":2}]}

# 再由 /events 推进到 1000：窗口为 (900,1000]，
# 不会补发 800 时 s2 的进入（那已被旧游标 900 跨过）
curl -s -X POST http://localhost:8080/sessions/$SID/events \
  -H 'Content-Type: application/json' -d '{"time_ms": 1000}'
```

```json
{
  "session_id": "…",
  "scene_id": "scene-001",
  "source_revision": 1,
  "cursor_ms": 1000,
  "events": [
    {"id": "s1", "speaker": "Alice", "track": 1, "time_ms": 1000, "type": "exit"},
    {"id": "s3", "speaker": "Alice", "track": 1, "time_ms": 1000, "type": "enter"}
  ],
  "active": [
    {"id": "s2", "speaker": "Bob", "start_ms": 800, "end_ms": 3000, "locked_track": 2, "track": 2},
    {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 2500, "locked_track": null, "track": 1}
  ]
}
```

1000 时刻 s1 退出（右开）与 s3 进入（左闭）同刻，**先退出后进入**。此后再用任一端点把时间往回（999）推进都返回 422，会话不变：

```bash
curl -s -i -X POST http://localhost:8080/sessions/$SID/events \
  -H 'Content-Type: application/json' -d '{"time_ms": 999}'
# HTTP/1.1 422 Unprocessable Entity
# {"detail":{"message":"time_ms 只能不减，不得小于当前游标","cursor_ms":1000,"time_ms":999}}
```

未知会话返回 404（与 `/advance` 一致）：

```bash
curl -s -i -X POST http://localhost:8080/sessions/no-such-session/events \
  -H 'Content-Type: application/json' -d '{"time_ms": 0}'
# HTTP/1.1 404 Not Found
```

### 示例 17：接纳修订后再事件推进（事件与轨道来自接纳后的新快照）

下面是一段可直接复制执行的完整脚本：创建 revision 1 → 建会话并推进到 900（s1、s2 已开始）→ 场景修订为 revision 2（s3 延长到 `[1000,4000)`，新增 s4 `[3200,4000)`）→ 会话接纳 → 用 `/events` 一次推进到 4000。

```bash
# 1) revision 1：s1[0,1000)、s2[800,3000) 锁轨 2、s3[1000,2500)
curl -s -X PUT http://localhost:8080/scenes/scene-evt-accept \
  -H 'Content-Type: application/json' \
  -d '{
    "track_count": 2,
    "expected_revision": 0,
    "subtitles": [
      {"id": "s1", "speaker": "Alice", "start_ms": 0,    "end_ms": 1000},
      {"id": "s2", "speaker": "Bob",   "start_ms": 800,  "end_ms": 3000, "locked_track": 2},
      {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 2500}
    ]
  }' >/dev/null

SID=$(curl -s -X POST http://localhost:8080/sessions \
  -H 'Content-Type: application/json' \
  -d '{"scene_id": "scene-evt-accept"}' \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["session_id"])')

# 2) 游标先推进到 900（s1、s2 已开始，接纳时固定原分轨 1、2）
curl -s -X POST http://localhost:8080/sessions/$SID/advance \
  -H 'Content-Type: application/json' -d '{"time_ms": 900}' >/dev/null

# 3) 场景修订为 revision 2：s3 延长到 4000，新增 s4[3200,4000)
curl -s -X PUT http://localhost:8080/scenes/scene-evt-accept \
  -H 'Content-Type: application/json' \
  -d '{
    "track_count": 2,
    "expected_revision": 1,
    "subtitles": [
      {"id": "s1", "speaker": "Alice", "start_ms": 0,    "end_ms": 1000},
      {"id": "s2", "speaker": "Bob",   "start_ms": 800,  "end_ms": 3000, "locked_track": 2},
      {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 4000},
      {"id": "s4", "speaker": "Carol", "start_ms": 3200, "end_ms": 4000}
    ]
  }' >/dev/null

# 4) 会话接纳最新快照（source_revision -> 2），随后事件推进到 4000
curl -s -X POST http://localhost:8080/sessions/$SID/accept >/dev/null
curl -s -X POST http://localhost:8080/sessions/$SID/events \
  -H 'Content-Type: application/json' -d '{"time_ms": 4000}'
```

```json
{
  "session_id": "…",
  "scene_id": "scene-evt-accept",
  "source_revision": 2,
  "cursor_ms": 4000,
  "events": [
    {"id": "s1", "speaker": "Alice", "track": 1, "time_ms": 1000, "type": "exit"},
    {"id": "s3", "speaker": "Alice", "track": 1, "time_ms": 1000, "type": "enter"},
    {"id": "s2", "speaker": "Bob",   "track": 2, "time_ms": 3000, "type": "exit"},
    {"id": "s4", "speaker": "Carol", "track": 2, "time_ms": 3200, "type": "enter"},
    {"id": "s3", "speaker": "Alice", "track": 1, "time_ms": 4000, "type": "exit"},
    {"id": "s4", "speaker": "Carol", "track": 2, "time_ms": 4000, "type": "exit"}
  ],
  "active": []
}
```

事件与轨道全部来自**接纳后**的会话快照（s3 的 `end_ms` 已是 4000、s4 是修订新增的字幕、`source_revision` 为 2）；接纳失败（409）或尚未接纳的场景修订都不会影响事件——事件生成只读取轮到该请求生效时的会话快照，并与推进、接纳在会话锁内完整串行。4000 时刻 s3、s4 同为退出，按新快照的会话字幕顺序（s3 在 s4 前）排列。

### 示例 18：播放中接纳场景修订

播放期间场景被修订（例如按示例 7 的方式把 `scene-001` 推进到 revision 2：s3 延长到 4000，并新增 s4 `[3200, 4000)`）。会话接纳最新快照：

```bash
curl -s -X POST http://localhost:8080/sessions/$SID/accept
```

```json
{
  "session_id": "9f2c…",
  "scene_id": "scene-001",
  "source_revision": 2,
  "cursor_ms": 900,
  "track_count": 2,
  "changes": 0,
  "subtitles": [ … s1, s2, s3(1000–4000), s4(3200–4000) ],
  "assignments": [
    {"id": "s1", "track": 1},
    {"id": "s2", "track": 2},
    {"id": "s3", "track": 1},
    {"id": "s4", "track": 2}
  ]
}
```

游标 900 时 s1、s2 已开始，保持原分轨 1、2 不变；s3、s4 在游标之后开始，按新快照重算分轨。`source_revision` 变为 2，场景本身的版本与内容不受接纳影响。

### 示例 19：接纳冲突返回 409（会话不变）

若场景的修订动了已开始字幕（上例中游标 900 时修改了 s1 的起止时间、删除 s2、把轨道数改为 3，或新增的字幕在 900 之前开始等），接纳失败：

```bash
curl -s -i -X POST http://localhost:8080/sessions/$SID/accept
```

```
HTTP/1.1 409 Conflict
```

```json
{
  "detail": {
    "message": "字幕 's1' 已开始，其 id/speaker/起止时间/locked_track 不得修改",
    "scene_id": "scene-001"
  }
}
```

固定已开始字幕的原分轨后新快照无解时同样返回 409。失败的接纳不改变会话：游标、快照、`source_revision` 保持原值，可继续推进或在场景改回合法内容后重试接纳。

### 接纳的并发修复说明

此前 `accept` 在进入会话锁**之前**读取场景快照，存在回写缺陷：请求 R1 读到场景版本 1 后被挂起；期间场景把某条未开始字幕的 `end_ms` 从 2000 改为 4000 并保存为版本 2，另一请求 R2 先完成接纳（会话到达版本 2）；R1 恢复后仍应用手中过期的版本 1 快照，把会话的 `source_revision` 与字幕**回退**到版本 1。

修复后，接纳把“读取场景快照 → 校验已开始字幕 → 重算分轨 → 替换会话状态”作为**完整操作**在会话锁内串行生效：场景读取发生在轮到该请求生效之时，因此迟到的请求接纳的一定是当时的场景快照——既不会把旧数据回写到会话上，也不会跳过旧数据却报告成功。会话的 `source_revision` 对同一会话只增不回退。请求、响应与错误码（200/404/409/422）均与之前兼容。

### 示例 20：并发接纳——迟到请求不会回写旧快照

```bash
# 1) 创建场景（版本 1：未开始的 s3 end_ms=2000）
curl -s -X PUT http://localhost:8080/scenes/scene-race \
  -H 'Content-Type: application/json' \
  -d '{
    "track_count": 2,
    "expected_revision": 0,
    "subtitles": [
      {"id": "s1", "speaker": "Alice", "start_ms": 0,    "end_ms": 1000},
      {"id": "s2", "speaker": "Bob",   "start_ms": 800,  "end_ms": 3000, "locked_track": 2},
      {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 2000}
    ]
  }'

# 2) 创建会话并推进游标到 900（s1、s2 已开始，s3 未开始）
SID=$(curl -s -X POST http://localhost:8080/sessions \
  -H 'Content-Type: application/json' \
  -d '{"scene_id": "scene-race"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["session_id"])')
curl -s -X POST http://localhost:8080/sessions/$SID/advance \
  -H 'Content-Type: application/json' -d '{"time_ms": 900}'

# 3) 场景修订为版本 2：s3 的 end_ms 从 2000 改为 4000
curl -s -X PUT http://localhost:8080/scenes/scene-race \
  -H 'Content-Type: application/json' \
  -d '{
    "track_count": 2,
    "expected_revision": 1,
    "subtitles": [
      {"id": "s1", "speaker": "Alice", "start_ms": 0,    "end_ms": 1000},
      {"id": "s2", "speaker": "Bob",   "start_ms": 800,  "end_ms": 3000, "locked_track": 2},
      {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 4000}
    ]
  }'

# 4) 并发发起两次接纳：串行生效，响应一致，均为版本 2 快照
curl -s -X POST http://localhost:8080/sessions/$SID/accept &
curl -s -X POST http://localhost:8080/sessions/$SID/accept &
wait
```

两个并发响应都是 `source_revision: 2`、`cursor_ms: 900`、s3 的 `end_ms: 4000`，分轨为 s1→1、s2→2（已开始，固定原分轨）、s3→1。无论请求到达顺序如何交错，会话都不会再回退到版本 1 的内容；可用推进验证（s3 在 2500 时仍在屏，说明其 `end_ms` 是 4000 而非 2000）：

```bash
curl -s -X POST http://localhost:8080/sessions/$SID/advance \
  -H 'Content-Type: application/json' -d '{"time_ms": 2500}'
# {"session_id":"…","scene_id":"scene-race","source_revision":2,"cursor_ms":2500,
#  "active":[{"id":"s2",…,"track":2},{"id":"s3",…,"end_ms":4000,…,"track":1}]}
```

健康检查：

```bash
curl -s http://localhost:8080/health
# {"status":"ok"}
```

## 本地开发与测试（不使用 Docker）

需 Python 3.11+：

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --port 8080
pytest
```

本地（非 Docker）运行时，场景数据库默认写入工作目录下的 `scenes.db`；可用环境变量改到别处，例如 `SCENES_DB_PATH=/tmp/scenes.db uvicorn app.main:app --port 8080`。

核心求解器（`app/solver.py`）无第三方依赖，也可直接运行其测试：

```bash
python tests/test_solver.py
```

## 算法说明

输入规模很小（≤12 条、≤3 轨），采用带剪枝的深度优先枚举：

1. 按输入顺序逐条分配轨道，每个位置按轨道号 1→k 升序尝试；
2. 候选轨道必须未被任何时间重叠的已分配字幕占用，锁定字幕只尝试锁定轨；
3. 代价在“同一说话人按时间排序后相邻”的字幕对两端都落位时累计（换轨计 1）；
4. 剪枝：当前代价不低于已找到的最优解时剪枝。由于按轨道号升序枚举，第一个到达的最优解即为字典序最小的轨道序列，无需额外比较。

实现已用穷举参考解对 4000 组随机实例交叉验证。

## 目录结构

```
app/
  main.py      # FastAPI 路由与请求/响应模型（Pydantic 校验），含片段另存 /scenes/{id}/clip
  solver.py    # 纯 Python 求解器与业务校验（无第三方依赖）
  storage.py   # 场景持久化：本地 SQLite（乐观锁版本控制；片段核对源版本与创建目标为同一事务）
  playback.py  # 播放会话：单进程内存态，推进/事件推进/接纳串行化（接纳含场景读取的完整操作）与保轨重算
tests/
  conftest.py        # pytest 夹具（场景接口使用临时数据库、会话隔离）
  test_solver.py     # 求解器单元测试
  test_api.py        # /assign HTTP 层测试
  test_scenes.py     # 场景创建/修订/读取/版本冲突/并发测试
  test_clips.py      # 片段另存：裁剪/保轨/边界、404/409/422、原子性、源与会话不变、独立性、快照错配回归
  test_playback.py   # 播放会话创建/推进/接纳/冲突/并发测试
Dockerfile
docker-compose.yml   # 挂载 scenes-data 具名卷到 /data
requirements.txt / requirements-dev.txt
```
