# sue-lt-v1 规则 2020–2026 检验：数据报告

> 按 [检验规则](sue_lt_2020_2026_plan.md) 第 5 节，运行之前提交。编写过程中**没有计算任何收益**；
> 只统计了覆盖率和每月选股名单（用于核对终值来源）。结论：数据标准全部满足，可以运行。

输入在 `output/research_only/sue_lt_2020_2026/inputs/`；Tiingo 价格只存本地，由 `manifest.json` 的
SHA-256 绑定。

## 1. 上市状态

- 2020 年以后按仓库的 357 份 Nasdaq 官方上市名单快照判断（间隔不超过 34 天）。
- 期间改过代码的 11 家（`nasdaq_symbol_history_2020.csv`：FB→META、NLOK→GEN、VIAC→PARA、WLTW→WTW、
  YY→JOYY、AAXN→AXON、BGNE→ONC、QRTEA→QVCGA、SGMS→LNW、CHK→EXE、DWAC→DJT），旧代码均在当时快照中核实。
- 2022-06-24 的快照不完整（缺 ASML、JD、PDD 等约 30 只），规则：前后两份快照都在即视为在上市。
- 从 NYSE 转来的（Walmart、Honeywell、Palo Alto、Palantir、Linde 等）在转板前不在快照中，按规则排除；
  CCIV→Lucid、IPOE→SoFi 在合并前是 NYSE 上市的 SPAC，同样排除。
- 11 个已知案例抽查全部正确（META 2021、HON 2020 与 2023、ASML 2022-06、WMT 2023、PARA 2021、
  ALXN 2020、FISV 2022 与 2024、PLTR 2023、AAPL 2020）。

## 2. 2020–2024 退市的大公司（防幸存者偏差）

- 从 SEC 取得 2020-01 至 2026-07 全部由 Nasdaq 提交的退市通知（Form 25-NSE），共 2,508 条、2,127 家
  （`sec_form25_nasdaq_2020_2026.csv`），再用各公司年报披露的公众流通市值筛出较大的公司。
- 本地价格文件已含 2024 年下半年以后退市的公司；此前退市的大公司从 Tiingo 补了 47 家，包括
  **SVB（SIVB）、Signature Bank、Bed Bath & Beyond、SunPower**（这四家保留退市后的场外交易价格，若持有
  其崩盘会自然计入），以及 E*TRADE、Horizon、Abiomed、Zynga、DISH、Shockwave、Mirati、People's United、
  PacWest、Mellanox、Cypress、FLIR、Proofpoint、Change Healthcare、Mandiant、CyrusOne、Syneos 等。
  2011–2019 检验已补的 Alexion、Xilinx、Maxim、Cerner、Citrix、Activision、Seagen、Splunk、Nuance 继续使用。
- 退市后的补位行（价格不变）按“末尾连续相同收盘价截到第一天”处理。
- 同时从 SEC 补了这些公司的季度净利润（`quarterly_supplement_2020.csv`）。Signature Bank 向 FDIC 而非
  SEC 报送财报，没有 SUE，不会被选中。
- **仍缺**：TD Ameritrade、Coupa（Tiingo 上的代码对应别的公司）。

## 3. 覆盖率

- 纳指 100 成分股价格覆盖率：2020–2024 每年约 99%（门槛 95%）。
- 股票池中能算出 SUE 的比例：中位数 90%，最低 67%（门槛：中位数 ≥ 80%）。低于 80% 的月份都是 1 月
  （年报尚未提交），与回测一致。
- 79 个月度信号，股票池中位数 2,805 只，每月流动性池均满 100 只。

## 4. 终值

- 本地已有 2024–2026 年的 210 条终值记录；2011–2019 检验的 11 条继续使用。
- 本次补充（`terminal_returns_2020.csv`，对价取自 SEC 完成交割的 8-K）：Alexion +0.12%
  （$60 + 2.1243 股 AZN ADS，ADS 价格已按 2026-02-02 的比例变更还原为当时的 57.77 美元）、Xilinx +1.03%
  （1.7234 股 AMD）、Walgreens −4.42%（$11.45 现金，或有权利按 0 计）。
- 其余提前结束、没有来源的序列按最后收盘价结束（规则第 5.4 条），运行时逐一列出。曾被选中的只有
  Etsy（2025 年底转到 NYSE 继续交易，按最后收盘价结束等同于在转板前卖出）。

## 5. 5 美元门槛

复权价在 2020–2026 曾低于 5 美元、且成交额排进前 250 的 24 只股票，用 Yahoo 只调拆股的价格还原当时真实价格
（新取 18 只；BITF、QRTEA 在 Yahoo 已无记录，沿用复权价）。

## 6. 结论

| 规则第 5 节 | 结果 |
|---|---|
| 纳指 100 价格覆盖率 ≥ 95% | 约 99%，满足 |
| SUE 覆盖率中位数 ≥ 80% | 90%，满足 |
| 终值有来源或列出 | 满足 |
