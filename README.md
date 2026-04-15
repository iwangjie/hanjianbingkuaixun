# 🩺 罕见病每日快讯

自动采集全球罕见病领域最新资讯，每日更新。

## 功能

- ⏰ 每天北京时间 17:00 自动采集罕见病相关新闻
- 🌐 使用 [Exa API](https://exa.ai) 搜索全球罕见病新闻
- 🔄 使用 LLM 自动翻译为中文
- 📁 按日期存储 JSON 数据，便于查看历史记录
- 🌍 提供 GitHub Pages 在线浏览页面

## 在线查看

访问: [https://iwangjie.github.io/hanjianbingkuaixun/](https://iwangjie.github.io/hanjianbingkuaixun/)

## 数据结构

```
data/
├── dates.json              # 日期索引
├── 2026-04-15/
│   ├── news_en.json        # 英文版
│   └── news_zh.json        # 中文版
├── 2026-04-14/
│   ├── news_en.json
│   └── news_zh.json
└── ...
```

每条快讯包含字段：
- `title` - 标题
- `summary` - 简介/摘要
- `date` - 发布日期
- `url` - 原始链接

## 手动触发

可在 GitHub Actions 页面手动触发 workflow 执行采集。
