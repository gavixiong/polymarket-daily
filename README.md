# Polymarket 日报

每天北京时间 08:00 自动抓取 Polymarket 预测市场数据，生成一篇 300–600 字的中文短报，并在 Dashboard 上展示时政、金融、科技三类市场的概率变动、热门事件、新上线、刚结算和万元级大单。

## 它怎么工作

```
Claude Code 定时任务（云端，每天 00:00 UTC）
   └─ python3 scripts/fetch.py      → data/<日期>.json + data/index.json
   └─ 读摘要，写 reports/<日期>.md  （中文短报）
   └─ git commit && git push        → GitHub Pages 自动更新 index.html
```

数据全部来自 Polymarket 的三个公开接口（Gamma、CLOB、Data API），不需要 API key，不需要钱包。接口说明、标签 ID 和已知坑见 [docs/api-research.md](docs/api-research.md)。

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

## 目录

| 路径 | 内容 |
|---|---|
| `index.html` | Dashboard 单页，无构建 |
| `scripts/fetch.py` | 数据抓取，纯标准库 |
| `config/watchlist.json` | 固定关注池 |
| `data/` | 每日结构化数据 |
| `reports/` | 每日中文短报 |
| `docs/` | API 调研 |
| `ROUTINE.md` | 定时代理的执行说明 |
