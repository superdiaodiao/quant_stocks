# 数据审计与重跑固定策略（研究用）

> 从原 README 第 7.2、7.3 节和原第 16 节之后的 SEC 研究小节移来。

### 7.2 审计数据

将日期替换为数据包对应的最近完整数据日期：

```bash
PYTHONPATH=. .venv/bin/python -m src.research.data_audit \
  --as-of 2026-07-24
```

审计重点检查：

- Nasdaq 指数日期不能来自未来；
- Nasdaq 指数必须覆盖审计日期当天或之前最近一个已完成的官方交易日；周末和
  节假日会自动回退到上一交易日，但缺少任何一个已经完成的交易日都会失败，
  不再用“5 个自然日以内”掩盖连续行情缺口；
- 历史回放还会逐日对照 Nasdaq 官方交易日历，检查基准是否缺交易日、重复日期、
  混入非交易日或存在非法收盘价；任一异常都会阻止历史数据完整性通过；
- 当前股票池价格覆盖率至少 95%；
- 新鲜财务覆盖率至少 90%；
- 直接复用正式 selector 的 TTM 同比计算：在 550 天正式上限内能够实际算出
  `net_income + revenue` 增长的股票中，200 天内仍新鲜可算的比例至少 90%；
  同时单独披露全股票池原始覆盖率和简单指标覆盖率，避免把外国发行人等结构性
  不覆盖与本次更新失败混为一谈；
- SEC Company Facts 的 Q2/Q3 有时只披露半年或九个月累计利润。解析器会在同一
  累计起点、申报日不晚于当前申报、相邻季度间隔 60–135 天的条件下，用累计值之差
  推导单季值，并把来源标记为 `derived_ytd:*`；显式单季值始终优先。此前只推导
  Q4，会把 MNST 等公司误报为季度链不足。对 9 个高优先级代码做官方 SEC
  定向刷新后新增 29 条推导记录（MNST 28 条、TCBI 1 条），MNST 的 37 条潜在候选
  财务缺口全部消除；缺口观测由 3274 降至 3237，代码由 421 降至 420，Top 3 和
  年度收益均未改变。其余银行/外国发行人缺口不是同一种累计值问题；
- SEC 对部分银行使用 `RevenuesNetOfInterestExpense`，官方定义为包含净利息收入
  及交易损益等的总收入，因此可以作为银行 revenue；`InterestIncomeExpenseNet`
  只表示净利息收入，不能替代总收入。官方 Company Facts 验证显示该标签对当前
  2021–2026 缺口没有实际改善，只为 HWC 增加了 4 条早期记录。定义和原始事实取自
  `https://data.sec.gov/api/xbrl/companyfacts/CIK0000750577.json`；
- BPOP、UMBF 等银行没有统一总收入标签，但同时披露标准化的净利息收入和非利息
  收入。解析器只在两项具有相同财政期、申报日和 accession 时合成为
  `derived_bank_revenue:*`，显式总收入仍优先。7 家银行新增 912 条可追溯记录，
  消除 224 条潜在候选缺口和 7 个代码，使覆盖率从 78.51% 升至 80.05%。
  FCNCA 因此在 2023-07、2023-10、2023-11、2024-01 和 2024-03 五个信号进入
  Top 3，使 2023 年收益从 41.47% 更新为 33.95%，2024 年从 253.41% 更新为
  291.65%；这再次证明缺失候选会实质改变历史成绩。继续定向刷新 FITB、CBSH、
  FFIN、SLM、PNFP、IBOC、AGNC 后又消除 144 条缺口和 5 个代码，收益与 Top 3
  未再变化；OZK 的 SEC 当前 ticker map 指向无效 Company Facts 地址并返回 404，
  需要另行确认历史 CIK；
- BKNG 在 2015 年后不再持续披露 `NetIncomeLoss`，但持续披露
  `NetIncomeLossAvailableToCommonStockholdersBasic`。解析器现在仅在公司净利润
  和 `ProfitLoss` 都不可用时，才把普通股股东可得利润作为最低优先级 fallback，
  并保留原始 concept。对 BKNG、PAYX 定向刷新后又消除 65 条缺口，代码减少 2 个；
  该修复通过横截面百分位间接使 2023-03-31 的第三名由 `SNEX` 变为 `AGYS`，
  因而 2023 年收益从 43.34% 降至 41.47%，旧成绩已按新数据如实更新；
- 历史优先级还包含已退出当前股票池、因而不在 SEC 当前 ticker map 的代码。
  定向刷新现在支持显式 `--ticker-cik TICKER=CIK`，未知历史代码会明确失败而不是
  静默显示 `requested=0`。SEC Company Facts 确认 COOP 的历史 CIK 为 `933136`；
  补齐后其 36 条缺口全部消除，并在 2021-04-30、2021-09-30 两个信号直接进入
  Top 3，分别替换 `ORGO`、`ARCB`，使 2021 年收益从 47.45% 更新为 55.66%；
- 同样通过 SEC 官方 Company Facts 实体名验证 ALTR=`1701732`、
  BECN=`1124941`、ANSS=`1013462` 三个已退出当前 ticker map 的历史 CIK；定向
  刷新后消除 89 条缺口和 3 个代码，Top 3 与收益未再变化；
