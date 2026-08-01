# BoltMind 获客模块

夜里跑的精准获客流水线：**海关数据线索 → 爬官网调研 → AI 生成公司画像/切入点/开发信草稿 → 早上一页早报 → 你审核修改 → 你亲手点发送 → 客户回复后 AI 分析意图并起草回信 → 你再审。**

AI 干 80% 的脏活，**扣扳机的永远是你**。这不是一句口号，是代码层面的硬约束：整个流水线跑完，产物只是数据库里一批 `status='pending'` 的草稿。发信只有一个入口（`mailer.send_draft`），需要同时满足三个条件才会真的走 SMTP：

1. 草稿状态是 `approved` —— 而 `approved` 只能由人在后台点出来
2. 配置里 `BOLTMIND_ALLOW_SEND=true`
3. 调用时显式传 `confirm=True`

这三道闸各自都有测试守着（`tests/test_mailer.py`），重构时不会悄悄失效。

---

## 一分钟跑起来

```bash
pip install -r requirements.txt
cp .env.example .env          # 填 ANTHROPIC_API_KEY 和公司信息
python -m boltmind.cli init

# 用虚构的演示数据跑一遍，看看产出长什么样（会真的调 API，约 $0.2-0.5）
python -m boltmind.cli run-night --demo --limit 3

# 打开审核后台
streamlit run app.py
```

没有 API key 也能看代码怎么跑：`python -m pytest tests/ -q`，90 个测试全程不联网。

---

## 每天的实际用法

### 凌晨（机器干）

```bash
0 3 * * * /path/to/repo/scripts/night.sh
```

`night.sh` 依次做四件事：读 `data/inbox/` 里的海关数据导出文件 → 规则预打分排序 → 挑前 N 家爬官网 → 调 AI 出画像和草稿 → 生成早报。跑完在 `data/briefs/brief-YYYYMMDD.md`（和 `.html`）。

### 早上（你干，30 分钟）

```bash
streamlit run app.py        # 或者直接看 data/briefs/*.html
```

后台第一屏就是那句汇报：**「昨晚处理了 8 家公司，3 家高优先级，草稿在此，理由如下」**。每家公司下面挂着：

- 为什么给这个优先级（AI 写的理由 + 规则预打分对照）
- 公司画像（业务、供应链角色、采购特征、该找什么岗位的人）
- 2-4 个切入点，**每个都标注依据来自哪条提单/官网哪一页**
- 风险提示（材料不足、疑似货代、疑似同行……）
- 开发信草稿全文，可以直接在页面上改

改完点「保存并通过审核」，再点「确认发送」。或者点「导出 .eml」拖进 Outlook 手动发 —— 前两周建议走这条路，`BOLTMIND_ALLOW_SEND` 保持 `false`，先确认草稿质量。

### 客户回复后

```bash
python -m boltmind.cli poll-replies
```

拉 IMAP → 用 Message-ID 匹配到之前发出去的那封信 → AI 分析意图（询价/要样品/要规格/暂时不需要/退订…）、紧急度、具体诉求和异议 → 起草回信。回信同样只存草稿。

**价格、交期、能否做某规格 —— AI 一律用 `[[待确认: ...]]` 占位，绝不自己编数字。** 占位符没填完就点发送，会被 `preflight` 直接拦下。

---

## 海关数据怎么进来

海关数据商（腾道、易之家、Panjiva、ImportGenius…）基本都是网页下载或收费 API，没有统一标准。所以流水线的入口设计成「文件」：

```
data/inbox/2026-08-buyers.csv     ← 从数据商后台导出，丢进来就行
data/inbox/2026-08-buyers.xlsx    ← .xlsx 也行（需要 openpyxl）
```

列名靠别名表自动识别，中英文都覆盖：`Buyer Name` / `采购商` / `Consignee` / `Importer` 都能认出是公司名列。认不出就手动指定：

```bash
python -m boltmind.cli import --path data/inbox/x.csv --map company_name=客户全称 --map value_usd=成交金额
```

接了付费 API 之后，写一个新的 source 类（实现 `fetch() -> Iterable[CustomsLead]`），放进 `boltmind/sources/`，其余代码一行不用改。

去重是按**归一化公司名**做的 —— `ACME FASTENERS CO., LTD.` 和 `Acme Fasteners Ltd` 会合并成同一家；提单按内容指纹去重，同一份文件重复导入不会产生脏数据。

---

## 钱花在哪、怎么控

每晚的 AI 花费有硬上限（`BOLTMIND_NIGHTLY_BUDGET_USD`，默认 $5）。到顶就停，剩下的公司留到下一轮，**不会超支**。

省钱的几个设计：

| 手段 | 效果 |
|---|---|
| 规则预打分先排序，只有排在前面的才进 AI | 一晚几百条线索 → 只有 20 家花 AI 的钱 |
| 系统提示词打 `cache_control` | 每晚几十次调用共用一份缓存，输入成本降 ~90% |
| 低优先级公司不生成开发信 | 判断为不值得跟进的，省掉第二次调用 |
| 官网正文截断到 12K 字符 | 避免某个站点的长文档把 token 打爆 |
| 回复不匹配我们发出去的信 → 直接跳过 | 陌生邮件不烧 AI 预算 |
| 自动回复（out of office）识别后不送模型 | 同上 |

