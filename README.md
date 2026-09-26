# 自然史标本与实验协作服务

本项目是一套可离线运行的 Python 后台，用于自然史馆、学校实验室和野外调查团队协同管理昆虫、植物及其他生物标本。系统把保藏与转运、分类实验复核、生物安全处置三个业务子域保存在 SQLite 中，提供角色权限、幂等请求、事务状态、版本化记录和可追溯审计。

## 目录

- `src/collection_logistics/`：馆藏环境指标、库房与转运路线、保藏资源、调拨任务和调整情景；
- `src/taxonomy_lab/`：采集设备、实验协议、观察记录导入、异常排除、分析租约、鉴定决定，以及批次结项（物料守恒、双方确认、更正失效）；
- `src/biosafety_ops/`：库区记录、有害生物监测、风险告警、处置工单和资源分配；
- `fixtures/`：离线验收使用的实验协议与结构化观察记录；
- `tests/`：领域规则、事务边界、权限、HTTP API 和命令行验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时只依赖 Python 标准库与 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -q
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m collection_logistics.acceptance --workspace .
PYTHONPATH=src python3 -m taxonomy_lab.acceptance --workspace .
PYTHONPATH=src python3 -m biosafety_ops.acceptance
```

验收会建立临时 SQLite 数据库，登记馆藏环境指标、保藏库房、转运路线和材料批次，完成实验观察导入、异常复核、生物安全告警与资源分配，并输出 JSON 结果。命令不访问公网，也不需要额外数据库、队列或常驻服务。

## HTTP API

```bash
PYTHONPATH=src python3 -m collection_logistics.api --database collection.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m taxonomy_lab.api --database taxonomy.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m biosafety_ops.api --database biosafety.sqlite3 --host 127.0.0.1 --port 8082
```

三个服务均提供 `GET /health`，其余接口使用 JSON。SQLite 文件保存业务状态、幂等结果和审计记录，进程重启后可以继续查询与复核。

## 实验批次结项流程

`taxonomy_lab` 在批次形成采信决定（`decided`）之后增加结项流程，把分散在活体观察、临时玻片、试剂和可入藏样品登记中的材料归拢成不可变结项单：

1. 操作员登记四类物料初始数量：活体（`live_specimen`）、临时玻片（`temporary_slide`）、试剂（`reagent`）、可入藏样品（`collectible_sample`）；登记试剂消耗，馆方记录每条实物去向（`accession` 入藏 / `return` 归还 / `destruction` 销毁）并填写原因，去向类型受物料类别约束。
2. 结项单快照方案版本与摘要、分析结果摘要、采信决定、实际参与者、全部异常排除和物料明细，并校验数量守恒：每份物料 `初始 = 消耗 + 入藏 + 归还 + 销毁`，同时交叉核对已批准排除数与分析剔除数。
3. 状态机：`submitted → instructor_confirmed → confirmed`；另有 `withdrawn`（提交人或指导教师撤回）、`returned` / `partially_returned`（指导教师或馆方整单/部分退回，附字段与原因）、`superseded`（更正失效）。撤回、退回或失效后可按递增 `serial` 重新提交，历史结项单全部保留。
4. 方案或关键观测更正时，指导教师用 `supersede` 让已确认旧结项失效（必须填写原因），批次重开并抬升 `revision`；随后可切换协议版本、重走封存分析与决定。馆方确认时已归档的实物去向只增不改，重新结项必须继承这些归档数量，结项单之间通过 `superseded_by_closeout_id` 串联。
5. 重复提交走幂等键回放；指导教师与馆方重复确认时按 `content_sha256` 回放原结项单，摘要不符即拒绝，不会产生新结果。

结项相关接口（均为 JSON，写操作需 `X-Actor-Id`，提交需 `Idempotency-Key`）：

- `POST /batches/{id}/materials`、`POST /batches/{id}/materials/{stock_id}/consume`、`POST /batches/{id}/dispositions`
- `POST /batches/{id}/closeouts`（提交）、`GET /batches/{id}/closeouts`（版本历史）
- `POST /closeouts/{id}/instructor_confirm`、`/museum_confirm`、`/withdraw`、`/return`、`/supersede`
- `GET /closeouts/{id}`、`GET /batches/{id}/closeout_report`（逐份物料的入藏/归还/销毁原因与守恒结果）
- `POST /batches/{id}/correct_protocol`（更正重开后切换方案版本）

新角色：`instructor`（指导教师：提交、教师确认、宣告更正失效、切换方案版本）与 `curator`（馆方：登记实物去向、馆方确认）。
