# 数据包与数据 Release

> 从原 README 第 4、5、11、14 节移来。下载命令见 README 的“首次安装”。

## 4. 为什么数据不再直接放进 Git

项目数据包含数千只股票的历史价格、PIT 财务数据和数百个股票池快照：

- `cleaned_stocks_data`：约 450 MB；
- `stocks_list_dir`：约 327 MB；
- `his_data`：约 553 MB；
- 未压缩合计超过 1.3 GB。

如果继续直接提交 CSV：

- 每次更新都会产生大量 Git diff；
- 历史 Blob 永远保留，仓库会持续膨胀；
- clone、fetch、review 和回滚越来越慢；
- 将 CSV 改成 `.gz` 后直接提交也不能解决版本膨胀，因为 Git 很难对压缩二进制做增量存储。

因此项目采用：

- **Git 仓库**：代码、测试、中文说明、小型正式回测结果；
- **GitHub Release**：完整版本化数据包；
- **SHA-256**：保证下载内容与发布内容一致。

## 5. 数据包内容

当前数据 Release：

- Tag：`data-2026-07-24`
- 文件：`quant_stocks_data_2026-07-24.tar.zst`
- 元数据：`data_release/latest.json`
- 位置：公开仓库 `superdiaodiao/quant_stocks` 的 Release（CI 从这里下载）；私有数据仓库
  `superdiaodiao/quant_stocks_data` 有一份逐字节相同的副本（见第 15 节）。

数据包包含：

```text
cleaned_stocks_data/
├── price/                         # 清洗后的股票价格
└── financial/                     # EPS、季度财务、原始 SEC 缓存和覆盖率

stocks_list_dir/
└── nasdaq/
    ├── nasdaq_300M.csv            # 当前候选股票池
    ├── nasdaq_index.csv           # Nasdaq Composite
    ├── snapshots/                 # PIT 历史股票池快照
    ├── corporate_actions.csv      # 公司行动
    ├── security_identity.csv      # 证券类型识别
    └── terminal_returns.csv       # 退市/终止收益

his_data/
├── us/nasdaq/                     # 原始历史数据，供旧初始化流程恢复
└── us/sp500/vix.csv               # 旧策略使用的 VIX 历史
```

`output/` 不放入数据包。正式回测结果保存在 Git，日常推荐和本机审计结果由运行环境自行积累。

## 11. 创建新的数据 Release

完成数据更新和审计后，生成数据包：

```bash
chmod +x scripts/create_data_release.sh
scripts/create_data_release.sh 2026-07-24
```

输出：

```text
dist/
├── quant_stocks_data_2026-07-24.tar.zst
├── quant_stocks_data_2026-07-24.tar.zst.sha256
└── quant_stocks_data_2026-07-24.json
```

建议发布步骤：

1. 数据审计 PASS；
2. 重跑固定策略；
3. 全量测试通过；
4. 生成 `.tar.zst`；
5. 再次核对 SHA-256；
6. 更新 `data_release/latest.json`；
7. 提交并推送代码；
8. 创建 GitHub Release 并上传三个文件。

使用 GitHub CLI：

```bash
gh release create data-2026-07-24 \
  dist/quant_stocks_data_2026-07-24.tar.zst \
  dist/quant_stocks_data_2026-07-24.tar.zst.sha256 \
  dist/quant_stocks_data_2026-07-24.json \
  --repo superdiaodiao/quant_stocks_data \
  --title "quant_stocks 数据快照 2026-07-24" \
  --notes "用于复现 can-slim-top3-v1 的完整数据快照。"
```

不要把 `dist/` 中的压缩包提交进 Git。新的数据包发到私有数据仓库。注意：GitHub Actions 用默认的
`GITHUB_TOKEN` 读不到私有仓库的 Release，所以只要 v50r3 / sue-lt-v1 工作流还经
`scripts/v50r3_restore_data.sh` 下载数据，`data_release/latest.json` 指向的 Release 就必须能被
工作流读到（留在公开仓库，或给工作流配一个能读私有仓库的 token secret）。

## 14. 研究缓存冷归档与恢复

SEC Company Facts 的 Wayback 历史快照体积约 33GB，不适合提交到 Git。两份
18,595-CIK 快照的全量审计记录在
`output/data_provenance/companyfacts_snapshot_variant_audit_2026-08-10.json`：
16,997 个文件字节完全一致，其余 1,598 个文件去掉顶层 `symbols` 元数据后也完全
一致，`semantic_difference_count=0`。因此冷归档只保存
`wayback-20250414-symbols-v2`；原始 WARC URL、ZIP SHA、capture 时间和展开统计由
`companyfacts_wayback_warc_extraction.json` 绑定。

创建可上传的 zstd 压缩分卷（每卷不超过 1900MB）以及机器可读 catalog：

```bash
scripts/create_research_cache_archive.sh
```

产物位于 `dist/research-cache-sec-companyfacts-2025-04-14/`，catalog 同时写到
`research_cache/sec-companyfacts-2025-04-14.json`。catalog 固定 snapshot manifest、
source evidence、variant audit、整包 SHA-256、每个分卷的大小和 SHA-256。上传到独立的
research-only GitHub Release 时，目录内也会带上两份 evidence JSON，使用：

```bash
gh release create research-cache-sec-companyfacts-2025-04-14 \
  --repo superdiaodiao/quant_stocks_data \
  --title "Research cache: SEC Company Facts 2025-04-14" \
  --notes "Research-only reproducibility cache; not a formal data release." \
  dist/research-cache-sec-companyfacts-2025-04-14/*
```