按默认配置，一家公司走完「分析 + 起草」两次调用大约 $0.1-0.2（claude-opus-5）。想再省就把 `BOLTMIND_EFFORT` 调到 `medium`，或者在 `.env` 里换成 `claude-sonnet-5`。

---

## 项目结构

```
boltmind/
  config.py          环境变量配置 + 我方公司信息（会写进提示词）
  db.py              SQLite 建表
  store.py           所有 SQL 集中在这里
  scoring.py         规则预打分（活跃度/新鲜度/金额/品类/可触达性）
  pipeline.py        夜间流水线编排 —— 不含任何发送逻辑
  brief.py           早报生成（Markdown + HTML）
  mailer.py          ⚠️ 唯一的发送入口，三道闸门都在这
  inbox.py           IMAP 收信 + 线程匹配 + 回信起草
  cli.py             命令行入口
  sources/           海关数据源（CSV/Excel 适配器、演示数据、可扩展）
  enrich/            官网爬取（robots.txt、限速、HTML 抽取）
  ai/                Claude 调用（结构化输出、重试、计费、预算闸门）
app.py               Streamlit 审核后台
scripts/night.sh     cron 用的夜间跑批脚本
tests/               90 个测试，全程不联网
```

数据库表：`companies` / `customs_records` / `enrichment` / `analyses` / `drafts` / `replies` / `runs`。直接 `sqlite3 data/boltmind.db` 就能查。

---

## 常用命令

```bash
python -m boltmind.cli init                      # 建库建目录
python -m boltmind.cli import --path <文件/目录>  # 只导数据，不跑 AI
python -m boltmind.cli run-night --limit 10      # 跑一轮完整流水线
python -m boltmind.cli brief --print             # 打印最近一次早报
python -m boltmind.cli drafts                    # 列出待审核草稿
python -m boltmind.cli show 12                   # 看 12 号草稿全文
python -m boltmind.cli approve 12 --to buyer@x.com
python -m boltmind.cli send --draft-id 12 --confirm
python -m boltmind.cli export-eml --draft-id 12  # 导出手动发
python -m boltmind.cli poll-replies              # 拉回复并起草回信
python -m boltmind.cli stats                     # 看数据总量
```

---

## AI 会不会编东西

这是这套系统最大的风险点，所以做了几层防护：

- **提示词硬约束**：只能用材料里出现过的事实，编造邮箱/人名/价格/认证/产能视为不合格输出；每个切入点必须能指回具体来源。
- **结构化输出**：用 `output_config.format` + JSON Schema 强约束，不会返回一段散文导致解析崩掉；解析失败会自动重试。
- **邮箱不采信模型**：`resolve_recipient()` 要求模型推荐的收件邮箱**必须在爬到的邮箱列表里出现过**，否则不采信（`tests/test_pipeline.py::test_resolve_recipient_rejects_invented_email` 守着这条）。
- **优先级下调**：模型说 high 但规则预打分很低时自动降级，防止把垃圾线索抬上去。
- **占位符机制**：不确定的信息用 `[[待确认: ...]]`，没填完发不出去。
- **草稿里附「引用的事实」清单**：每封信都列出用了哪些事实、来自哪里，你扫一眼就能核对真伪。

即便如此，**发之前还是得自己读一遍**。这就是为什么这套系统的终点是草稿而不是发送。

---

## 合规

写代码时已经处理的部分：

- 爬虫默认遵守 `robots.txt`，串行 + 每次请求间隔 1 秒，带可识别的 User-Agent，只抓公开页面，不登录不绕验证码。
- 不群发。一家公司一封信，人工逐封确认。
- 识别退订意图（`unsubscribe`）后不再生成回信草稿，并提示加入不再联系名单。

需要你自己确认的部分：

- **给欧盟企业发商业邮件**受 GDPR 约束，通常靠「合法利益」基础，但要求提供退订方式并能说明数据来源。建议在邮件签名里加一行退订说明和数据来源声明。
- **美国** CAN-SPAM 要求真实的发件人信息、有效的实体地址和退订机制。
- **海关数据本身的使用条款**看你和数据商签的合同。
- 部分国家（德国 UWG、加拿大 CASL）对未经同意的商业邮件有更严格的规定，B2B 开发信前值得先查一下目标市场的规则。

这套系统能帮你把信写好，但发给谁、按什么频率发、怎么处理退订，是业务判断，不是代码能替你决定的。

---

## 接下来能做什么

这套东西本身就是 BoltMind 的获客模块，可以直接往上长：

- **多渠道**：LinkedIn / 展会名单 / 询盘平台接进来，共用同一套画像 + 打分 + 草稿逻辑（只需新增一个 source）
- **跟进序列**：发出去 7 天没回复 → 自动生成第二封跟进草稿（仍然人工审核）
- **成单回流**：把「哪些切入点最后成交了」喂回打分模型，让预打分越用越准
- **和现有仓储系统打通**：`Super smart warehousing system.txt` 里那套库存看板 —— 客户问某规格有没有现货时，回信草稿可以直接带上真实库存
- **多人协作**：现在是单人单机 SQLite，要多个销售分线索的话换 Postgres + 加个 owner 字段就行

---

## 提醒

`data/` 和 `.env` 都在 `.gitignore` 里 —— 海关数据、客户邮箱、API key 不会被提交到仓库。第一次真跑建议 `--limit 1`，确认一家公司的产出质量满意了，再放开量。
