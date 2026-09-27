# 地震台网事件编目与修订

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8307`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务、乐观锁和历史版本快照。
- `src/service.py`：用例编排、幂等处理、版本控制、修订单审批与审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：可操作的编目与修订演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8307
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。旧数据库启动时会自动把当前行回填为历史版本快照。

## 核心对象

- `station`：观测台站。
- `event`：地震事件，状态机为 `candidate → associated → reviewed → published → revised`（另有 `withdrawn` 撤稿终态）。
- `revision_order`：**独立编目修订单**，把“改震级”从直接编辑事件改为先提交、后审批的独立流程，状态机为 `pending → approved / withdrawn`。

## 编目修订流程（修订单）

事件发布后，分析/复核人员收到补报台站数据时，不再直接改事件，而是提交一张待审修订单：

1. **提交待审修订单** `POST /api/revision_orders`，必填：
   - `event_id`：关联的已发布（`published`/`revised`）事件；
   - `new_stations`：新增台站（补报台站代码列表，或含 `station/time_offset/distance_km` 的报告对象）；
   - `magnitude`：修订震级；
   - `basis`：修订依据（补报数据说明、重新标定依据）。
   - 提交时自动快照事件的 `base_version` 和 `base_magnitude`。
2. **审批通过** `POST /api/entities/<order_id>/actions {"action":"approve"}`（`reviewer`/`admin`）：
   - 仅当事件当前版本仍等于 `base_version` 时才允许通过，否则返回 409「修订单版本已过期」，**不覆盖新数据**，修订单保持待审；
   - 通过后事件变为 `revised`，补报台站并入 `reports`，`magnitude` 与 `revision_count` 同步更新，依据写入 `last_revision_basis`；
   - 事件与修订单在同一事务内更新，事务内再做一次乐观锁检查防止并发覆盖。
3. **撤回修订单** `{"action":"withdraw"}`（提交人/管理员）：修订单变为 `withdrawn`，**事件内容保持原样**；撤回后不可再审批。

### 历史编目按版本保留

`entity_versions` 表在每次创建/更新时归档完整快照。可通过下面接口查询任一对象的全部历史版本，旧版震级、台站和状态都可追溯：

```
GET /api/entities/<id>/versions
```

修订单本身也保留版本快照；审批操作同时向事件（`revise`，含依据与新增台站）和修订单（`approve`）写入审计记录。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询（`events`、`stations`、`revision_orders`），可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `GET /api/entities/<id>/versions`：读取对象全部历史版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 页面操作

打开根路径即可使用演示页面：选择身份（analyst/reviewer/admin…），完成台站注册、事件创建→关联→复核定震→发布；对已发布事件可「提交修订单」（填写新增台站、修订震级、依据），复核人员在修订单表中「审批通过」或「撤回」。待审修订单若检测到基线版本落后会显示「版本已过期」并禁用审批；「历史版本」按钮可逐版本查看旧版编目。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

事件关联使用简化时间差和距离阈值，不包含完整地震定位、震级标定或台站仪器响应。