- HOLX=`859737` 经官方实体名验证后，与 PNFP、SKYW、VIRT、XEL、NWE 一起刷新，
  又消除 71 条缺口和 3 个代码。VIRT 在 2021-01 信号进入 Top 3 并替换 FRHC，
  SKYW 在 2024-10、2025-01 信号分别替换 ITRI、DAVE；2021 收益因此从 55.66%
  更新为 47.37%，2024 从 291.65% 更新为 297.31%，2025 从 22.40% 更新为
  12.63%，使历史跑赢年份从 5/6 降为 4/6；
- 继续核对 SEC 实体后确认 PNFP 在 2025 年末由旧主体 CIK `1115055` 迁至
  新主体 `2082866`，NWE 在 2023 年由旧主体 CIK `73088` 迁至新控股主体
  `1993004`；补抓旧主体后两者分别恢复 294、278 条季度记录。XEL 在 2019 年后
  改用标准总公用事业收入标签 `RegulatedAndUnregulatedOperatingRevenue`，
  解析器新增该总收入标签后恢复近年收入链。三项合计消除 68 条潜在候选缺口和
  3 个代码，Top 3 与年度收益未改变；
- VLY 的银行收入和 VTRS 的普通股股东利润仍是旧解析产物，强制重建后分别恢复
  142 条银行收入、44 条此前遗漏的净利润记录，又消除 42 条缺口和 2 个代码。
  VLY 本身没有进入 2021-02-26 Top 3，但加入合格横截面后改变了百分位排名，
  使第三名由 `FRHC` 变为 `AMKR`；2021 年收益由 47.37% 更新为 46.55%，其余
  年度不变。候选财务覆盖率最终由 82.13% 提升至 82.88%，同时再次证明未入选
  候选也可能通过横截面排名间接改变选择；
- PCVX、TSEM、CCOI 的后续核对显示三种不同情形：PCVX 有完整净利润事实但没有
  SEC 收入事实，TSEM 是只有 20-F 的外国发行人，二者不能靠猜标签补齐；审计
  profile 因此新增“只有净利润、无收入事实”的独立分类。CCOI 则是普通股利润
  fallback 加入前的旧解析产物，强制重建后净利润由 24 条增至 131 条，消除
  19 条候选缺口和 1 个代码。CCOI 在 2023-12-29 信号直接进入 Top 3 并替换
  `NBIX`，使 2024 年收益由 297.31% 更新为 286.56%；覆盖率由 82.88% 提升至
  83.01%，其余年度不变；
- 最新 Top 20 中，SLAB 是 fallback 加入前的旧解析结果，ITCI 则在 2025 年
  被收购后退出当前 ticker map。SEC 官方实体页确认 ITCI 历史 CIK 为 `1567514`；
  强制重建 SLAB 并按历史 CIK 补抓 ITCI 后，分别得到 280、188 条利润/收入记录，
  合计再消除 40 条候选缺口，净减少 1 个缺口代码，覆盖率由 83.01% 提升至
  83.29%。两者虽然在部分时点通过增长阈值，但正式 Top 3 和年度收益均未改变；
- 将检查范围扩展到优先级 21–50 后，一次性强制重建 EWBC、SFNC、TBBK、INDB、
  COLB、FIBK、SBCF、CVBF、OSIS、ACLS、KRYS、MDGL 共 12 个美国发行人；其中
  10 个恢复跨证据期的完整 TTM 链，KRYS、MDGL 只在后期形成收入链。该批次消除
  162 条候选缺口和 10 个代码，覆盖率由 83.29% 升至 84.40%，最差信号由
  71.67% 升至 73.33%。2025-12-31 信号由 `MU` 变为 `VICR`，使 2025 年收益
  由 12.63% 更新为 13.28%；其余年度不变；
- 原始缓存与断点续跑链路完成真实试点后，按潜在候选财务缺口优先级一次批量刷新
  CCEP、ARCC、DOX、OZK、CHKP、ASML、ARGX、SNY、SBLK、AZN、ICLR、NVMI、
  TCOM、GBDC、PCVX、TSEM、DSGX、GLNG、LOGI、WIX。20 个请求中 19 个成功形成
  原始缓存；OZK 的 SEC 当前 CIK 返回 404 并进入失败冷却。LOGI 恢复全部 17 条
  候选财务缺口，并进入 2021-01-29 信号、延续至 2021-02-26 持仓，使 2021 年
  收益由 46.55% 更新为 55.71%；其余年度不变。候选财务覆盖率由 84.40% 升至
  84.51%，缺口代码由 382 降至 381。该变化再次说明缺失候选会实质改变历史成绩；
- 随后专门选择 `SEC_QUARTERLY_PARTIAL` 的高优先级缺口，批量刷新 KRYS、MDGL、
  CLDX、UNIT、VNOM、LLYVK、UTHR、ACT、CSWC、APA；10 个 CIK 全部成功并进入原始
  缓存。ACT、CLDX、UTHR 恢复完整季度链，共消除 33 条候选缺口和 3 个代码，使
  覆盖率由 84.51% 升至 84.74%，缺口代码由 381 降至 378；其余 7 个代码虽然原始
  输入已可复现，但历史季度链仍不完整。该批次没有改变任何 Top 3 或年度收益；
