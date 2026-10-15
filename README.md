# Polymarket 日报

每天北京时间 08:00 自动抓取 Polymarket 预测市场数据，生成一篇 300–600 字的中文短报，并在 Dashboard 上展示时政、金融、科技三类市场的概率变动、热门事件、新上线、刚结算和万元级大单。

## 它怎么工作

```
Claude Code 定时任务（云端，每天 00:00 UTC）
   └─ python3 scripts/fetch.py      → data/<日期>.json + data/index.json
   └─ 读摘要，写 reports/<日期>.md  （中文短报）
   └─ git commit && git push        → GitHub Pages 自动更新 index.html
```

数据来源，全部无需 API key：

| 来源 | 用途 |
|---|---|
| Gamma / CLOB / Data API（公开接口） | 市场、概率、成交量、走势、大单、持仓 |
| `polymarket.com/api/biggest-movers?category=` | 官方"Breaking"突发榜，按类别，含 24h 走势 |
| `polymarket.com/breaking` 页面内嵌的 @Polymarket X 镜像 | 官方 X 快讯（约 60 条/天）与新盘公告，脚本按关键词粗分为政经 / 科技 / 文体 / 其他 |
| `news.polymarket.com/feed` | Polymarket 自己的专栏 RSS |

接口说明、标签 ID 和已知坑见 [docs/api-research.md](docs/api-research.md)。日报结构与写作规则见 [ROUTINE.md](ROUTINE.md)。

## 本地运行

```bash
python3 scripts/fetch.py
python3 -m http.server 8080
# 浏览器打开 http://localhost:8080
```

## 调整

- 关注池：编辑 `config/watchlist.json`，slug 取自 `polymarket.com/event/<slug>`。
- 阈值（变动幅度、成交量门槛、每类条数）：`scripts/fetch.py` 顶部常量。
- 分类标签与噪音黑名单：同文件的 `CATEGORIES` 与 `NOISE`。
- 短报写法：`ROUTINE.md`。

## 小红书

每天三样产出：`reports/<日期>.xhs.md` 是 4 到 10 条快讯体正文，`cards/<日期>/` 是 1242×1660 的图片（固定四张：封面、头条走势、突发榜、长期盘；浮动四张：大单、结算、新盘、快讯，满足条件才出），`cards.html` 是图片的源页面。
正文的候选由 `scripts/fetch.py` 打分（意外 × 关注 × 新鲜）并按 `reports/*.cards.json` 里的 `picked` 做 7 天去重，最终挑选由写作代理完成。图片用 `scripts/render_cards.py` 生成，优先 Playwright，没有就用本机 Chrome。

## 目录

| 路径 | 内容 |
|---|---|
| `index.html` | Dashboard 单页，无构建 |
| `cards.html` | 小红书卡片页，`?card=cover` 单张 |
| `scripts/fetch.py` | 数据抓取与候选打分，纯标准库 |
| `scripts/render_cards.py` | 卡片截图成 PNG |
| `cards/` | 每日图片 |
| `config/watchlist.json` | 固定关注池 |
| `data/` | 每日结构化数据 |
| `reports/` | 每日中文短报 |
| `docs/` | API 调研 |
| `ROUTINE.md` | 定时代理的执行说明 |