已发布的归档入口：
[`research-cache-sec-companyfacts-2025-04-14`](https://github.com/superdiaodiao/quant_stocks_data/releases/tag/research-cache-sec-companyfacts-2025-04-14)
（私有数据仓库，下载需要 `gh auth login`；catalog 里的 `repository` 字段仍是最初发布的公开仓库，
catalog 有 SHA 绑定所以不改，恢复脚本默认从私有仓库下载，可用 `QUANT_DATA_REPO` 覆盖）。

下载并恢复时必须显式给出恢复父目录；脚本拒绝覆盖已存在的同名快照。若 parts 目录
已经包含全部分卷（例如手工下载），不会重复联网下载：

```bash
restore_parent="$(mktemp -d)"
scripts/restore_research_cache_archive.sh \
  research_cache/sec-companyfacts-2025-04-14.json \
  dist/research-cache-restore \
  "$restore_parent"
```

恢复流程依次验证每个分卷 SHA、拼接后的整包 SHA、解压后的 snapshot manifest；只有
三层都通过才报告成功。该快照仍缺当前后续研究范围内的 104 个 symbol，只能作为
research archive 和离线复放输入，不能替代正式 annual/quarterly 数据、正式 validation
或解除 `BLOCKED` 状态。

## 15. 私有数据仓库与按数据集备份

全部研究数据备份在**私有**仓库
[superdiaodiao/quant_stocks_data](https://github.com/superdiaodiao/quant_stocks_data)。仓库只放
Release，不放代码；因为是私有的，数据不加密。下载需要 `gh auth login`，账号要有仓库权限。

每个数据集一个 Release，tag 为 `cache-<数据集>-<YYYY-MM-DD>`，包含：

- `<数据集>-<日期>.tar.zst.part-000` ……：tar（pax 格式）+ zstd 压缩包，切成小于 1.9 GiB 的分片
  （默认 256 MiB，分片并行上传更快）；
- `<数据集>-<日期>.parts.sha256`：每个分片的 SHA-256；
- `<数据集>-<日期>.manifest.json`：每个文件的相对路径、大小和 SHA-256，以及整包 SHA-256、
  来源目录和生成时间。

包内路径相对于项目根目录（`research_cache/...`、`cleaned_stocks_data/...`、
`output/research_only/...`）。`research_cache/`、`cleaned_stocks_data/` 和 `stocks_list_dir/`
从主 checkout 读（worktree 里这几处是指向主 checkout 的符号链接），`output/research_only/`
从运行脚本的 checkout 读。

数据集（`scripts/backup_data.sh list`）：

- 冻结数据版本：`reversal-v1`、`reversal-v2`、`reversal-v2-1`（对应的
  `research_cache/reversal_2012_2026*`，加上未进 git 的
  `output/research_only/reversal_2012_2026/inputs*/special_distributions.csv`），以及原始缓存
  `reversal-v2-fill`、`reversal-v2-1-alpaca`；
- `stocks-data`：`cleaned_stocks_data/` 和 `stocks_list_dir/`（比 `data-2026-07-24` 多出 SEC
  companyfacts 缓存和之后更新的价格与快照，所以单独备份）；
- `research-outputs`：`output/research_only/` 下未进 git 的研究结果（未跟踪和被忽略的文件）；
- `research_cache/` 下其余每个目录各是一个数据集，名字把 `_` 换成 `-`，例如 `sec-alt`、
  `osap`、`t-grid`；`borrow-fees`、`forward-observation` 是还在更新的实时缓存，每次备份是一个
  带日期的快照。

跳过 `.DS_Store`、`__pycache__`、`*.pyc`。

```bash
scripts/backup_data.sh <数据集|all|list>
scripts/restore_data.sh [--force] [--keep-download] <tag> [目标目录]
```

`backup_data.sh` 先确认目标仓库是私有的，在 git 之外的临时目录打包、写清单、上传，再重新下载、
核对分片和整包 SHA-256、解压到临时目录逐个文件对照清单，最后拿清单对照现场文件（实时缓存在
备份之后变化是正常的，只提示不报错）；验证通过后删除临时文件。同一天已有的 tag 会跳过。
环境变量：`QUANT_DATA_REPO`、`QUANT_DATA_ROOT`、`BACKUP_DIST`、`BACKUP_DATE`、`ZSTD_LEVEL`、
`PART_SIZE`、`UPLOAD_JOBS`、`KEEP_DIST=1`。

下载用多个并行 HTTP range 请求（`scripts/data_backup_manifest.py download`，并发数
`QUANT_DOWNLOAD_WORKERS`，默认 48）：从部分网络访问 GitHub 时单个连接只有几十 KB/s。每个下载的
资产都对照 GitHub 报告的 SHA-256 digest。

`restore_data.sh` 也能恢复两个从公开仓库复制来的旧 Release：`data-2026-07-24`（单个压缩包加
`.sha256`）和 `research-cache-sec-companyfacts-2025-04-14`（恢复到
`output/data_provenance/companyfacts_historical_snapshots/`）。

手动恢复：

```bash
gh release download <tag> -R superdiaodiao/quant_stocks_data -D restore
cd restore
shasum -a 256 -c *.parts.sha256
cat *.tar.zst.part-* | zstd -dc | tar -xf - -C <目标目录>
```