- 第二批按同一 profile 刷新 BANR、ZION、ZNTL、VKTX、MRVL、WAFD、AXSM、ORKA、
  RCKT、KURA，10 个 CIK 全部成功。该批次再消除 16 条候选缺口和 2 个代码，使
  覆盖率升至 84.85%、缺口代码降至 376；固定 Top 3 和各年度收益均未变化；
- 启用可复现优先级批处理后，首批请求 FSV、MNDY、QFIN、XP、ATAT、STNE、AGNC、
  ASND、CAMT、GMAB、HTHT、LEGN、MMYT、BNTX、DLO、GGAL、MLTX、TIGO、WSBC、
  CIGI；20 个 CIK 全部进入原始缓存，缓存代码从 45 增至 65。XP、STNE、ASND、
  BNTX、GGAL 没有当前解析器可用的季度结果；WSBC 恢复完整链，消除 11 条候选缺口
  和 1 个代码，使覆盖率由 84.85% 升至 84.93%、缺口代码由 376 降至 375。该批
  代码没有直接进入交易账本，但新增合格横截面改变了百分位排名，使 2026 回放收益
  由 26.05% 更新为 36.07%；其余年度不变。原始输入仍已固定，后续扩展外国发行人
  数据源或解析规则时无需再次逐只下载；
- 改用缓存专用可行动性排名后，下一批 20 个唯一 CIK 全部成功，无失败。LKFN、
  QURE、SYBT、TFSL、TLN 五个代码恢复完整链，共消除 20 条候选缺口和 5 个代码，
  使覆盖率由 84.93% 升至 85.06%、缺口代码由 375 降至 370；Top 3 和年度收益
  均未变化。manifest CIK 数只从 65 增至 84，说明一个请求主体此前已由另一 ticker
  别名缓存，也由此推动后续按 CIK 识别本地缓存、避免别名重复联网的修复；
- 第三批首次完整验证 CIK 级限流与本地别名复用：网络处理 20 个唯一 CIK、21 个
  ticker，BATRA/BATRK 共享请求；WAFDP 从已有 CIK 纯本地解析。全部成功，缓存绑定
  代码由 85 增至 107。净缺口代码由 370 降至 369，但缺口观测仍为 2180，Top 3
  和年度收益不变，说明只剩 1–2 个缺口的 partial 尾部边际收益已明显下降；缓存
  调度因此进一步把低缺口 partial 和低缺口 NO_PARSED 分层后置；
- 第四批混合 6 个高缺口 NO_PARSED 与 14 个低缺口 partial，20 个 CIK 全部成功
  缓存，但 6 个 NO_PARSED 均只有 20-F/40-F/6-K 等外国定期报告，当前解析为空；
  仅 CAKE 完全补齐，净减少 1 条缺口和 1 个代码。由此停止把模糊 NO_PARSED
  直接视为高可行动性，并新增原始 payload 画像。当前 124 个缓存 CIK 中，85 个
  为 `US_GAAP_WITH_10Q`，39 个为 `FOREIGN_PERIODIC_NO_10Q`；
- 优先级表新增 `raw_sec_cache_profile` 和 `recommended_data_action`。剩余缺口目前
  拆为：263 个未缓存代码（1220 条）可继续抓 SEC；38 个已确认需外国季度来源
  （621 条）；64 个 US-GAAP 代码需重解析或接受历史长度限制（271 条）；2 个
  临床阶段公司需确认“无经营收入”政策（31 条）。原始画像同时记录当前支持的直接
  收入概念和银行收入组件，因此不会再把 AGNC/BUSE 的历史口径限制与 PCVX/MLTX
  的真实无营收混为“待补标签”；
- 概念映射检查确认 ARCC、GBDC 两个 BDC 使用标准
  `GrossInvestmentIncomeOperating` 作为营业总投资收入。加入该标签并从缓存离线
  重解析后，GBDC 完全补齐，ARCC 缺口由 36 降至 20，合计净消除 36 条缺口和
  1 个代码，使覆盖率升至 85.32%；Top 3 和年度收益不变。PCVX、MLTX 属于真实
  无营收阶段，未人为填造收入；AGNC 的 2020 年后收入口径不连续，暂不冒险拼接；

可按上述优先级只刷新指定代码，避免为了验证少数缺口重新请求全部股票：

```bash
PYTHONPATH=. .venv/bin/python -m src.io.fundamentals_update \
  --as-of YYYY-MM-DD --force --tickers MNST BPOP UMBF
```

刷新任务按唯一 SEC CIK 合并请求：多个股类或历史代码若属于同一发行主体，只下载
一次 Company Facts，再分别解析各代码。成功抓取的代码会整段替换旧解析历史，
避免规则修正后旧错误行残留；抓取失败的代码不会删除已有数据。

首次补齐完整原始缓存时，可以分批断点续跑，只请求尚未缓存的代码：

```bash
PYTHONPATH=. .venv/bin/python -m src.io.fundamentals_update \
  --cache-missing-only --limit 20 --workers 4
```

