# v50r3 运维手册

> 从原 README 第 3 节移来。概览见 [README 第 3 节](../README.md#3-运行与运维)。协议已于 2026-09-26 由 `v50r3 freeze` 工作流冻结，下文“首个信号前的操作”中的演练、建副本、冻结步骤已经完成，保留作换机器或另起 r4 时参考。

**在 GitHub 上运行（推荐，2026-09-25 起）：** 整个观察可以完全在 GitHub Actions 上
进行，不依赖任何个人电脑。下面的工作流都在 master 上（Actions 页面可手动运行）：

1. **v50r3 rehearsal**：在 GitHub 的 runner 上用真实数据完整演练一次 SIGNAL
   （逐个下载价格，约 2.5–3 小时），报告作为 artifact 上传。`verdict` 为 `FAIL`
   时不要冻结。
2. **v50r3 freeze**：输入 `FREEZE` 运行一次。它从当前 master 建立 `live/v50r3`，
   在上面冻结 r3 协议和账本、写入 r2 的 supersession 记录并推送，同时把
   supersession 记录提交到 master。`live/v50r3` 已存在时拒绝运行。在 2026-09-30
   20:30 UTC（北京时间 10-01 04:30）前运行，第一期就是 9-30；更晚则顺延到下一个
   月末。
3. **v50r3 scheduler**：每 30 分钟自动运行。冻结前它会跳过；冻结后它检出
   `live/v50r3`，到期时恢复数据包、执行 SIGNAL 或 MARK（`--workers 1`），把账本、
   新信号、事件记录和 `latest_valued_bundle/` 推回 `live/v50r3`。收盘价尚未发布时
   以警告结束，下一次自动重试；需要人工补录的疑似拆股、仍欠着的错过月份或其他错误
   会让任务失败。每次运行的结果，包括失败，都会发到下文的 issue #2。GitHub 自己只把
   失败通知发给触发运行的人：定时运行算你的，唤醒器和重试启动的运行算机器人的，
   失败了没人收到邮件。
4. **v50r3 record sourced event**：补录一条事件（规则见下文以“估值规则”开头的
   一段）。在 master 上运行，填写股票、类型（`SPLIT` / `MARKET_MOVE` /
   `TERMINAL_RETURN`）、日期、公开来源链接；拆股另填价格因子（二拆一填 `0.5`，
   十合一填 `10`），退市另填终值收益（归零填 `-1`）。它像 staging 一样下载这只
   股票的价格，按下文的规则核对，通过后只提交并推送
   `sourced_event_supplement.csv`；核对不通过时任务失败，日志里写明原因。下一次
   SIGNAL 或 MARK 自动使用它；推送后它立即启动一次调度器，被这条事件卡住的运行不用
   等晚几个小时的定时任务。SIGNAL 因疑似拆股被拒时，任务日志的
   `unexplained_split_like_moves` 列出股票、日期和价格比。
5. **v50r3 window waker**：GitHub 的定时任务会晚几个小时才启动（本仓库 00:30 UTC
   的定时任务每天约 03:45 才开始，12:15 UTC 的一次 17:11 才开始），所以不能指望每
   30 分钟的调度器在数据一发布就跑 SIGNAL。它在工作日晚上启动：窗口里有到期的
   SIGNAL（月末，或错过月份的补跑）时，每 10 分钟查一次 Nasdaq 是否已发布这个交易
   日，发布后立即用 workflow_dispatch 启动调度器（这种启动 GitHub 会马上执行）。
   重试和每日 MARK 仍由调度器自己的定时任务负责。
6. **v50r3 test email**：按下面的方式发一条测试通知，确认通知设置好了。

**结果通知**：调度器每次冻结信号、每次估值后，在 issue #2「v50r3 观察记录（自动）」下
发一条评论（信号写目标持仓；估值写净值和纳指、QQQ 的对比），GitHub 会把评论按你的通知
设置用邮件或手机 App 推送给你。运行没有成功时也发一条，写明原因和要不要你操作：疑似
拆股要补录事件、持仓或候选股缺收盘价（多半是停牌）、错过了月末信号、推送失败或其他
错误。同一件事一周内只发一次（评论末尾藏着一个键，调度器靠它去重，只认
`github-actions[bot]` 发的评论）。评论和账本一样是公开的。
另外，仓库 Settings → Secrets and variables → Actions → **Secrets** 标签下的
**Repository secrets** 里有 `MAIL_USERNAME`（邮箱地址）和 `MAIL_PASSWORD`（Gmail 的
应用专用密码，或 QQ 邮箱、163 的 SMTP 授权码）时，同样的内容还会直接发邮件；收件人默认
是同一个地址，要发到别处再加 `MAIL_TO`。非 Gmail 邮箱还要在同一页面的 Variables 里设
`MAIL_SERVER`（如 `smtp.qq.com`）和 `MAIL_PORT`（`465`）。Environment secrets、
Codespaces 或 Dependabot 下的 secrets 读不到。

Nasdaq 在收盘后四到五小时才发布当天的数据：2026-09-24 的股票和 QQQ 历史行在 UTC
00:16 还没有、01:00 已经有了；纳指的官方收盘价要等盘后交易在纽约时间 20:00 结束。
所以 SIGNAL 实际在北京时间早上 8–9 点前后开始（冬令时晚一小时），GitHub 上约 2.5
小时完成，窗口到北京时间 16:00（冬令时 17:00）关闭，够重试一次。调度器每次运行前先用
`scripts/v50r3_sources_ready.py` 检查，未发布就跳过并留一条提示，不算失败；SIGNAL 因
收盘价还没到齐而没通过时，它立即排队重试，不等定时任务（2026-09-25 调度器上线后的
五个小时里，每半小时一次的定时触发只来了两次）。如果只差几只点名的候选股（不超过
10 只），多半是停牌，立即重试只会两个半小时后同样失败：这时不排队重试，issue #2 上
说明是哪几只；之后的 SIGNAL 先到 Nasdaq 查这几只的收盘价（`scripts/v50r3_notify.py
--still-blocked`），出现了才再跑一次，重跑还缺就不再跑这个日期；缺的是动量起点（63
个交易日前）那天的收盘价时，以后也不会补上，直接不再跑。按冻结的规则，候选股缺收盘价
时信号不能冻结，所以窗口关闭后按错过的月份处理，在之后的交易日窗口自动补跑。查询本身
失败时照常运行。

冻结后在 Settings → Branches 保护 `live/v50r3`（禁止强制推送和删除；不要限制
推送者，否则 `github-actions[bot]` 无法提交账本）。补录事件和调度器可以同时运行：
谁的推送晚到，谁就把自己的提交接在对方之后再推送。也可以在任一电脑上检出
`live/v50r3`、恢复数据包，先运行 `scripts/v50r3_event_prices.py <股票>` 准备这只
股票的价格，再运行下文的 `record-sourced-event`，提交并推送
`sourced_event_supplement.csv`。GitHub 的定时任务可能延迟，偶尔跳过；窗口有
十几个小时，每 30 分钟都会重试，错过还有补跑兜底。公开仓库 60 天无活动时 GitHub
会停用定时任务（停用前会发邮件）。

下面是在自己电脑上运行的做法（备用）：

**首个信号前的操作（按顺序）：**

1. 演练。在任一美股交易日收盘后（夏令时 UTC 20:30 之后），用与正式运行相同的
   机器、网络和并发数：

   ```bash
   PYTHONPATH=. .venv/bin/python scripts/research_v50r3_rehearsal.py --as-of 2026-09-28
   ```

   它在 `output/research_only/v50/r3_rehearsals/` 下的临时目录里完整执行 r3 的
   SIGNAL staging 和选股，不写账本、信号或正式数据。报告列出每个阶段的耗时、
   就绪门、无 CIK 的股票、被剔除的未来财报行、缺价格文件的股票、排名池中未经
   复核的疑似拆股，以及一次 staging 能否在下一个窗口内再容纳一次重试。
   `verdict` 为 `FAIL` 时不要冻结 r3。从月末窗口开启前 3 小时起直到窗口关闭，脚本
   会拒绝运行，避免占用正式运行的锁。

2. 建独立副本。正式运行不在日常开发的仓库里跑，而在一个固定版本的独立副本里跑：
   一个 git worktree，停在 `live/v50r3` 分支上，只接收冻结提交和调度器提交的账本。
   r3 协议按哈希绑定了 51 个代码文件，在同一个仓库里跑的话，冻结后这些文件一个都
   不能改；有了独立副本，master 可以照常开发。先把 r3 代码合进 master 并拉到本地，
   然后在主仓库里运行：

   ```bash
   scripts/setup_v50r3_live.sh            # 默认建在 ../<仓库名>_live
   ```

   脚本会建立 `live/v50r3` 分支和工作副本，复制本仓库的数据目录（没有数据时改为
   下载数据 Release），把被协议绑定、却会被数据覆盖的已跟踪文件恢复成分支里的版本，
   建立独立的 `.venv`，最后用 `status` 自检。远端已有 `live/v50r3` 时（例如换机器），
   它改为建立跟踪远端分支的副本。r3 的冻结、打包、冻结信号、估值、追加事件记录这些
   写入命令，以及调度器的 `run`，在其他分支上都会直接拒绝；`status` 和 `check` 在
   哪里都能跑。

3. 演练和数据源探测都通过后，在独立副本里冻结 r3 并推送 `live/v50r3`：

   ```bash
   cd ../quant_stocks_live
   PYTHONPATH=. .venv/bin/python scripts/research_v50r3_corrected_v47.py freeze-protocol
   PYTHONPATH=. .venv/bin/python scripts/research_v50r3_corrected_v47.py write-v50r2-supersession
   git add output/research_only/v50/corrected_v47_20260924_r3 \
     output/research_only/v50/corrected_v47_20260905_r2/superseded_by_v50r3.json
   git commit -m "research: freeze v50r3 prospective protocol and supersede r2"
   git push -u origin live/v50r3   # watchdog 读这个分支
   ```

   再把 r2 的接替记录带回 master，这样主仓库里残留的 r2 定时任务也会直接以退出码 3
   结束，不会再尝试 r2 的信号：

   ```bash
   cd ../quant_stocks            # 主仓库，master
   git fetch origin live/v50r3
   git checkout origin/live/v50r3 -- \
     output/research_only/v50/corrected_v47_20260905_r2/superseded_by_v50r3.json
   git commit -m "research: record that v50r3 superseded v50r2"
   git push
   ```

4. 调度（在独立副本里）。删掉 r2 的定时任务。窗口有十几个小时，可以不配 cron，
   在窗口内（例如北京时间早上）到独立副本里手动运行一次，失败了过一会儿再运行：

   ```bash
   cd ../quant_stocks_live
   PYTHONPATH=. .venv/bin/python scripts/research_v50r3_scheduled_run.py run --push
   ```

   也可以让 cron 在任何时区每小时调用一次：

   ```cron
   35 * * * * cd /path/to/quant_stocks_live && PYTHONPATH=. .venv/bin/python scripts/research_v50r3_scheduled_run.py run --push >> logs/v50r3_scheduler.log 2>&1
   ```

   `--push` 在冻结信号或追加估值成功后，只提交账本、新的 signal 文件、事件记录和
   `latest_valued_bundle/`（不会带上工作区的其他改动），再推送 `live/v50r3`；需要
   本机 git 已配置身份和推送权限。推送失败时退出码为 1，但信号已经冻结，手工
   `git push` 即可。

   Nasdaq 会拒绝同一出口 IP 的突发并发请求：2026-09-25 在 GitHub 托管的 runner 上，
   16 路并发在第 14 个请求后收到 403，之后该 runner 的所有 Nasdaq 请求都被拒；同一
   环境逐个请求 400 个全部成功（约 2.35 秒一个）。在这类机器上运行时加
   `--workers 1`（选股下载约 2 小时，窗口足够）。

5. 在 GitHub 的 Settings → Branches 里保护 `live/v50r3`：禁止强制推送和删除。

之后的规矩：独立副本里只运行上面的命令，不手工改文件；不要把 master 合进
`live/v50r3`，也不要把 `live/v50r3` 合回 master（那会把冻结协议带回 master，之后
master 上每次改动闭包文件，CI 都会变红）。账本在 `live/v50r3` 上，看结果用
`git show origin/live/v50r3:output/research_only/v50/corrected_v47_20260924_r3/prospective_ledger.jsonl`。
冻结后若发现运行时缺陷，照 r2 → r3 的方式另起 r4 接替，而不是改动副本。

手工入口（在独立副本里）：

```bash
PYTHONPATH=. .venv/bin/python scripts/research_v50r3_corrected_v47.py status
# 只判断不执行
PYTHONPATH=. .venv/bin/python scripts/research_v50r3_scheduled_run.py check
# 执行到期的 SIGNAL（stage-bundle + freeze-signal）或 MARK（stage-bundle + append-mark）
PYTHONPATH=. .venv/bin/python scripts/research_v50r3_scheduled_run.py run
```

退出码：0 表示无事可做、已完成（包括刚完成补跑），或另一个 staging 正在进行；1 表示
出错（含 git 记录失败）；2 表示错过了 SIGNAL 窗口且这个月还没补上（已持有的组合照常
估值）；3 表示 r3 未冻结或账本缺失。

规则：SIGNAL 窗口 = 月末交易日官方收盘后 30 分钟起，至下一交易日盘前交易开盘
（纽约时间 04:00）止，不含该时刻。换算为北京时间：夏令时约为次日 04:30–16:00，
冬令时约为 05:30–17:00；月末是周五或逢假日时，窗口一直延到下一个交易日的盘前开盘。
staging 必须在窗口内开始，数据包的 `created_at` 和账本里 `SIGNAL_FROZEN` 的
`recorded_at` 都必须早于窗口关闭，所以实际最晚开始时间 = 窗口关闭时间减去一次
staging 的耗时（演练报告里有）。窗口开头仍是美股盘后交易时段，若综指当日历史行尚未
发布、兜底又要等盘后结束，staging 会失败，稍后重试即可。MARK = 最近一个已完成
（收盘后 30 分钟）、晚于最新冻结信号日且尚未估值的交易日。

补跑：错过月末窗口后，错过的月末日期永不回填（不会在事后按 9-30 的数据补出一个
"9-30 信号"，再假装 10-01 收盘就买了）。调度器改为在之后第一个开着的交易日窗口里
补跑一次：用该交易日的数据（它就是最近一个已收盘的交易日），规则与月末窗口相同
（收盘后 30 分钟起，至下一交易日盘前开盘止），下一交易日收盘执行。每个交易日都有
这样一个窗口，北京时间大约是次日 04:30–16:00（冬令时 05:30–17:00），周五的窗口一直
开到周一盘前。例如 9-30 的窗口错过了，10-02 白天开机就会补跑 10-01 这一期，只晚一个
交易日建仓。补跑之前，上个月的持仓照常持有（第一期则是空仓）。每个错过的月末只补
一次，最晚到下一个月末前一个交易日的窗口；再错过，这个月就没有信号，下一个月末照常。
补跑的信号在账本里记为 `signal_role: CATCH_UP`，并写明 `catch_up_for`（补的是哪个
月末）和错过的窗口；每次估值都会列出所有补跑信号，以及包含补跑调仓的完整月份
（`complete_months_with_catch_up_rebalance`），方便分别看含和不含这些月份的结果。
第一期若是补跑，建仓那个月不是完整月，不计入逐月胜负，但计入累计净值。
窗口期间（包括补跑）不要运行 `schedule_run.sh` 或日常流水线：它们会改写正式股票池
和指数文件，隔离检查会让 staging 失败。

此外还有两个检查用的 GitHub Actions：`.github/workflows/tests.yml` 在每个分支的每次
push/PR 上跑不依赖本地数据包的测试子集（排除清单见
`tests/data_dependent_test_files.txt`），并在干净 checkout 上复验 r1/r2 协议，以及
r3 协议和它的代码闭包（该分支上没有冻结的 r3 时跳过）；在本机运行调度器时，每次
推送账本都会在 `live/v50r3` 上触发一次。GitHub 上由 `github-actions[bot]` 推送的
提交按 GitHub 的规定不触发工作流，冻结的代码改由下面的 watchdog 每天复验。
`.github/workflows/signal_watchdog.yml` 平时每天一次、月末前后每小时读取
`live/v50r3` 跑一次 r3 `check`，只凭仓库内容就能判断窗口是否错过、错过的月份是否
已补跑、协议是否已冻结、代码是否漂移，失败即由 GitHub 通知仓库所有者，是独立于本机
的 dead-man switch：一个月份没补上之前，它每天都会报一次。
冻结前 `live/v50r3` 还不存在，它会照常报错提醒。

估值规则：一只股票从买入那天收盘持有到下一次调仓收盘，MARK 只要求"当日仍持有或
当日买入"的股票有当日收盘价；已卖出的股票之后退市、拆股都不影响估值，出现的疑似
拆股会记在账本里备查。已复核的公司行动表只到 2026-07-14 且被协议绑定；之后的拆股
大多由价格更新自动识别（数据商按新单位改写历史时）。仍无法解释的疑似拆股（单日
比值 ≤0.70 或 ≥1.40，或接近整数拆股比例，且当天没有异常放量）若发生在持有期内，
MARK 会失败关闭；若
发生在可能进入排名池的股票上，SIGNAL 数据包不提升。报错会写明股票和日期。查到
来源后用下面的命令追加一条记录，再重跑即可：

```bash
r3() { PYTHONPATH=. .venv/bin/python scripts/research_v50r3_corrected_v47.py "$@"; }
# 持有中的股票拆股（1 拆 2 → 因子 0.5；日期 = 数据里第一次出现新价位的那天）
r3 record-sourced-event --ticker ABC --type SPLIT --date 2026-11-24 --factor 0.5 \
  --source-url https://example.com/abc-split-announcement
# 确认是真实涨跌、不需要调整
r3 record-sourced-event --ticker ABC --type MARKET_MOVE --date 2026-11-24 \
  --source-url https://example.com/abc-news
# 持有中的股票被收购或退市（停牌不要登记，它会自己恢复）；日期 = 最后一个收盘价
# 日期，收益 = 最终对价/最后收盘价 - 1。登记后这只股票的历史就此结束。
r3 record-sourced-event --ticker ABC --type TERMINAL_RETURN --date 2026-11-18 \
  --terminal-return 0.012 --source-url https://example.com/abc-merger-closing
```

记录写在 `output/research_only/v50/corrected_v47_20260924_r3/sourced_event_supplement.csv`，
只能追加：命令会先核对已存数据（拆股因子与当天跳变之差不超过 30% 的当日涨跌；
确认真实涨跌时那天必须确有疑似拆股的跳变；终值要求确实已停止交易）；已估值过的
持有期内的日期不接受补录，已冻结的净值永不改写。每次估值都在账本里绑定当时用到的
行数和哈希，事后改动已用过的行会被拒绝。调度器提交信号和估值时会把这个文件一并
提交。持有中的股票若只是停牌，MARK 会一直等到恢复交易；选中的股票若在执行日没有
收盘价，那一份留作现金直到下一个信号。

如果错过 SIGNAL 窗口，程序拒绝事后按错过的日期补建，只按上面的规则补跑；不得通过
修改日期或复用未来股票池绕过。这一限制是为了让前瞻证据可复核，不影响历史训练数据
继续用于诊断。
