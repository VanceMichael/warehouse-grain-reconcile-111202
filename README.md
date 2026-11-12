# grain-reconcile

本地数据集合装载、查询与仓库对账。渠道交接时结算按订单收费、履约按包裹记数量，直接 join 会把拆成多个包裹的订单重复计费；本仓库改为：订单应收先按约定权重分摊到包裹，再与承运商逐包裹实结额核对，差额定义为实结减应收。不拿金额与件数相减，也不对结果去重。

## 数据组织

输入表（`warehouse/definition.py` 的 `SCHEMA`）：

- `charges`：`line TEXT PRIMARY KEY, order_id TEXT NOT NULL, channel TEXT NOT NULL, business_date TEXT NOT NULL, currency TEXT NOT NULL, amount TEXT NOT NULL`
- `packages`：`package TEXT PRIMARY KEY, order_id TEXT NOT NULL, units INTEGER NOT NULL`
- `settlements`：`line TEXT PRIMARY KEY, package TEXT NOT NULL, channel TEXT NOT NULL, business_date TEXT NOT NULL, currency TEXT NOT NULL, amount TEXT NOT NULL`
- `channel_mappings`：`version, source_channel, target_channel, effective_from`，主键 `(version, source_channel, effective_from)`
- `allocation_rules`：`order_id TEXT PRIMARY KEY, mode TEXT NOT NULL`，模式为 `explicit` 或 `units`
- `allocation_weights`：`order_id, package, weight`，主键 `(order_id, package)`

运行时表（`RUNTIME_SCHEMA`）：`recon_runs`（运行记录：指纹、参数、状态、汇总）、`recon_results`（分区结果）、`recon_allocations`（分摊步骤快照）、`recon_settlements`（参与运行的结算行快照）。`schema.json` 描述全部结构。

`examples/source.json` 为本地合成输入，`examples/rules.json` 为按币种配置的容差与精度，`examples/mapping_v2.json` 为映射修订样例，`examples/expected.json` 与 `examples/expected_reconcile.json` 是公开样例结果。输入批次以内容 SHA-256 固定；相同批次重投不重复写入，相同批次内容不同则报错。装载以完整输入包为事务，主键冲突不留下半批记录。结算行按标识幂等吸收：重投同内容不重复计入，同标识不同内容阻断该次发布。金额用十进制文本，导出对象按内容哈希命名并重新校验。

## 对账语义

- **分摊**：收费行按订单的分摊规则拆到包裹。`explicit` 用登记权重；`units` 以包裹数量作为权重依据——数量只是权重规则可选的依据，不直接当作金额。权重不足、规则缺失、权重指向未知包裹都阻断运行，绝不随意均分。
- **尾差**：分摊金额按币种精度量化（ROUND_HALF_UP），尾差稳定落到权重最大（并列取包裹号最小）的包裹，同一输入重跑归属不变；逐行守恒：各包裹分摊合计恒等于收费行金额。
- **渠道归一**：按映射版本与生效日期把来源渠道归一到目标渠道，多个旧渠道可映到同一新渠道；映射缺失或重复阻断运行，不从包裹号猜测渠道。不同币种从不相加。
- **核对**：按（包裹、归一渠道、业务日期、币种）汇总两侧，差额 = 实结 - 应收。同一业务键的多条合法结算明细都参与累计。没有结算记录（`settled` 为 `null`）与金额确为零（`"0.00"`）分开表达。
- **分类**：`matched`、`within_tolerance`（|差额| ≤ 容差，边界含等于）、`true_difference`、`indeterminate`（缺侧数据或币种未配置容差）。存在 `indeterminate` 或 `true_difference` 时不判定通过。
- **运行**：一次运行固定双方水位与映射版本，运行记录持久化。同标识同输入重跑幂等返回原结果；同标识不同输入拒绝覆盖。失败只留 `failed` 记录，不留下被当成完整的批次，修正后可同标识重跑。
- **映射修订**：`--reuse-from` 只重算受影响渠道的分区，未受影响的分摊与结算明细整体继承；增量结果与全量重算逐行一致（要求两侧水位、币种规则与输入数据未变）。
- **钻取与校验**：`explain` 从差额钻回收费行、结算行、分摊步骤（权重、份额、尾差标记）与规则（映射、分摊、容差、精度）；`verify` 自动校验逐行分配守恒、分区合计、差额与分类，而非仅比较总额。

## 环境与验证

Python 3.11 以上，仅使用标准库，无第三方安装。`runtime.txt` 记录验证解释器版本。数据库和对象目录均由入口参数指定。

```bash
python3 -m compileall -q warehouse tests scripts
python3 -m unittest discover -s tests -v
```

## 运行

每次使用全新临时目录执行真实导入、对账、钻取、校验、映射修订与导出：

```bash
python3 scripts/demo.py
```

分步调用可用：

```bash
python3 -m warehouse import --db /tmp/grain.sqlite3 --input examples/source.json --batch main
python3 -m warehouse inspect --db /tmp/grain.sqlite3
python3 -m warehouse reconcile --db /tmp/grain.sqlite3 --run run-v1 --mapping-version v1 \
  --charge-watermark 2026-11-04 --settlement-watermark 2026-11-04 --rules examples/rules.json
python3 -m warehouse results --db /tmp/grain.sqlite3 --run run-v1
python3 -m warehouse explain --db /tmp/grain.sqlite3 --run run-v1 --package p2 --channel express --date 2026-11-01 --currency CNY
python3 -m warehouse verify --db /tmp/grain.sqlite3 --run run-v1
python3 -m warehouse reconcile --db /tmp/grain.sqlite3 --run run-v2 --mapping-version v2 --reuse-from run-v1 \
  --charge-watermark 2026-11-04 --settlement-watermark 2026-11-04 --rules examples/rules.json
python3 -m warehouse runs --db /tmp/grain.sqlite3
python3 -m warehouse export --db /tmp/grain.sqlite3 --objects /tmp/grain-objects
```

`scripts/demo.py` 用合成数据展示拆单、多币种、重复明细、缺侧数据、映射修订、失败语义与可重入执行，打印实际命令和完整输出。小规模样例在数秒内结束，失败返回非零退出码，不回放预制日志。手工重复使用数据库路径时应留意已存在的业务主键与运行标识。
