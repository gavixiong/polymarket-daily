# Polymarket 日报

每天自动生成一份 Polymarket 预测市场中文短报（时政 / 金融 / 科技），并在 GitHub Pages 上展示。

## 布局

- `scripts/fetch.py` — 唯一的数据脚本，纯标准库。写 `data/<日期>.json` 与 `data/index.json`，打印摘要。
- `config/watchlist.json` — 固定关注池（event slug 列表）。
- `index.html` — Dashboard，静态页，直接 fetch `data/` 与 `reports/`。无构建步骤。
- `reports/<日期>.md` — 每日中文短报，由定时代理按 `ROUTINE.md` 撰写。
- `docs/api-research.md` — 三个 Polymarket 公开 API 的实测说明、标签 ID、已知坑。

## 命令

```bash
python3 scripts/fetch.py            # 抓今天的数据
python3 scripts/fetch.py 2026-10-15 # 指定日期文件名（数据仍是当前快照）
python3 -m http.server 8080         # 本地看 Dashboard
```

## 约定

- 日期一律按北京时间（UTC+8）。
- 定时任务只能改 `data/` 和 `reports/`，脚本和页面的修改由人工会话完成。
- 分类靠 Gamma 标签 ID，噪音靠 `NOISE` 正则黑名单；调阈值改 `scripts/fetch.py` 顶部常量即可。
