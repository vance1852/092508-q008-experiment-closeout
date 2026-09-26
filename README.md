# 自然史标本与实验协作服务

本项目是一套可离线运行的 Python 后台，用于自然史馆、学校实验室和野外调查团队协同管理昆虫、植物及其他生物标本。系统把保藏与转运、分类实验复核、生物安全处置三个业务子域保存在 SQLite 中，提供角色权限、幂等请求、事务状态、版本化记录和可追溯审计。

## 目录

- `src/collection_logistics/`：馆藏环境指标、库房与转运路线、保藏资源、调拨任务和调整情景；
- `src/taxonomy_lab/`：采集设备、实验协议、观察记录导入、异常排除、分析租约、鉴定决定，以及联合实验结项流程（材料台账、不可变结项单、双边确认、入藏档案与数量守恒）；
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

## 联合实验结项流程

`trial_closure` 建立在实验批次（`draft → running → sealed → analyzed → decided`）之上，批次形成采信决定后即可结项。

- 新增角色：`instructor`（指导教师）、`museum_officer`（馆方接收负责人）。
- 材料台账：`POST /materials` 登记活体观察、临时玻片、剩余试剂、可入藏样品四类材料；`POST /materials/{id}/consumptions` 追加样品消耗，累计消耗不得超过登记数量。
- 结项单：`POST /batches/{id}/closures` 汇总方案版本（含内容摘要）、实际参与者、异常排除结论、分析/决定版本和每份材料的入藏/归还/销毁去向；提交时强制数量守恒（初始 = 已消耗 + 已入藏 + 已归还 + 已销毁），材料覆盖不完整直接拒绝。
- 双边确认：结项单状态为 `submitted → instructor_confirmed → confirmed`。`POST /closures/{id}/confirm` 需带 `Idempotency-Key` 与 `party`；每次确认（含重复确认）都回放冻结快照、重算摘要并复核守恒，同键回放返回原结果，换键重复确认被拒绝。
- 状态分支：教师可在确认前 `POST /closures/{id}/withdraw` 撤回（保留原因）后重新提交，新版本号递增、旧版本保留；馆方确认后只能由教师 `POST /closures/{id}/invalidate`（类别为 `protocol_correction` 或 `observation_correction`）声明作废并保留原因，之后才能重新提交。
- 已归档实物去向不可覆盖：馆方确认时只向 `accession_records` 追加不可变入藏档案（含 catalog_code 与原因）；旧结项作废或重新提交都不会删除或改写档案，已入藏数量计入后续守恒基数。
- 部分退回：`POST /closures/{id}/partial_return` 仅馆方可用，把已确认的“销毁”等量改判为“退回”；冻结明细不被 UPDATE，改判以追加的调整记录保存，读取与守恒查询时叠加。
- 查询：`GET /batches/{id}/closures` 查看版本链，`GET /batches/{id}/conservation` 回答结项前后数量是否守恒，`GET /materials/{id}/trace` 回答每份样品为何入藏、归还或销毁（含消耗台账、各版结项决定与入藏档案）。
