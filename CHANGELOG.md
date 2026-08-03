# Changelog

## 1.8.0 (2026-08-03)

**新增**
- **「🔑 IAM Principal 打标」面板**:列出中心 + 全部注册账号里**有 Bedrock 调用权限的 IAM Role / User**,逐个显示 `map-migrated` 打标状态(已打标 / 无效打标 / 未打标)、权限来源、最后使用时间,未打标行直接给出可复制的 `aws iam tag-role|tag-user` 修复命令。对应 MAP 文档新增的 **IAM principal tagging(2026-06-08 起生效,官方推荐)**——给调用方 Role/User 打标即可分账,**不必再创建 application inference profile、不必改应用代码**
- 新 API `GET /?format=principals`(`&cached=1` 读快照、`&limit=` 调扫描上限);快照随 EventBridge 定时任务预热到 S3 `cache/principals.json`,页面默认秒开
- IAM 新增 18 个**只读**动作(`ListRoles`/`ListUsers`/`GetRole`/`GetUser`/`List*Policies`/`GetPolicy*` 等),中心角色 `template.yaml`(独立 `Sid: IamRead`)、`onboard-account.yaml`、页面 🎲 生成命令三处同步。**不含任何 `iam:Tag*`/写权限** —— 看板只检测并给出命令,打标动作由使用者自己执行

**变更**
- 面板表格加**账号下拉选择**(多账号时一次只渲染选中的那个,不再把所有账号纵向堆叠;选项上直接标注该账号待处理数或"权限不足")、**按打标状态筛选**(全部 / 未打标 / 无效打标 / 已打标)与**分页**(默认每页 10,可切 25/50/100/全部)。三者都只重渲染已取回数据,**不重新扫描 IAM**;切换账号或筛选条件时页码归零,避免停在空页。汇总卡片仍是全账号合计(已在说明里注明)
- 修复长修复命令把「名称」列撑爆、进而把「最后使用 / 标签值」表头挤成竖排的**样式错乱**:命令改为独立整宽子行(不参与列宽竞争),表头强制不换行,名称/权限来源列加宽度上限
- **分账告警不再对已用 IAM principal 打标的用量误报**:此前只认 application inference profile,直连模型 ID / 系统跨区 profile 的用量一律判为"无法分账"并每个窗口推钉钉。现检测到存在已打标的 IAM principal 时**降级为巡检消息**(金额与模型仍如实列出,只是不升级为告警),并提示"资源标签优先级更高,两种方式只应选一种"。判定只读快照缓存,**IAM 扫描慢或失败绝不影响告警链路**
- 告警文案给出**两种**分账方案(① IAM principal 打标(推荐,无需改代码) ② application inference profile),此前只提 ②
- 用量表「类型」列的悬停提示改为"不可按**资源**标签分账;若调用方 Role/User 已打标仍可分账",避免把 `taggable=false` 误读成"这笔钱一定归集不了"
- IAM 扫描使用**专用客户端配置**(`adaptive` 重试模式 / 最多 8 次),而非通用的 `FAST`(2 次 legacy)。IAM 是全局单端点且限流严格,并发扫描下实测会 `Rate exceeded` 导致漏评估 principal
- **残缺扫描不再覆盖完好快照**:未扫全(`truncated` / `notEvaluated`)的结果若遇到仍在 TTL 内的完整快照则放弃写入,并在写入时标记 `partial`。否则 `totals.tagged` 会被写低,告警侧据此判定"无人打标"而**误报**。账号级 `error`(如成员账号缺 IAM 只读权限)属稳定状态不算残缺,否则快照永不更新
- **告警侧区分"确认无人打标"与"没扫到"**:`tagged_principal_state()` 返回 (计数, 是否可信);残缺快照且计数为 0 时**照常告警但在钉钉消息里标注该结论尚未确认**并引导去面板做后台全量重扫 —— 既不静默压制真问题,也不让人照着可能错误的结论行动。计数 >0 即便快照残缺也可信(存在性下界成立)
- **「⚡ 全量重扫(后台)」改为异步**:同步请求经 CloudFront 只有 60s,大账号(实测 519 角色)注定扫不全、也就无法回写快照。改为点击后异步 invoke 自身跑完整扫描(可用完整 Lambda 预算),前端每 4s 轮询 `format=principals_job`,完成自动刷新;进行中重复点击会被去重,其他页面打开面板时自动接管轮询。任务状态存 S3 `cache/principals-job.json`,`running` 超 15 分钟视为僵死并自动解锁(否则一次意外失败会把按钮永久禁用)
- **同步扫描结果也回写快照**:否则刚打完标、页面已显示"已打标 N",告警链路仍读旧快照里的 0,最长一个定时周期(默认 6h)内两边结论不一致。回写同样受残缺保护
- `ce_cost` 内的标签值校验闭包提为模块级 `tag_mis_reason()`,与新面板共用同一套「首尾多了空格 / 大小写不一致 / 值不匹配」判定,避免两处分叉

