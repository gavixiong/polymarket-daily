# Polymarket API 调研（2026-10-15 实测）

全部为公开接口，无需 API key，无需钱包。只读。

## 三个 API 的分工

| API | Base URL | 用途 | 日报相关度 |
|---|---|---|---|
| Gamma | `https://gamma-api.polymarket.com` | 市场/事件元数据、价格快照、成交量、标签、分类 | 核心，80% 的数据来自这里 |
| CLOB | `https://clob.polymarket.com` | 订单簿、价格历史曲线 | 画走势图、算 24h/7d 变化 |
| Data API | `https://data-api.polymarket.com` | 成交记录、持仓、大户 | 大额交易、"聪明钱"动向 |

## 1. Gamma API：市场与事件

### 层级
- **Event（事件）** = 一个话题，例如 "Fed Decision in October?"，下面挂多个 Market。
- **Market（市场）** = 一个具体二元问题，例如 "Will there be no change in Fed rates?"，有 Yes/No 价格。
- 价格 = 概率。`outcomePrices: ["0.735","0.265"]` 即 Yes 73.5%。

### 关键 endpoint

```
GET /events?active=true&closed=false&order=volume24hr&ascending=false&limit=100&offset=0
GET /markets?active=true&closed=false&order=volume24hr&ascending=false&limit=100&offset=0
GET /events?tag_id=2          # 按标签 ID 过滤
GET /events?tag_slug=tech     # 按标签 slug 过滤
GET /markets?closed=true&tag_id=2&order=closedTime&ascending=false   # 刚结算的
GET /events?order=createdAt&ascending=false                          # 新上线的
GET /tags?limit=1000          # 标签列表（实测只返回 100 条，不全）
```

- 每页最多 **100 条**，用 `offset` 翻页。
- 排序字段实测可用：`volume24hr`, `volume`, `liquidity`, `createdAt`, `closedTime`, `endDate`。

### Market 上可直接用的字段

| 字段 | 含义 | 备注 |
|---|---|---|
| `question` | 问题文本 | |
| `outcomes` / `outcomePrices` | 选项与当前概率 | JSON 字符串，要 `json.loads` |
| `oneDayPriceChange` | 24 小时概率变化 | **现成的，不用自己算**。约 78% 市场有，无变化时为 null |
| `oneWeekPriceChange` / `oneMonthPriceChange` | 7 天 / 30 天变化 | 同上 |
| `volume24hr` / `volume1wk` / `volume` | 成交量 | 美元 |
| `liquidity` | 流动性 | |
| `bestBid` / `bestAsk` / `spread` / `lastTradePrice` | 盘口 | |
| `endDate` | 预计结算时间 | |
| `closedTime` | 实际结算时间 | 只有 closed=true 的有 |
| `description` | 结算规则全文 | 可喂给 LLM 写解读 |
| `resolutionSource` | 结算依据 URL | |
| `clobTokenIds` | 两个 token ID | 查 CLOB 价格历史要用 |
| `conditionId` | 市场链上 ID | 查 Data API 持仓要用 |
| `events[].tags[]` | 所属标签 | 分类用 |
| `events[].commentCount` | 评论数 | 热度参考 |
| `competitive` | 0~1 竞争度 | 越接近 1 越"不确定"，越有讨论价值 |

### 标签 ID（实测常用）

| ID | 标签 | 日报分类 |
|---|---|---|
| 2 | Politics | 时政 |
| 100265 | Geopolitics | 时政 |
| 144 | Elections | 时政 |
| 1597 | Global Elections | 时政 |
| 126 | Trump | 时政 |
| 78 | Iran | 时政 |
| 102289 | Midterms | 时政 |
| 100328 | Economy | 金融 |
| 120 | Finance | 金融 |
| 21 | Crypto | 金融（噪音大，见下） |
| 235 | Bitcoin | 金融 |
| slug `tech` | Tech | 科技 |
| slug `business` | Business | 科技/金融 |
| 537 | OpenAI | 科技 |
| slug `science` | Science | 科技 |
| 1 | Sports | **排除** |
| 100639 | Games / 64 Esports | **排除** |

`tag_slug` 实测可用：`tech`, `business`, `crypto`, `geopolitics`, `world`, `finance`, `economy`, `science`。

## 2. CLOB API：价格历史

```
GET /prices-history?market=<clobTokenId>&interval=1w&fidelity=360
GET /prices-history?market=<clobTokenId>&interval=1d&fidelity=60
```
- `interval`: `1h` `6h` `1d` `1w` `1m` `max`
- `fidelity`: 采样间隔（分钟）
- 返回 `{"history":[{"t":unix秒,"p":价格},...]}`
- 用途：日报里画小走势图，或者补算 Gamma 缺失的 oneDayPriceChange。

