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
  --repo superdiaodiao/quant_stocks \
  --title "quant_stocks 数据快照 2026-07-24" \
  --notes "用于复现 can-slim-top3-v1 的完整数据快照。"
```

不要把 `dist/` 中的压缩包提交进 Git。

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
  --repo superdiaodiao/quant_stocks \
  --title "Research cache: SEC Company Facts 2025-04-14" \
  --notes "Research-only reproducibility cache; not a formal data release." \
  dist/research-cache-sec-companyfacts-2025-04-14/*
```

已发布的归档入口：
[`research-cache-sec-companyfacts-2025-04-14`](https://github.com/superdiaodiao/quant_stocks/releases/tag/research-cache-sec-companyfacts-2025-04-14)。

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
