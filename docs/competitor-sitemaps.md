# Dreamina / Media.io 云端采集

这两站现在属于 `config.json` 的原生 sitemap 来源，与原有 12 组来源一起由同一个 GitHub Actions 日报读取，无需另设定时任务。原来的 API、定时和邮件设置保留。

- **Dreamina**：递归读取英文 article/default sitemap，仅统计配置中的 ai-image、ai-video、resource、create、seedance、tools、ai-model、ai-voice、seedream、blog 路径。
- **Media.io**：读取 sitemap 中的 `/ai/explore/zone/` 路径。

覆盖完整只指这些配置的路径，不代表全站。扫描遵守 robots；选中的任何分片读取失败时，保留此前基线并在报告标明错误，不将部分结果作为成功快照。其他语言副本和 XML 索引不计为页面。

## 历史基线

`bootstrap/competitor-sitemaps/` 保存此前采集的公开 URL 基线：Dreamina 7,840 条、Media.io 1,369 条。保留 first_seen、last_seen 和 sitemap lastmod；first_seen 是监控首次发现时间，lastmod 是站点声明的修改时间，两者均不等于发布日期或搜索热度。种子不包含密钥、邮箱或本机路径。

工作流在恢复缓存后运行 `python scripts/seed_competitor_sitemaps.py`。只有对应快照缺失时才导入，已有快照始终优先；损坏、越界或范围不匹配会报错，不覆盖已有数据。后续成功扫描累计保存已见 URL，减少页面暂时消失再出现导致的重复提醒。

Actions 缓存过期时仍可补回这份历史基线，但种子之后新增的已见历史需要最近的缓存或产物恢复；种子不是持续更新的备份。每次产物同时保存这两站的快照。

## 免费连接验收

在 **Actions → New-term radar → Run workflow** 中选择 `command: competitors`。

此模式仅请求两站公开 robots 和 sitemap，不注入 AIsa、其他付费 API 或邮件凭据，也不运行完整日报。它复制正式快照到临时目录进行扫描，正式快照不前移，因此这次验收发现的新 URL 仍能进入下一次日报。历史基线不会被当成当天新增。

结果在工作流摘要和产物的 `competitors-report.md`、`competitors-report.json`、`competitors-status.json` 中。JSON 保存完整新增 URL、扫描时间、配置覆盖以及正式快照未变的哈希证明。任一站未完整成功、robots 未核验、基线未复用或正式快照变化时，验收失败并保留错误证据。

完整日报仍按原邮件逻辑发送；新 URL 只是选词线索，不自动等于热门产品或欧美搜索需求。