## 3. Data API：成交与持仓

```
GET /trades?limit=200&takerOnly=true
GET /trades?limit=50&takerOnly=true&filterType=CASH&filterAmount=10000   # 单笔 ≥ $10k
GET /trades?market=<conditionId>&limit=100
GET /holders?market=<conditionId>&limit=10
```
- trades 字段：`title`, `outcome`, `side`(BUY/SELL), `price`, `size`, `name`/`pseudonym`(交易者), `proxyWallet`, `timestamp`, `eventSlug`, `transactionHash`
- `size * price` = 美元金额
- holders 返回每个 token 的前 N 大持仓者及持有份额。
- 用途："今日大单"、"谁在重仓"。有用户名，可读性好。

## 4. 实测发现的坑

1. **噪音极大**。按 24h 成交量排序，前排全是体育、电竞、"Bitcoin Up or Down 5 分钟"、每日气温这类。必须按标签过滤 + 关键词黑名单（`Up or Down`, `temperature`, ` vs `, `Game`, `Map`, `shots on target`, `Spread:`）。
2. **刚结算的市场**同样被体育淹没，要按 tag_id 过滤后再取 `order=closedTime`。
3. `oneDayPriceChange` 对低价市场（价格 0.001）会出现 -0.49 这种假大动，要配合最低价格或最低成交量门槛（例如 price 在 0.05~0.95 之间且 volume24hr > $20k）。
4. `/tags` 只返回 100 条，拿不全，直接用上表的 ID 即可。
5. 评论接口 `/comments?parent_entity_type=Event&parent_entity_id=` 返回空，暂不可用。
6. 无认证时没有发现速率限制问题，但建议每次请求间隔 ≥ 200ms，日报一次跑下来大约 20~40 个请求。
7. **财报市场**（2026-10-22 实测）：标签 Earnings `tag_id=1013`，每家公司每季一个"Will Meta (META) beat quarterly earnings?"，description 里写了预计发布日（"release earnings on October 21, 2026"）和一致预期（"consensus estimate for Meta's GAAP EPS … is $6.31"）。成交只有几百到一万多美元，过不了任何成交门槛，所以单独抓：已结算的按 `/events?tag_id=1013&closed=true&order=endDate` 取，`closedTime` 在财报发布后几小时到半天。

## 5. 日报可以直接产出的板块（数据已验证可取）

| 板块 | 数据来源 | 筛选逻辑 |
|---|---|---|
| 今日最大变动 | Gamma `oneDayPriceChange` | 时政/科技/金融标签，abs 变化 ≥ 5 个点，价格 0.05~0.95，volume24hr ≥ $20k |
| 今日热门 | Gamma `volume24hr` | 同标签过滤，去噪 |
| 今日新上线 | Gamma `order=createdAt` | 过去 24h，同标签过滤 |
| 今日已结算 | Gamma `closed=true&order=closedTime` | 按标签拉，过去 24h |
| 大单追踪 | Data API `filterAmount=10000` | 过去 24h，排除体育 |
| 临近结算 | Gamma `endDate` | 未来 7 天到期且仍有争议（price 0.2~0.8） |
| 固定关注池 | 手动维护 event slug 列表 | Fed、美国大选、中美、AI 公司等，每天固定报概率 |

## 6. 官网非公开接口（2026-10-16 补充，需浏览器 User-Agent）

| 接口 | 返回 | 备注 |
|---|---|---|
| `GET https://polymarket.com/api/biggest-movers?locale=en&category=<politics\|world\|finance\|tech\|crypto\|culture>` | `{"markets":[{question,currentPrice,oneDayPriceChange,closed,events[{slug}],history[47 点],clobTokenIds}]}` | 就是 polymarket.com/breaking 页面的数据。含刚结算跳到 100% 的市场。不带 category 时返回全站前 25 |
| `GET https://polymarket.com/breaking`（HTML） | 页面内嵌 @Polymarket 过去约 24 小时的 X 帖子：`<a href="https://x.com/polymarket/status/<id>">`，含 "Breaking news" / "New polymarket" 两种类型、UTC 时间、正文、配图 | 没有 JSON 接口；正则见 `scripts/fetch.py` 的 `_POST_RE`。实测一天约 66 条，63 条快讯 + 3 条新盘，只有 1 条带概率 |
| `GET https://news.polymarket.com/feed` | 标准 RSS，Polymarket 专栏，约每周 2 篇 | pubDate 带 `GMT` 后缀，`%z` 解析前要替换成 `+0000` |

首页"热门"列表翻页请求实测为 `gamma-api.polymarket.com/events/keyset?order=volume24hr&ascending=false`，即纯 24 小时成交量倒序；首屏精选与"中期选举焦点"卡片是编辑置顶。
