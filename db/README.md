# db/ — 数据与基线

本目录存放**可版本化的数据资产**，与 `conf/`（行为配置）分离：

| 文件 | 用途 | 生产替换 |
| --- | --- | --- |
| `feeds.json` | 离线舆情信源 fixture（虚构数据，9 条，含 1 条注入样本） | 舆情采集管道 + 信源可信度服务 |
| `baseline.json` | 各评测套件的指标基线，CI 门禁据此做 regression diff | 评测平台（LangSmith / Braintrust）的基线表 |

约定：

- 目录内只放**只读数据**，运行时产物一律写入 `artifacts/`（已 gitignore）；
- `feeds.json` 中所有外部正文都被 harness 当作不可信数据，不参与系统提示；
- `baseline.json` 带 `suite` 字段，门禁只与同名套件的基线比较，避免跨套件误判回归；
- 更新基线：`python main.py eval --suite nightly --update-baseline`。

`conf/` 与 `db/` 的职责边界：**改 conf 改变系统行为，改 db 只改变输入数据与比较基准**。
