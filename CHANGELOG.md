# Changelog

## 1.2.0 (2026-07-07)

**新增**
- 🔕 告警忽略清单:配置页可按模型/profile id 豁免(支持前缀通配 `global.*`),白名单内用量不告警,消息尾注跳过数量
- ⏱ 推送按窗口节流:同一窗口(6/12/24h)最多推送一条,选大窗口不再因 6h 扫描频率重复轰炸;节流状态存 S3(cache/alert-state.json),🧪 手动测试不受节流限制

**变更**
- 定时扫描频率与推送频率解耦:EventBridge 照旧每 6h 扫描(顺带刷新页面快照),仅推送节流

## 1.1.0 (2026-07-06)

**新增**
- 模型三形态区分:直连模型 ID / 系统跨区 profile / application inference profile(显示"名字 (底层模型)")
- 类型列(绿=可分账 app profile / 黄=不可分账)+ 整行悬浮即时显示 ARN / ModelId
- 🔔 分账告警:非 app inference profile 用量 → 钉钉 webhook(可加签),EventBridge 定时(`ALERT_RATE` 参数),页面配置 + 异步"立即检查"
- 📸 S3 快照缓存:定时任务刷新 7 天 global 快照,页面打开约 0.3s 出数;点「查询估算」取实时
- 页脚显示版本号

**变更**
- 一键部署 CFN 化:`deploy.sh` = package + deploy 包装,全部资源栈内管理;`destroy.sh` = 删栈(自动清空缓存桶 + 兜底清理 secrets 回收期)
- 错误监控 / 运行时灰区面板默认隐藏(`OPS_PANELS=true` 开启)
- 默认查询窗口 30 天 → 7 天;CloudFront 源超时 30s → 60s
- UI 降噪:去霓虹渐变,靛蓝实色主题;删"单价来源"列

**修复**
- global 视图 inference profile 反查拖慢导致 504
- 手动测试告警被 CloudFront 超时重试放大成多条推送(改异步自调用)
- 钉钉 markdown 换行(需双换行)

## 1.0.0 (2026-06-24)
- 初版:用量/成本估算、global 聚合、多账号跨 Org、单价配置、灰区统计、错误监控