若目标是优先降低会影响历史选股的财务缺口，可直接复用验证报告生成的优先级表：

```bash
PYTHONPATH=. .venv/bin/python -m src.io.fundamentals_update \
  --cache-missing-only \
  --cache-priority-file \
    output/can_slim_technical_candidate_financial_priorities.csv \
  --limit 20 --workers 4
```

优先级文件必须包含 `ticker`；优先使用 `cache_refresh_priority_rank`，否则回退到
`priority_rank` 或文件行序。缓存专用排名把已有部分季度链、最可能通过新版
Company Facts 补齐的代码前置，把明确需要新季度来源的外国发行人后置；原始缺口
严重度排名仍单独保留，不改变研究披露。文件未列出的当前股票池代码仍按原顺序接在
后面，不会被永久排除。已缓存代码和仍在失败冷却期的代码会被跳过，随后自动由下一
优先代码补位。Coverage JSON 会
记录优先级文件路径、SHA-256、排序规则、实际命中的优先代码数和本批请求代码，
因此每批下载选择可以复核。
`--limit` 限制唯一 SEC CIK 请求数，不是 ticker 行数；同一发行主体的多个股类或
历史代码会一起进入本批，不会拆开，也不会重复消耗请求名额。缓存补齐模式选中一个
CIK 后，还会顺带解析股票池中所有尚未缓存的同 CIK 别名，包括原先处于 ticker
级失败冷却的别名；这不增加网络请求，并避免以后为同一主体再次下载。因此
`requested_tickers` 可能略多于 `--limit`，但 `requested_ciks` 不会超过它。
审计会把“本批实际请求”“符合条件但因 `--limit` 延后”和“处于失败冷却”分开
计数，并只保留前 20 个样例；不会再把所有尚未轮到的代码误报为冷却。`--limit`
必须是正整数，0 或负数会在任何文件或网络操作前拒绝。

每个成功批次都会更新 manifest、正式解析结果和覆盖审计；再次运行会跳过已有缓存，
继续处理下一批。`--tickers ...` 可把缺口检查限制到指定代码。该模式必须联网，
不能与 `--offline-cache` 或 `--reparse-cache` 同时使用。若本次没有任何需要刷新或
成功解析的代码，任务不会重新序列化年度和季度正式 CSV，只更新必要的状态与审计。
未形成原始缓存的失败代码会记录独立的 `cache_last_attempt`、状态和原因，并按
`--refresh-after-days` 冷却，避免在每个后续批次重复占位；需要立即重试时可显式
增加 `--force`。已经成功保存原始响应、但解析结果为空的代码仍视为“原始缓存完成”，
不会因为当前解析器无法提取季度链而重复下载同一响应。
在 2026-07-30 的实际网络条件下，20-code 优先级批次约耗时 10 分钟；SEC 延迟和
重试会显著影响时长，因此推荐以 20 为常规批次，确认网络稳定后再提高 `--limit`。
全股票池模式遇到 SEC 当前 ticker map 不认识的代码时，会跳过这些代码并在审计中
单独列出，不会阻塞所有可抓取代码；但用户通过 `--tickers` 显式请求未知代码时仍会
严格失败，必须提供经核实的 `--ticker-cik`。

需要按“修复一个 CIK 实际减少多少历史候选缺口”控制边际收益时，使用有界修复批次：

```bash
PYTHONPATH=. .venv/bin/python -m src.research.fundamentals_repair_batch \
  --as-of YYYY-MM-DD --limit 20 --workers 4
```

每批的原子 JSON 是权威审计记录，`index.csv` 只是可重建索引。即使进程在 JSON
落盘后、CSV 更新前中断，下一次仍会读取最新 JSON 的继续、缩小批次、暂停或复核
决定，不会因索引落后绕过停止信号；下次成功更新索引时会自动补回遗漏的 JSON 批次。
批后 payload 画像只解压本批 ticker 对应的不同 CIK，不再扫描全部原始缓存。只有在
人工复核来源或解析器确实发生变化后，才应使用 `--override-stop`。

在线刷新使用的 SEC ticker→CIK 映射也会按规范化内容计算 SHA-256，并以
`ticker_maps/ticker_map_<sha256>.json.gz` 内容寻址保存。映射未变化时复用同一
文件；SEC 后续增删映射时保留新快照。Coverage JSON 记录本次使用的映射路径和哈希，
缓存 manifest 同时校验所有映射快照，避免 live ticker map 的变化无法复现。

可随时只读查看进度，并同时验证 manifest、文件清单和 SHA-256：

```bash
PYTHONPATH=. .venv/bin/python -m src.io.fundamentals_update \
  --cache-audit-only
```

输出包含股票池覆盖率、已缓存 CIK 数、压缩/旧格式文件数、总字节数、缺口数量和
前 20 个缺口样例。增加 `--tickers ...` 时只审计指定代码。该命令不联网、不写文件。

历史代码若已不在 SEC 当前 ticker map，必须同时提供经官方实体页确认的 CIK：

```bash
PYTHONPATH=. .venv/bin/python -m src.io.fundamentals_update \
  --as-of YYYY-MM-DD --force --tickers COOP --ticker-cik COOP=933136
```

