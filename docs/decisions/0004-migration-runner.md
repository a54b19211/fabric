# 0004 统一迁移执行器：每库独立账本，未知状态拒绝

- 状态：已实现（分支 `r2/fabric-b1`），待评审。设计：`r2-migration-runner.md` 及 r219 修订（`r2-migration-runner-r2.md`）。
- 日期：2026-11-07

## 决定

- 四个旧执行器（`migrate.py`、`history.py`、`index_publication.py`、`publication.py`）合并为 `migration/runner.py`，旧命令和函数名保留为薄包装。
  整个文件一次交给 PostgreSQL 执行（函数体、注释、引号由 PostgreSQL 解析）；`migration/sqlcheck.py` 只在执行前读一遍文件，
  拒绝顶层事务控制、`CONCURRENTLY`、未闭合引号。`CONCURRENTLY` 走 `migration/online.py`。
- 清单是代码（`migration/manifests.py`）：对象库兼容清单是 V001-V035 的显式元组，之后每个新文件必须声明且只属于一个角色
  （`app` / `history` / `publication`）；测试约束。
- 账本：对象库继续用 `schema_migrations`（一行不改），sha256 另记入事件表；History 与原采集库用只追加事件表
  `fabric_schema_ledger_events` 和视图 `fabric_schema_ledger_current`（触发器拒绝改、删、截断）。更正基线是追加
  `baseline_revoked` 事件，不改旧行。
- 一次运行持有一个 advisory lock（`0x4661627269634d67`，覆盖账本表创建之前到最后一个文件之后）。锁所在的连接就是执行每个文件事务的连接，
  每次提交前核对 `pg_locks` 里本会话仍持有该锁；锁丢了（后端被终止、被释放）就不提交（`MigrationLockLost`）。适用于 apply、adopt、更正、参考数据和在线操作。`--plan` 只读，不建表。
- 没有账本的库：`--adopt-baseline` 只证明“今天的结构等于前 k 个文件的全新安装结构”，写 `observed_baseline` 事件，
  **不表示这些文件曾被执行**。含 DML 的文件必须声明只读后置条件并通过；没有结构痕迹的文件逐个按后置条件判断，不任选最大前缀。
  **没有 `--accept-diff`**：未知结构只给差异报告；出路是 `migration/compat_mappings.py` 里经审阅的具名条目，或新的修正迁移。
- 临时容器（`migration/scratch_db.py`）按记录的容器 ID、标签和卷名拆除，不按名字。
- 原子性：文件、后置条件、账本行在同一个事务里。V008–V011 四个旧文件自带顶层 `BEGIN;`/`COMMIT;`，执行时只把这两条语句
  按原长度换成空白（`neutralise_transaction_control`，执行前由 sqlcheck 再验证没有残留的事务控制）；磁盘上的文件和记录的校验和仍是已发布的字节。
- 采用基线的指纹范围：角色自己创建的表上的**全部**列、约束、索引、触发器、规则都计入，多出任何一项都是差异；
  其他角色的表（如 documents）只比较本角色新增的键。
- 不改任何已发布的 `V*.sql`，不删除或改写任何账本行，不清空旧账本。

## 后果

- 迁移文件在一个账本上只执行一次：测试里“重建旧库再整份重跑”的写法改为只执行旧库缺的那一个文件。
- History 角色现在包含索引表（旧 `migration.history` 只装 9 个文件，`migration.index_publication` 另装 5 个）。顺序交错与旧的两段顺序的
  全新安装结构相同（`tests/integration_migration_equivalence.py`）。
- 现役库第一次必须 `--plan`，再 `--adopt-baseline`；建账之前运行 `--apply` 会被拒绝并提示。