**已知局限(面板内已注明)**
- "有 Bedrock 调用权限"基于**静态解析**内联 + 托管策略(用户额外看所属组),**不求解 Deny 语句 / 权限边界 / SCP / Condition**;仅靠 `Action:"*"` 命中的标注为**宽泛授权**需人工确认。宁可多列也不漏
- AWS 服务关联角色(`/aws-service-role/`)不可打标,故跳过;单账号默认上限 1500 角色 / 500 用户,触顶或超时会显式标注**未扫全**并如实报告已扫数量,不静默丢数
- 未评估的 principal(时间到、或限流/权限导致读取失败)会计入 `notEvaluated` 并在面板「未扫全」标记上显示个数,**其打标状态按未知处理而非当作已打标**,归因(达上限 / 时间到 / 读取失败)在 note 里分开说明
- 页面实时扫描受 CloudFront 源站超时(60s)约束,预算压在 45s 内、到点返回部分结果而非 504;EventBridge 预热快照用完整 Lambda 预算,故快照通常全量、实时为尽力而为
- IAM 标签键**区分大小写**,写成 `Map-Migrated` 的 MAP 不认,已单独识别为无效打标并说明原因

**⚠️ 升级需手工介入一处**(不做仅该面板不可用,不阻塞):成员账号的 `BedrockUsageReader` 角色需补 IAM 只读权限,否则该账号在新面板显示"权限不足"(用量与账单统计不受影响)。详见 [docs/UPGRADE-1.8.0.md](docs/UPGRADE-1.8.0.md)

## 1.7.0 (2026-07-31)

**新增**
- **GPT-5.6(Sol / Terra / Luna)用量纳入统计**:这类模型走 Responses API(`bedrock-mantle` 端点),指标在 `AWS/BedrockMantle` 命名空间、维度是 `Model` 而非 `ModelId`,此前**完全统计不到**(页面不报错,就是没有这几行,容易误判为"没人用")。现与 `AWS/Bedrock` 并发查询后合并,类型列显示 **Responses API (mantle)**
- **按 Bedrock Project 拆分**:mantle 端点用 Project 做成本归集,模型行下展开 project 子行(名字 / id / token / 成本)。project id → 名字来自 `bedrock-mantle` 控制面,权限不足时降级显示 id;project 分项之和与模型总量对账,差额显式归入 **(未归集)**,不静默丢数
- 内置 GPT-5.6 三档单价(2026-07-30 调价后:Terra ↓20%、Luna ↓80%、Sol 未变;三个可用区同价)
- IAM 新增 `bedrock-mantle:ListProjects` + `bedrock-mantle:ListTagsForResource`(中心角色 template.yaml、`onboard-account.yaml`、页面 🎲 生成命令三处同步)。⚠️ 两条必须一起给:`ListProjects` 连带返回 tags,只给前者整个请求直接 401

**变更**
- mantle 行不再参与分账告警:该端点本就不支持成本分配标签,改用 `build_data` 输出的 `taggable` 判定,避免 GPT-5.6 因"不可分账"被每个窗口反复推送钉钉
- mantle 行的缓存列显示 `—` 而非 `0`:该命名空间没有 cache token 指标,`0` 会被误读成"没命中缓存"
- mantle 模型跳过 inference profile 反查(其不存在 profile),省掉无谓 API 调用

**修复**
- 真实账单(CE)金额恒为 0:服务名按 `Amazon Bedrock Service` 精确匹配,但 Marketplace 计费实际上报 `Amazon Bedrock` 或 `Claude Sonnet 5 (Amazon Bedrock Edition)` 等,导致所有账单行被过滤光。改为匹配含 `amazon bedrock` 的服务名;并在匹配为空时打印 CE 返回的服务名清单便于定位

**⚠️ 升级需手工介入两处**(不做仅功能降级,不阻塞):成员账号补 IAM 权限、单价表补 GPT-5.6 三条(单价 secret 会整表覆盖代码内置值,**新装同样需要**)。详见 [docs/UPGRADE-1.7.0.md](docs/UPGRADE-1.7.0.md)

## 1.6.0 (2026-07-26)

**新增**
- 真实账单(CE)面板支持设定 **map-migrated 标签值**:MAP 场景该标签值只有一个指定值,但客户打标时常手滑多敲空格——标签"打上了"却不生效。设定后:
  - 只有值**完全一致**才计入"已打标"与打标占比
  - 值不符的金额单独列为 **⚠️ 无效打标**(总卡片 + 每账号一列),并列出具体错误值(空格以可见黄点显示)与原因(首尾多了空格/大小写不一致/值不匹配),方便直接去资源上改标
  - 留空则维持原行为(任何非空值都算已打标)
- 设置保存在中心账号 S3 缓存桶 `cache/settings.json`,无需改基础设施;新增 API `?format=settings` / `?action=save_settings`

## 1.5.0 (2026-07-15)

**变更**
- 布局重排:查询条件工具栏(账号/区域/日期/快捷范围)置顶 → 💰 真实账单(默认展开)→ 用量 & 成本估算。打开页面自动按最近 7 天查询并展示真实费用
- 快捷范围(7/30/90天)同时联动刷新账单与估算;「查询费用」按钮改为「刷新费用」
- 移除「数量单位」切换,固定显示原始 token(页脚保留 ÷1000 = 账单千 token 的对账口径说明)
- ⚠️ 打开页面/切换快捷范围即触发 CE 查询(每账号 $0.02 API 费用)

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
