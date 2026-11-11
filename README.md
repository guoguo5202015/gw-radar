# 广外通知雷达

通过 QQ 邮箱 SMTP 推送广外通知；**国际交流合作部的“国际项目”是最高优先级**。

## 国际项目重点监控

监控 [国际项目](https://internationaloffice.gdufs.edu.cn/gjxm/ztqk.htm) 下的全部 8 个栏目：总体情况、交换生项目、单向派出项目、双学位项目、留基委项目、寒暑期项目、合作办学、访学纪闻。

- 每轮读取首页及真实“下页”的后续 2 页。跟随官网倒序分页链接，不猜页码。
- 五个机会栏目优先推送申请、报名、奖学金等项目，标题不含“通知”“报名”也能收录。公示结果、游记、明确仅限研究生或教师的内容不发；同时允许本科生的项目保留。
- 总体情况和合作办学建立基线后，正文或附件链接更新也提醒。其他已收录国际项目的正文、标题、附件名称或链接变更会重新判断并推送。
- 首次接入时补发最近 45 天内的可行动项目，以及正文中明确尚未截止的更早项目。历史档案仍可搜索，不集中发送多年旧通知。
- 国际项目先于其他部门发送；每轮最多 8 封，余下通知留到下轮。正文抓取失败、SMTP 失败或邮件配置缺失均保留重试，成功发送后才记入发送记录。可恢复的抓取/发信异常在 Actions 中显示 warning，仍执行原工作流的记录保存步骤。
- 邮件突出栏目、截止依据、报名资格、附件。正文未明确截止日期时提示核对附件；暂不自动读取 DOC/PDF 附件内部内容，不把推测日期当成事实。

## 运行与配置

GitHub Actions 计划每 30 分钟运行，GitHub 可能延迟调度。支持手动运行，默认不额外发送样例邮件。开发环境需要样例时可设置 `SEND_SAMPLE=true`。

在仓库 Actions Secrets 设置 `MAIL_USER`、`MAIL_PASS`（QQ SMTP 授权码），可选 `MAIL_TO`；缺省收件人是发件人自身。不要把凭据写入源码。

```sh
python gw_notify.py --dry-run --international-only
python gw_notify.py --dry-run --international-only --preview-dir /tmp/radar-preview
python -m unittest discover -s tests -v
python gw_notify.py
```

`--dry-run` 不发信、不改 `seen.json` 或网页；只有指定 `--preview-dir` 才将邮件预览和核验摘要写入该目录。

`seen.json` 保存收录记录、内容指纹、待发送队列、成功发送标记及每个来源的抓取状态。不要清空此文件，否则会失去去重记录。首次全新运行只建立底库。

网页由 GitHub Pages 的 `main` 分支 `/docs` 发布，默认显示国际项目。其他部门的来源及旧通知保留。程序仅使用 Python 标准库。
