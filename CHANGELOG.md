# Changelog

## 1.4.0 (2026-07-15)

**新增**
- 无发现也推送「✅ Bedrock 用量巡检」心跳消息:每个窗口一条,链路通断一目了然(此前无违规时完全静默,无法区分"没事件"和"发送坏了");节流规则对告警/巡检消息同样生效,同一窗口仍最多一条

**变更(措辞)**
- 钉钉消息「分账」改为「无标签用量/标签归属」:告警标题为「Bedrock 无标签用量告警」,并非所有客户都是分账场景,标签还可用于成本归集、项目核算等。⚠️ 机器人若用「关键词」安全设置且关键词为"分账",需同步改为"Bedrock"
- 告警链路诊断日志:`[alert_check]` 入口配置摘要与耗时、`[build_data]` 区域数/耗时/单区失败明细、`[dingtalk]` 发送成功/errcode/异常堆栈/跳过原因、`[load_alerts]` secret 读取失败原因——定时调用失败不再无迹可查

**变更**
- 手动测试(🧪)不再占用节流窗口,测试后不会挤掉下一条定时推送

## 1.3.4 (2026-07-14)

**修复**
- 真实账单(CE)面板日期区间不准:`_range` 已把选中末日 +1 天(含当天),`ce_cost` 又 +1 天,导致查询范围比选择多一整天、展示的账单窗口末日比选中大 2 天(看起来"日期不跟随选择")。修复后 CE 查询精确覆盖 [开始日, 结束日](均含),展示改为"账单窗口 X → Y(含)",与日期选择器一致

## 1.3.3 (2026-07-09)

**变更**
- 区域下拉移除 🌐 global 选项,页面只按具体区域查询(默认 us-west-2);API 的 `region=global` 与后台快照/分账告警的全区扫描不受影响

## 1.3.2 (2026-07-09)

**修复**
- 跨账号角色支持自定义后缀:中心角色 AssumeRole 资源放宽为 `BedrockUsageReader*`;🎲 生成的接入命令角色名自动带随机后缀(`BedrockUsageReader-xxxx`),避免目标账号已被其他看板纳管时 create-role 撞名;`onboard-account.yaml` 新增 `ReaderRoleName` 参数
- 场景:同一个账号要被多个看板(不同中心账号)纳管时,此前角色名写死 `BedrockUsageReader` 且中心角色只授权精确名字,第二个看板接入必然 AccessDenied(中心侧需 `git pull && ./deploy.sh` 更新后生效)

## 1.3.1 (2026-07-09)

**修复**
- 🎲 页面生成的接入命令 inline policy 补上 `ce:GetCostAndUsage`(1.3.0 只更新了中心角色和 onboard-account.yaml,漏了生成命令),否则命令接入的账号「真实账单」跨账号查询 AccessDenied

## 1.3.0 (2026-07-07)

**新增**
- 💰 Bedrock 真实账单面板(Cost Explorer):仅 Amazon Bedrock Service 账单行(UnblendedCost,非估算);<b>跨账号一账号一行</b>(中心 + 全部注册账号,中心同账号自动去重):总费用 / map-migrated 已打标 / 未打标 / 打标占比;按钮触发查询(每账号每次 $0.02 CE API 费用)
- IAM 新增 ce:GetCostAndUsage(中心角色 + onboard-account.yaml reader 模板)

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