每次成功访问 SEC Company Facts 时，原始 JSON 会按 CIK 原子写入
`cleaned_stocks_data/financial/sec_companyfacts_cache/`，并生成包含来源 URL、
抓取时间、字节数和 SHA-256 的 `manifest.json`。离线重解析和在线刷新都会在
任何缓存/网络写入前逐文件核对 manifest 的文件清单、字节数和 SHA-256；缓存被
修改、缺失或出现未登记文件时会失败，不能先为变化后的输入静默重新生成哈希。
目录中已有 payload 却没有 manifest 时同样拒绝自动接管。若新 ticker 映射到已缓存
CIK，则直接读取本地 payload、解析该别名并更新绑定，不再访问 SEC。解析器规则变化
后可以完全离线。manifest 同时作为 ticker→CIK 的本地索引；增量重解析只解压目标
CIK，不会为更新少数股票而展开全部缓存，但正式写入前仍会核对完整文件清单、字节数
和 SHA-256。增量重解析只 upsert 指定代码的新事实，其他代码和本批未重新出现的
旧事实保持不变。

增量 `reparse_state` 的 ticker 指纹同样绑定主 parser、输出 schema 与 Python/Pandas
运行时；对登记的外部季度 ticker 还绑定外部季度 parser 和该 ticker 的注册表项。因而
外部季度 parser 变化会只使受影响 ticker 重新解析，不会把无关的国内 payload 误判为
已更新或强制全量重跑。

新增缓存默认使用确定性 gzip 格式 `CIK##########.json.gz`；同一份内容会产生稳定
字节，方便 SHA-256 复核。旧的未压缩 `.json` 仍可直接读取且不会被自动删除。
当前真实样本从约 5.27 MB 降至约 381 KB（约为原大小的 7.23%），显著降低完整
股票池缓存的本地磁盘和哈希读取成本。

只补原始缓存、完全不读取或写入正式年度/季度 CSV 时，必须显式增加
`--raw-cache-only`。`--limit` 按唯一 CIK 计数，同一 CIK 的多个 ticker 会一起
绑定；独立的 raw-cache state 会跳过已成功项、冷却临时失败，并默认不再请求已确认
404 的 Company Facts。在线 CLI 默认将外层批次限制为 25 个 CIK（可显式用 `--limit` 调整）；任务每完成 5 个 CIK
会原子更新 state，并立即重签绑定全部 payload 与 state 的 manifest。刷新开始时还会
写入事务 journal；收到中断时会取消尚未开始的请求、等待正在执行的请求停止，再做一次
最终 checkpoint。即使进程在 payload 已落盘但 checkpoint 间隔尚未到达时硬中断，下一次
加锁验证也会在确认既有 payload 未被外部改动后自动补写 manifest，因此已成功的部分
批次可直接续跑，无需手工重签 manifest：
checkpoint 会复用上一版已验证的未变更 CIK 条目，只重新计算本批发生变化的 payload，
避免每 5 个 CIK 都重新读取和哈希整个历史缓存；完整审计和 snapshot 验证仍会逐文件核对。

```bash
PYTHONPATH=. .venv/bin/python -m src.io.fundamentals_update \
  --cache-missing-only --raw-cache-only \
  --cache-priority-file output/can_slim_technical_candidate_financial_priorities.csv \
  --limit 25 --workers 4
```

不带 `--raw-cache-only` 的 `--cache-missing-only` 保留原有语义：抓取成功后还会把
解析结果非破坏性合并进正式财务 CSV。需要严格隔离数据获取和正式发布时不要省略
该参数。

```bash
PYTHONPATH=. .venv/bin/python -m src.io.fundamentals_update \
  --reparse-cache incremental --tickers EWBC
```

只有在已完成差异审计并取得明确的数据发布授权后，才可以从缓存完全重建正式年度与季度
CSV；全量模式必须同时指定不可变 snapshot 和 recipe-bound 的 v2 scope：

```bash
PYTHONPATH=. .venv/bin/python -m src.io.fundamentals_update \
  --reparse-cache full \
  --cache-snapshot <immutable-snapshot-dir> \
  --full-rebuild-scope <recipe-bound-scope-v2.json>
```

在冻结正式财务版本期间，先运行同一门槛的无写入预演：

```bash
PYTHONPATH=. .venv/bin/python -m src.io.fundamentals_update \
  --reparse-cache full --dry-run \
  --cache-snapshot <immutable-snapshot-dir> \
  --full-rebuild-scope <recipe-bound-scope-v2.json>
```

该命令完整验证 manifest、历史 ticker 覆盖和所有 raw payload，并在一次性临时目录
中重建年度/季度 CSV。输出会列出正式与重建文件的 SHA-256、行数、ticker 集合和每个
ticker 的事实行数差异；临时文件随后删除。它不会写正式 CSV、coverage、raw cache 或
`reparse_state.json`，因此可作为发布新数据版本前的必经比较步骤。
报告中的 `formal_content_match` 与 `formal_rebuild_gate` 是机器可消费的放行信号：
只有 annual/quarterly 两侧事实内容（忽略 `fetched_at`）都一致时才是 `PASS`；任一侧
有行数或事实差异都会明确返回 `BLOCKED_FORMAL_CONTENT_MISMATCH`，不能仅因 raw
coverage gate 通过就替换冻结正式文件。
同一 immutable snapshot、scope 和运行时配方的重复离线重解析还必须产生相同的 annual/
quarterly 字节 SHA；该确定性由测试持续约束。

