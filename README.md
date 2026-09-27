# 地震台网事件编目与修订

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8307`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8307
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `station`：观测台站；`event`：地震事件及其多个修订版本；`revision_order`：发布后的编目修订修订单。

## 编目修订流程

事件发布（`published`）后，补报台站数据不再允许直接改事件震级，必须走独立修订单：

1. 复核人员创建 `revision_order`，必填 `event_id`、`added_stations`（新增台站报告）、`magnitude`（修订震级）、`basis`（修订依据）。服务端快照提交时的事件版本 `event_base_version`，订单进入 `pending`。
2. 审批人对修订单执行 `approve`：事件在同一事务内变为 `revised`，合并新增台站、写入新震级与依据、`revision_count` 加一；事件与订单各自产生新版本。
3. 历史编目通过 `GET /api/entities/<id>/versions` 按版本完整保留（含每次震级、台站列表和状态）。
4. 若修订单提交后事件已被其他订单修订（当前版本与 `event_base_version` 不一致），审批返回 `409 ConflictError`（版本已过期），旧审批不会覆盖新数据，需基于新版本重新提交。
5. `withdraw`（撤回）和 `reject`（驳回）只改变修订单状态，事件内容与版本保持原样。

修订单状态机：`pending → approved / rejected / withdrawn`。演示页面 `static/index.html` 支持提交、审批（过期会前置提示）、驳回、撤回及历史版本查看。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤；修订单还支持`?event_id=`过滤。
- `POST /api/<kind>`：创建对象（含 `POST /api/revision_orders`）；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `GET /api/entities/<id>/versions`：读取对象全部历史版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

事件关联使用简化时间差和距离阈值，不包含完整地震定位、震级标定或台站仪器响应。
