# LingTai 名称同步与编辑边界验收

截至 2026-09-12；维护者 @peter-stei-5525-5d5ebaa5，服务端协作者 @boris-cher-7418-1578233d。

## 用户确认的要求

- 灵台修改 NAME 后，Puffo 自动反映；描述可以不显示。
- Puffo 不提供会被下一次来源同步覆盖的改名入口。这是必需关闭项。
- 保持来源权威和单向性：无名仍为 null，“未命名”只用于显示。

## 实施与证据清单

| 项目 | 当前证据 | 状态 |
| --- | --- | --- |
| 来源读取 | 共用 read_source_profile；.agent.json 优先，仅不存在时回退 init.json；运行期只跟随当前.agent.json，不发布任何bootstrap回退值；缺失文件/字段、损坏均保留，仅显式null可清 | 已有解析测试及新增同步测试通过 |
| 自动同步 | daemon 独立任务每轮完成后等待30秒，只向自己身份 PATCH display_name；成功才缓存 | 745dc0f7；全量pytest通过，2项环境跳过 |
| 失败与重试 | 10秒/Agent超时，失败记daemon日志，下轮重试；不写源文件 | 同步测试验证网络失败重试、损坏保留、成功去重 |
| 正常改名实际传播 | 本地运行daemon；修改fixture名后约21.5秒网页自动显示新名 | name-sync-rename.png / name-sync-live.json；随后owner与non-owner聊天页14.2秒同步；两边已打开完整资料页5.5秒同步 |
| 有名→null→有名 | server三态PATCH支持显式null；daemon和Web回归通过 | 通过：两账号真实清空12.9秒、重新命名30.0秒；两边原始缓存均明确null，源文件未被同步器改写 |
| 启动/预热旧快照 | LingTai不走旧sync_full_profile，防止名字被导入快照覆盖 | 新同步测试通过 |
| Web资料页编辑入口 | managedProfile关闭profileWritable及runtime编辑；提交适配器再次拒绝来源字段修改 | 真实owner资料页无改名/编辑资料按钮，提示在灵台修改；name-sync-profile-owner.png/.txt |
| Web名称清空 | server明确null时不能回退旧portal名；资料页使用实时profile投影 | 新回归先红后绿；资料页双账号正常改名通过，null清空/恢复已通过真实双账号验证 |
| Control edit_agent | guarded_edit_params在任何写入前拒绝来源字段变更 | 既有实现和测试 |
| CLI rename/profile | 审计发现原先绕过guard；现已补拒绝 | 同步测试含CLI拒绝与配置未改断言，已通过 |
| Qt桌面编辑 | 审计发现原先可改名并直接写配置；现已设来源字段只读并在保存入口拒绝 | Qt真实控件测试验证只读、保存按钮禁用、强行调用handler仍拒绝，21项通过 |
| 服务端签名PATCH | 自动来源发布需要Agent签名写接口；不能把同一签名入口整体禁掉 | 显式null协议由Boris负责；不将本机文件手改/直接持钥API视为普通产品入口 |
| 说明文案 | 删除“永远停留导入快照”的说明，改为连接时同步名称 | 工作区已改；随新增编辑边界更新release |

描述、提示词、模型、capabilities和工作区内容均不进入名称PATCH。对本地fixture的名称写入是测试操作；同步器自身只读源文件。尚未合并或发布。

## 原创建入口复验触发条件

- AddMemberToChannelModal若新增scope=space生产挂载，需实测该创建入口。
- Mobile Cloud若移除max-md:hidden，需补真实手机创建入口测试。

这两处在先前17/17可达入口验收中已标为不可达，不计作真实点击通过。

## 公开范围与运行期来源边界

- CLI/Qt将role、soul及运行时编辑一并保持只读是有意沿用灵台管理边界；不能通过换运行时或本地persona编辑绕开名称管理。
- 来源当前文件丢失时，即使init.json有旧的非空名字也不在运行期回退发布，避免旧名覆盖最新自命名值；导入发现仍保留原回退兼容规则。
- nickname是否作为另一个显示值尚未决定，本轮没有发布nickname。描述没有已确认的公开权威字段，因此不读取提示词来生成描述。

## LingTai 权威字段与已知上游行为

以下来源为 @lingtai-de-3574-d881bf55 对 lingtai-kernel origin/main 的源码核查（2026-09-12，消息199035）；本轮没有额外进行灵台自命名重启实验。

- agent_name 为本轮唯一名称来源。identity.py:29 的 _set_name 只允许从无名设置一次，没有清空操作；change_name.py:144 的改名技能会保持 init.json 与 .agent.json 一致，可跨重启保存。
- nickname 可变可清（identity.py:42），但它的显示优先级尚未决定，本轮没有同步它。
- .agent.json 没有自由文本 description（identity.py:83）。profile/soul是运行时指令及工作区内容，不应被生成或复制成公开描述。
- 运行期 system(name_set) 只写 .agent.json，不改 init.json。CLI与ACP构造时从init.json取名（cli.py:138、166、182）；恢复当前manifest主要用于molt_count等状态（228–234），构造后按内存name重写manifest（base_agent:703）。因此仅运行期自命名的Agent可能重启后再次变成显式null。
- 上述“当前manifest存在且明确null”是权威值变化，Puffo会同步清空；这与文件丢失不能证明清空不同。是否让运行期自命名同时持久化init.json归上游产品决定，本轮不特判或伪造保留名。

## 最终集成结果

- Web 9b217098；daemon e48b168b；server 18d3538（Boris e398d2a8的同内容cherry-pick）。
- Server补丁独立验证：profiles39、v2 identities27、WS107；组合18d3538完整lib1429通过/0失败/3忽略（Boris seq199053复核）。组合Docker镜像构建并在隔离本地栈运行成功。
- 两个真实账号（owner / non-owner）打开聊天和资料页，未刷新即收到更新。清空时两边原始profile缓存均为null，分别显示“未命名”/“Unnamed”；恢复时两边收到同一个真实名称。
- 本轮source mutation由验收脚本执行，每阶段比较写入后的完整.agent.json与同步后字节，均未被同步器改写；随后恢复原来的测试名称。
- 证据位于workspace artifacts/lingtai-import/restoration/name-sync-*.json/.png。此文档只记录已执行证据，先前e24a97a0验收包的导入与工具调用证据继续适用，名称同步由本轮补充。

上游待定项由 LingTai 协作者登记于 [lingtai-kernel#1709](https://github.com/Lingtai-AI/lingtai-kernel/issues/1709)，本轮没有改变灵台自命名的持久化策略。

CI检查：daemon e48b168b 的pre-commit与Python3.11/3.12均通过；Web CI出现3项失败，server剩一项检查在运行；不能称为三仓全部CI通过。

部署顺序约束（Boris seq199053源码复核）：先部署Server #370，再部署daemon #349。旧Server会忽略display_name:null但仍返回成功，新daemon因此缓存已发布，无法保证清空生效。当前没有自动兼容协商替代此顺序。Web #955 的 CI 发现3项失败：Boris确认2项billing在基线上同样失败；新增重试测试存在等待失败态不足的问题，已补等待失败态，CreateAgentModal整文件21项通过；推送后继续检查CI。
