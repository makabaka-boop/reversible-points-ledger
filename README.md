# Points Ledger

一个用 **FastAPI + SQLite** 实现的模拟积分账本。账户余额只能是**整数且不可为负**，
每笔转账写一对金额相反、**不可修改**的分录；一批转账在同一个数据库事务里，
**要么全部记账，要么全部不记**；冲正必须引用原交易且**最多成功一次**；管理员可以
关闭一个结算期并保存期末余额快照，关闭后不能再向该期补写分录，旧交易的冲正只进入
当前开放期。转账与关期、两次并发冲正的冲突全部由数据库裁决。

## 验收

```bash
docker compose config --quiet
docker compose build
docker compose run --rm verify
```

`verify` 一次性服务会等待 `api` 健康后运行 25 个测试（重放、并发、故障/回滚、
数据库级不变量），全部通过时退出码为 0。

## 运行

```bash
docker compose up api        # http://localhost:8000 （交互式文档 /docs）
```

所有写接口需要请求头 `X-Admin-Key: admin-secret`（可用环境变量 `ADMIN_KEY` 修改）。

## API 概览

| 方法 | 路径 | 说明 |
| ---- | ---- | ---- |
| POST | `/accounts` | 创建账户 `{"name": "alice"}` |
| GET  | `/accounts` | 账户列表及当前余额 |
| GET  | `/accounts/{id}` | 单个账户 |
| GET  | `/accounts/{id}/entries` | 某账户的全部分录（不可变） |
| POST | `/accounts/{id}/issue` | 发行积分 `{"amount": 1000}`（正整数） |
| POST | `/transfers` | 批量转账，见下 |
| GET  | `/transactions` / `/transactions/{id}` | 交易及分录 |
| POST | `/transactions/{id}/reverse` | 冲正一笔转账（最多成功一次） |
| GET  | `/entries` | 全部分录（对照口径） |
| GET  | `/periods` | 结算期列表 |
| POST | `/periods/close` | 关闭当前开放期、保存快照并开新期 |
| GET  | `/periods/{id}/snapshot` | 读取已关闭期的期末余额快照 |

批量转账请求体：

```json
{
  "transfers": [
    {"from_account": 1, "to_account": 2, "amount": 300},
    {"from_account": 2, "to_account": 3, "amount": 100}
  ]
}
```

金额必须是严格的正整数（`1.5`、`"10"`、`true`、负数和 0 一律 422）。一批中只要有
一笔在提交时会导致某个账户余额为负，整批返回 409 且不留任何痕迹。

### 快速试一下

```bash
curl -s localhost:8000/health
A=$(curl -s -X POST localhost:8000/accounts -H 'X-Admin-Key: admin-secret' -H 'Content-Type: application/json' -d '{"name":"alice"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
curl -s -X POST localhost:8000/accounts/$A/issue -H 'X-Admin-Key: admin-secret' -H 'Content-Type: application/json' -d '{"amount":1000}'
curl -s localhost:8000/accounts
```

## 不变量是如何被保证的

全部关键规则都落在 SQLite schema / 事务里，而不是应用层的先查后写：

1. **整数、非零金额**：`entries.amount INTEGER NOT NULL CHECK (amount <> 0)`，
   API 层 Pydantic 用严格整数（拒绝布尔、浮点、字符串）。
2. **转账成对、方向相反**：每笔 `transfer` 在同一事务插入
   `-amount` 与 `+amount` 两条分录。
3. **整批原子**：一批中的所有交易和分录在一个 `BEGIN IMMEDIATE` 事务内写入；
   事务提交前会从原始分录聚合重算所有账户的最终余额，若任一为负则中止并回滚整批。
   这允许同一批内合法的循环链（A→B、B→C、C→A 净额为零），同时拒绝最终状态透支的批。
4. **余额永不为负、并发由数据库裁决**：每个写事务一开始就取 `BEGIN IMMEDIATE`
   保留锁，因此所有写操作（转账、冲正、关期）在数据库层面串行化；提交前的余额
   聚合检查与提交对其他写者是不可分割的。拿不到锁的请求收到 503 可重试。
5. **分录/交易/快照不可变**：`BEFORE UPDATE/DELETE` 触发器直接
   `RAISE(ABORT, '... immutable')`，原冲正分录永远不会被删除或覆盖。
6. **冲正最多一次**：`reversals.original_txn_id UNIQUE`；冲正插入镜像分录，
   且分录写入当前开放期。两次并发冲正中只有一个事务能提交，另一个得到 409。
7. **关期不可补写**：`entries` 上的 `BEFORE INSERT` 触发器校验目标期必须仍为
   `open`；关期在同一事务里写入每个账户的期末快照、把旧期置为 `closed` 并创建
   下一开放期（部分唯一索引保证任意时刻恰好一个开放期）。旧交易在关期后被冲正时，
   镜像分录只进入新开放期，旧快照保持冻结。

## 对照口径：从原始分录重算余额

测试不信任 API 自报的余额。所有一致性断言都把

```sql
SELECT account_id, SUM(amount) FROM entries GROUP BY account_id
```

当作对照基准（账户全集来自 `/accounts`，无分录账户余额为 0），并核对：

* API 余额 == 从原始分录重算的余额，且始终非负；
* 每笔转账恰好两条金额相反的分录；
* 关期快照 == 关期那一刻从分录重算的余额，之后不再变化；
* 跨期冲正只影响开放期，累计余额仍等于全量分录重放结果。

## 测试

`tests/` 针对**运行中的真实 HTTP 服务**（uvicorn 线程池 + SQLite）发起请求，
而不是仅在进程内调用函数，因此并发测试是真实的并行请求：

* `test_replay.py`：发行、多笔转账、跨期冲正、再次关期的完整历史重放，逐阶段
  用原始分录重算余额对照 API 与快照；
* `test_concurrency.py`：
  * 12 个线程同时从只有 5 积分的账户转出 —— 恰好 5 笔成功、7 笔 409，余额永不
    为负；
  * 10 个线程同时冲正同一笔交易 —— 恰好 1 笔 201、其余 409，原分录保留；
  * 转账与关期同时发生 —— 没有任何分录漏进已关闭期，快照与分录始终一致，随后对
    关闭期交易的冲正只进入新开放期；
* `test_rollback.py`：非法整批回滚、批处理中途注入异常回滚、`SIGKILL` 杀死持有
  未提交事务的进程后重开无半截数据且库立即可写、连续失败后服务仍一致可用；
* `test_rules.py`：非负、整数、整批原子、冲正规则、关期不可补写、唯一开放期、
  管理员鉴权，并直接用 sqlite3 连接验证 UPDATE/DELETE 分录和快照、向关闭期插入
  分录、重复冲正等操作都会被数据库拒绝。

本地不使用 Docker 时也可以直接跑（测试通过共享数据库文件复位）：

```bash
pip install -r requirements.txt -r requirements-dev.txt
LEDGER_DB=/tmp/ledger.db uvicorn app.main:app --host 127.0.0.1 --port 8000 &
VERIFY_BASE_URL=http://127.0.0.1:8000 LEDGER_DB=/tmp/ledger.db pytest -v tests
```

## 布局

```
app/db.py       SQLite 连接、schema、触发器、BEGIN IMMEDIATE 事务助手
app/ledger.py   账户/转账/冲正/关期的事务性服务逻辑
app/main.py     FastAPI 路由与严格整数校验
tests/          针对真实服务的重放、并发、故障回滚与不变量测试
Dockerfile          api 服务
Dockerfile.verify   一次性 verify 测试服务
docker-compose.yml  api + verify（共享命名卷上的 SQLite 文件）
```