v2 scope 除 raw snapshot、正式 ticker 集和正式 CSV SHA 外，还绑定 Company Facts
parser、外部季度 parser、外部季度注册表、输出 schema、Python/Pandas 运行时的内容
配方。执行前必须与当前环境逐项匹配；否则 full/dry-run 会在解析或写入前拒绝。旧 v1
scope 只保留为历史诊断，不能原地覆盖升级，也不能用于新的命令行 full rebuild；应以新
路径创建 v2 scope，保留旧证据不被改写。
本轮 checkpoint 与 comparison 代码更新后，曾生成的
`output/data_provenance/companyfacts_rebuild_scopes/manifest-ff932997f3143f50-copy-current.json`
因 parser 已继续变化而被 recipe SHA 门槛正确拒绝；随后重新生成并绑定当前代码的
scope 为
`output/data_provenance/companyfacts_rebuild_scopes/manifest-ff932997f3143f50-copy-batch25.json`，
其 live recipe SHA 为 `9c5b6b51528631901445ec7f9262eb378c8beea1755affe170efc6f6195cec3e`。
对应的最新 full dry-run 证据为
`output/data_provenance/companyfacts_rebuild_dry_runs/manifest-ff932997f3143f50-copy-scope-446b137bc783f3d7.json`；
它已通过 raw coverage 和 recipe gates，但
`formal_rebuild_gate=BLOCKED_FORMAL_CONTENT_MISMATCH`，因此仍不可替换冻结正式文件。
Python API 若传入 immutable manifest SHA，也必须同时传入 recipe SHA（反之亦然），
避免下游脚本只绑定 raw payload 而漏掉解析器版本。

两个模式都先完成年度和季度临时文件，再成对替换正式输出；第二个文件替换失败时
会把第一个文件回滚到原版本，不能留下年度与季度版本不一致的状态。`incremental`
只对指定代码做非破坏性 upsert，保留本批 payload 没有重新解析出来的正式事实；
若年度和季度两侧都没有新事实，则不会读取或重写约 61 MB 的正式 CSV。它不要求
缓存覆盖整个股票池。`full` 从空表开始生成，且必须先覆盖当前全部可投资代码以及
两个正式输出中已经存在的代码，部分缓存绝不能覆盖正式全量文件。该完整性检查
位于实际写入函数内部，不只存在于命令行参数解析层；未来脚本直接调用 Python API
也不能绕过。只有 manifest 绑定的官方 ticker-map 缺失或 Company Facts 404
负向证据可满足该覆盖门槛；timeout 和普通抓取失败仍会阻断全量重建。全量预检只
读取 manifest 和压缩文件字节以核对覆盖率与 SHA-256，不会
为研究画像额外解压全部 payload；显式 `--cache-audit-only` 仍会生成详细 payload
画像。刷新与离线重建共享跨进程文件锁，本地同时启动两个任务时会串行执行，
避免缓存、年度 CSV 和季度 CSV 相互覆盖。

当前 raw cache manifest 可验证，但它不是正式财务版本的等价物：活动 cache state
含 1,425 个 `raw_cached` ticker、11 个官方 Company Facts 404 和 5 个不在精确
SEC ticker-map 的代码；这些是缓存状态计数，不代表当前股票池的历史 PIT 财务已闭环。
当前 required universe 已缓存 1,602/1,618；另有 16 个官方不可用代码，
`cache_resolution_coverage=1.0`、ordinary unresolved=0。raw-only 批次全部明确
`formal_outputs_read=false`、`formal_outputs_written=false`，不会改变正式年度/季度 CSV。

对冻结正式 CSV 的来源审计显示：年度 249,734 行中 247,801 行、季度 296,957 行中
218,488 行可直接绑定当前 immutable SEC raw snapshot，直接 raw match coverage 均为
1.0；但其余 80,402 行是历史派生选择。公式审计目前只有 73,577 行匹配、6,825
行失败（其中 Q4 operand unresolved 1,987、value mismatch 4,838），所以 raw coverage
100% 不等于当前 parser 可以精确重建 formal annual/quarterly。报告位于
`output/data_provenance/companyfacts_formal_source_audit_manifest-ff932997f3143f50-copy.json`
和 `output/data_provenance/companyfacts_formal_formula_audit_manifest-ff932997f3143f50-copy.json`；
release-selection lockfile 在逐行 proof 完整前必须 fail-closed，不能把当前 raw-only
cache 当成正式发布或 IBKR 准入证据。

当前研究 lockfile `output/data_provenance/companyfacts_release_selection/manifest-ff932997f3143f50-copy.jsonl.gz`
已绑定 copy snapshot `manifest-ff932997f3143f50-copy`：raw 466,289 行、逐行
`derived_proven` 73,577 行、`derived_unproven` 6,825 行。它只证明来源选择和
已审计 operand，不改变正式 CSV；默认 replay 会因 6,825 条未证明派生行而阻断。

