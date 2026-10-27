# 0.6.0 升级记录（2026-10-27）

本 fork 从 0.5.0 合并上游正式版 `v0.6.0`，提交为
`e1ce49e83dfefeb985b1db24d173cb785ca75dbc`。

## 已保留

- 中文报告、原有 X 账号和 AI video 主题。
- 全部 14 组 sitemap，包括 Toolify 分片修复、Dreamina 和 Media.io 的范围限制、基线导入与缓存。
- Gmail SMTP、Resend 兼容、定时任务频率和现有状态文件。
- 原有五个收款平台、Reddit 求助与原有付费查询的配置。

新版本修复 AIsa 新站发现接口，改善 MCP 错误信息；报告可使用已有数据提供
12 个月趋势、首页论坛与 AI 摘要信息，卖家筛选也得到更新。

## 新增查询的开关

升级使用 `config.json` 显式覆盖上游新增默认值，避免自动扩大付费采集范围。
原有付费功能仍按既有配置运行，实际总费用随数据量变化。

| 配置 | 本 fork 的值 | 原因 |
|---|---|---|
| `word_age.enabled`、`markets.enabled`、`serp_shape.enabled` | `false` | 会增加付费查询，或在无法复用结果时购买新查询 |
| `new_sites.words` | `0` | 暂不额外购买新站排名词 |
| `asks.extra_sources`、`feeds.enabled` | `[]` | 新来源可能调用付费接口，免费来源也会增加后续模型处理量 |
| `sellers.platforms` | 原有五个平台 | 不自动增加 Paddle、Dodo、Waffo 的付费查询 |
| `builders.weekday`、`money.weekday` | `null` | 不新增定时付费信号 |
| `sidelights.names`、`sidelights.whois_names`、`sidelights.products` | `0` | 不新增 WHOIS、Discord 和产品长尾查询 |
| `ai_answers.enabled` | `false` | 保持付费 AI 回答关闭 |
| `competition.page_age`、`dot_com.enabled` | `false` | 暂不增加 RDAP 等待时间；域龄目前也不参与决策 |

`year_curve` 和 `yours` 保留上游默认值，复用已有月搜索量、首页结果及已配置的
Search Console 数据。此 fork 当前没有配置自有站或 Search Console。

上述开关只属于此 fork 的 `config.json`；上游示例配置仍保留上游默认值。

## 验证与回滚

升级后运行完整测试、构建检查，以及不访问网络、不发邮件、使用临时状态目录的
命令行检查。`radar.db` 的表结构无需迁移。

更新前的云端代码保留在本地分支 `codex/backup-before-0.6.0-20261027`。
本机 `Data/AI/new-term-radar` 的部署单独备份和合并，保留其未提交补丁及配置，
不使用云端配置覆盖本机。测试通过不代表本机先前的网络故障已经恢复。

JEV 调用记录：`jev_called=false`。原因：这是一次性版本升级与复杂代码审查；
版本、文件、开关和测试结果均可直接检查，JEV 不会降低总成本或总耗时。
规则来源：本次会话的 AGENTS.md JEV 使用规则。
