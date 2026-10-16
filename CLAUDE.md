# Polymarket 日报

每天自动生成一篇小红书正文和一组图片，Dashboard 在 GitHub Pages 上。

## 布局

- `scripts/fetch.py` — 唯一的数据脚本，纯标准库。分 config / http / shaping / sources / scoring / summary / main 七段。写 `data/<日期>.json`，打印摘要。
- `scripts/render_cards.py` — 把 `cards.html` 截成 PNG 到 `cards/<日期>/`。
- `scripts/check_zh.py` — 校验五张图上每一行都有中文标签，缺的按 JSON 片段补进 `*.zh.json`。
- `assets/shared.js` — 两个页面共用的格式化、中文覆盖、图表（份额条、档位条、走势图）。
- `index.html` Dashboard、`cards.html` 卡片页，都是静态页，无构建。
- `reports/<日期>.xhs.md` 正文、`reports/<日期>.zh.json` 中文覆盖与已选条目，由定时代理按 `ROUTINE.md` 写。
- `config/pins.json` 长期关注市场的 always / never 覆盖（默认空，规则自动选）。`docs/api-research.md` 接口实测说明。

## 命令

```bash
python3 scripts/fetch.py            # 抓今天的数据
python3 scripts/render_cards.py     # 生成图片
python3 -m http.server 8080         # 本地看页面
```

## 约定

- 日期按北京时间（UTC+8）。
- 事件分三种：binary / exclusive（互斥，画份额条）/ ladder（阈值，各画一条）。分类在 `event_brief`。
- 定时任务只改 `data/`、`reports/`、`cards/`。脚本和页面由人工会话改。
- 中文只来自 `*.zh.json` 覆盖文件和 `shared.js` 里的少量固定词典，数据文件保持英文原文。