- 股票价格中没有超出审计日期的未来行；
- EPS 数据包含 PIT 所需字段。

#### 历史价格缺口的 SEC 线索（研究用）

当 PIT 价格缺口可能与并购、退市或更名有关时，先生成只读 SEC submissions
线索，而不是直接写入价格、`terminal_returns.csv` 或身份映射：

```bash
PYTHONPATH=. .venv/bin/python scripts/sec_submission_triage.py --refresh
PYTHONPATH=. .venv/bin/python scripts/sec_submission_triage.py
```

首条命令原子缓存每个 SEC submissions payload，报告绑定逻辑 payload SHA-256；第二条
只从该缓存离线重建报告。候选标签如
`PRICE_SOURCE_AND_TERMINAL_RETURN_REVIEW` 和 `IDENTITY_TRANSITION_REVIEW`
仅指明下一步人工核查所需证据，绝不等价于已确认的终止收益或更名。任何正式价格、
终止收益或身份数据的修改都必须单独决定并重新验证。

#### 候选价格来源的离线可复核评估（研究用）

当 SEC 线索显示某个价格缺口值得继续调查时，可以先缓存公开历史页的原始字节，
并量化它是否真正覆盖缺口、以及与本地 Nasdaq 行在重叠日是否一致：

```bash
PYTHONPATH=. .venv/bin/python scripts/stockanalysis_price_triage.py --refresh
PYTHONPATH=. .venv/bin/python scripts/stockanalysis_price_triage.py
```

第一条命令只把原始 HTML 封装为带 SHA-256 的 research cache；第二条完全离线复放。
报告会把“完整覆盖且重叠一致”仍标为
`REVIEW_REQUIRES_LICENSE_AND_FORMAL_DATA_AUTHORIZATION`：它只说明下一步可申请来源/
许可和正式数据恢复审查，绝不会自动写入任何价格 CSV、终止收益、身份映射、coverage
或 validation artifact。

### 7.3 重跑固定策略

```bash
PYTHONPATH=. .venv/bin/python -m src.research.can_slim_validation
```

运行后应重新生成 [历史结果](history/legacy_results.md) 列出的正式结果。重点检查：

- 年度结果是否与 [历史结果](history/legacy_results.md) 接近；
- `passed_every_historical_year` 是否符合预期；
- 交易成本压力测试是否通过；
- 逐笔账本净值能否与每日回测净值对账。

完整验证只在单次进程内复用只读中间结果：4,142 个价格文件的日期元数据只加载
一次，历史审计与候选覆盖共享同一批 PIT 季度增长快照，0/10/30/50 bps 成本回放
共享与成本无关的 selector 结果和预调整价格。缓存不会跨运行持久化，也不会跳过
输入指纹或门禁。2026-07-31 在当前数据集上的轻量计时由约 231.6 秒降至 136.5 秒；
优化前后回测、账本、年度、成本压力、流动性和候选覆盖产物逐字节一致，summary
除必然变化的全源码 SHA-256 外结构化一致。

八个正式验证产物会先全部写入同目录临时文件，再依次替换；正常异常或某个
`os.replace` 失败时会回滚已经替换的文件，避免留下普通错误造成的混合版本。
包含八个文件大小和 SHA-256 的 `can_slim_validation_artifacts_manifest.json` 最后提交。
会验证当前八个文件是否属于同一次写入，但“manifest 可验证”不等于“仍是最初冻结
版本”。当前工作区这八个文件报告 5/6，策略依赖 SHA 为 `728cc6e340...`，与
2026-07-31 冻结证据 `736b28e72f...` 不同；因此它们只能视为后续 research snapshot，
不能再标成原冻结 validation。正式年度/季度财务 SHA 仍保持不变，release 继续
`BLOCKED`，在明确新模型版本前不要重签或覆盖冻结结论。
POSIX 不提供跨多个文件的断电级原子替换，因此机器断电、内核崩溃或
`SIGKILL` 后不承诺自动回滚；但独立 production gate 和每日推荐流水线都会先验证
manifest，任何缺失、部分更新或混合版本都会拒绝继续。重新运行完整验证成功后，
才能再次执行 production gate。

季度财务发生增量更新后，可把上一个 Release 解压到临时目录，并固定其他全部
输入，只比较两个季度财务版本：

```bash
PYTHONPATH=. .venv/bin/python \
  -m src.research.quarterly_data_version_impact \
  --reference-quarterly /path/to/release/cleaned_stocks_data/financial/quarterly_fundamentals_point_in_time.csv
```

命令不会修改正式财务文件或冻结模型，会生成年度收益差、目标组合变化月份以及
包含双方 SHA-256 和事实键增删数量的 JSON 摘要。任何使历史胜负、目标组合或
候选覆盖发生变化的增量批次，都应先解释其数据来源，再更新 README 和验证产物。

## SEC 研究证据包

### 研究性 SEC 完成申报证据包

对于 AVDX、PPBI 的历史价格缺口，`scripts/sec_completion_evidence.py` 只缓存 SEC 8-K 原始 HTML 字节、SHA-256 和可离线复放的审阅包：

    PYTHONPATH=. .venv/bin/python scripts/sec_completion_evidence.py --refresh
    PYTHONPATH=. .venv/bin/python scripts/sec_completion_evidence.py

