# Polymarket 日报

每天北京时间 07:00 抓取 Polymarket 预测市场数据，产出一篇小红书正文（4 到 10 条快讯体）和一组 1242×1660 的图片，并在 Dashboard 上展示官方突发榜、快讯、热门、新上线、结算和资金流向。

线上：https://gavixiong.github.io/polymarket-daily/

## 流程

```
Claude Code 定时任务（云端，每天 23:00 UTC）
   ├─ python3 scripts/fetch.py          → data/<日期>.json（含打分后的候选）
   ├─ 写 reports/<日期>.xhs.md          正文
   ├─ 写 reports/<日期>.zh.json         中文标题、封面文案、已选条目
   ├─ python3 scripts/render_cards.py   → cards/<日期>/*.png
   └─ git push                          → GitHub Pages 更新
```

## 数据来源（无需 API key）

| 来源 | 用途 |
|---|---|
| Gamma / CLOB / Data API（公开接口） | 事件、概率、成交量、7 天走势、大单 |
| `polymarket.com/api/biggest-movers?category=` | 官方"Breaking"突发榜，含 24 小时走势 |
| `polymarket.com/breaking` 内嵌的 @Polymarket X 镜像 | 只在内部使用：给市场变动匹配原因、识别官方挂了概率的快讯、新盘公告 |
| `news.polymarket.com/feed` | Polymarket 专栏 RSS |

接口细节与已知坑见 [docs/api-research.md](docs/api-research.md)，写作规则见 [ROUTINE.md](ROUTINE.md)。

## 结构

| 路径 | 内容 |
|---|---|
| `scripts/fetch.py` | 抓取、分类、候选打分（意外 × 关注 × 新鲜，7 天去重）。纯标准库 |
| `scripts/render_cards.py` | 把卡片页截成 PNG。Playwright 优先，本机回退 Chrome |
| `assets/shared.js` | 两个页面共用：格式化、中文覆盖、份额条 / 档位条 / 走势图、下载 |
| `index.html` | Dashboard |
| `cards.html` | 卡片页，`?card=cover` 单张 |
| `config/pins.json` | 长期关注市场的 always / never 覆盖，默认空 |
| `data/` `reports/` `cards/` | 每日数据、正文与覆盖文件、图片 |

## 本地

```bash
python3 scripts/fetch.py
python3 scripts/render_cards.py
python3 -m http.server 8080
```

## 调整

- 长期关注的市场默认按规则自动选（7 天里 70% 的日子进成交量前十，取 5 个）；`config/pins.json` 的 always / never 可强制纳入或排除。
- 阈值、分类标签、噪音黑名单、打分参数：`scripts/fetch.py` 顶部 config 段。
- 卡片样式：`cards.html` 的 CSS；图表画法：`assets/shared.js`。
