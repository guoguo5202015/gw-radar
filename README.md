# 广外通知雷达

每30分钟抓取广外各部门公开网站的通知，有新通知就推送到微信（PushPlus），并生成可搜索的网页。

- 网页：开启 GitHub Pages（main 分支 /docs 目录）后访问
- 推送 token 放在仓库 Settings → Secrets and variables → Actions，名字为 `PUSHPLUS_TOKEN`
- 抓取来源在 `gw_notify.py` 的 `SOURCES` 里增删
- 首次运行只建立底库，不推送