该工具不会改写正式价格、终止收益、证券身份、年度/季度财务 CSV、coverage 或 validation artifacts。AVDX 的现金条款和 PPBI 的换股条款仍须结合有许可的 PIT 价格、最后可交易日与正式影响审计后，才可另行申请正式数据变更授权。

### 不可变 SEC Company Facts 输入快照

活跃 Company Facts cache 可以刷新，因此在任何候选 full rebuild 前应先固定其原始输入快照：

    PYTHONPATH=. .venv/bin/python scripts/companyfacts_cache_snapshot.py
    PYTHONPATH=. .venv/bin/python scripts/companyfacts_cache_snapshot.py --verify-snapshot <snapshot-dir>
    PYTHONPATH=. .venv/bin/python scripts/companyfacts_cache_snapshot.py --create-full-rebuild-scope <snapshot-dir> --rebuild-scope <new-recipe-v2-scope.json>
    PYTHONPATH=. .venv/bin/python scripts/companyfacts_cache_snapshot.py --record-full-dry-run <snapshot-dir> --rebuild-scope <recipe-v2-scope.json>

快照在 cache lock 内把 manifest 引用的原始文件复制到独立目录（新快照的
`storage_method=copy`）；后续活跃 payload 原子替换时，旧快照字节仍可离线复放。
旧版 hard-link 快照只作为兼容格式逐文件校验，不能把已被活跃 cache 原地更新污染的
目录当成 immutable 输入。v2 scope 同时固定该 snapshot 对应的 formal annual/quarterly
ticker 并集、两份 formal 文件 SHA、ticker-set SHA，以及 parser/runtime recipe SHA；它
不从当天可变的 current-universe 文件推导范围。full rebuild 只解析 scope 内有
manifest-bound raw payload 的 ticker；有官方负向证据的不可寻址 ticker 会留在 scope
审计中，但不会被伪造成 raw payload。`--record-full-dry-run` 还会记录 recipe 是否与当前
parser 匹配；它只记录 snapshot、scope、候选 annual/quarterly SHA 和与正式文件的比较，
不会写入正式数据；任何 mismatch 都阻断正式替换，仍需显式授权。

未显式指定 `--rebuild-report` 时，dry-run 报告文件名由 snapshot 与 scope 的语义内容共同寻址；scope 身份包含正式输出 SHA、ticker-set SHA 和 parser recipe SHA。相同输入会复用同一路径，不同 recipe 或正式基线会并存，避免后一次诊断覆盖旧版本证据。

正式 full reparse 也拒绝隐式范围：必须同时给出已验证的不可变 raw snapshot 与该 snapshot 绑定的 scope，例如：

    PYTHONPATH=. .venv/bin/python -m src.io.fundamentals_update \
      --reparse-cache full --dry-run \
      --cache-snapshot <snapshot-dir> --full-rebuild-scope <scope.json>

这条命令仅输出对比；它不改变正式 annual/quarterly CSV 或 reparse state。没有这两个显式输入时，full 模式会在任何正式文件写入前拒绝运行。

### Company Facts formal 来源与 release-selection 证明（研究用）

正式 CSV 的 direct rows 可以用 immutable raw snapshot 做逐行来源审计；派生 rows
还必须通过同一 parser recipe 的 operand/formula 审计。生成证明报告和逐行锁文件：

```bash
PYTHONPATH=. .venv/bin/python scripts/companyfacts_formal_source_audit.py \
  --snapshot <immutable-snapshot-dir> \
  --annual-output output/annual_fundamentals.csv \
  --quarterly-output output/quarterly_fundamentals.csv \
  --output output/data_provenance/companyfacts_formal_source_audit.json
PYTHONPATH=. .venv/bin/python scripts/companyfacts_formal_formula_audit.py \
  --snapshot <immutable-snapshot-dir> \
  --annual-output output/annual_fundamentals.csv \
  --quarterly-output output/quarterly_fundamentals.csv \
  --output output/data_provenance/companyfacts_formal_formula_audit.json
PYTHONPATH=. .venv/bin/python scripts/companyfacts_release_selection_manifest.py \
  --snapshot <immutable-snapshot-dir> --create \
  --annual-output output/annual_fundamentals.csv \
  --quarterly-output output/quarterly_fundamentals.csv \
  --formula-audit output/data_provenance/companyfacts_formal_formula_audit.json \
  --manifest output/data_provenance/companyfacts_release_selection/selection.jsonl.gz
```

lockfile 只接受与 snapshot、formal 文件 SHA 和 parser recipe 完全一致的 formula
audit。direct raw rows 必须在该 snapshot 中找到同一 CIK；派生 rows 只有逐行
`dataset + ordinal + row_sha256` proof 标记为 matched 时才会以 `derived_proven`
进入 replay，其余 rows 默认 fail-closed。`--exclude-unproven-derived` 可生成只含
raw 与 proven-derived 行的研究数据集；`--allow-unproven-derived` 仅用于显式的
研究诊断，不能作为正式 annual/quarterly 替换或 release/IBKR 准入授权。
