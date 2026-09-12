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
| 有名→null→有名 | daemon和Web单测通过；旧服务端不支持显式null | 服务端改动及真实双账号验收待完成 |
| 启动/预热旧快照 | LingTai不走旧sync_full_profile，防止名字被导入快照覆盖 | 新同步测试通过 |
| Web资料页编辑入口 | managedProfile关闭profileWritable及runtime编辑；提交适配器再次拒绝来源字段修改 | 真实owner资料页无改名/编辑资料按钮，提示在灵台修改；name-sync-profile-owner.png/.txt |
| Web名称清空 | server明确null时不能回退旧portal名；资料页使用实时profile投影 | 新回归先红后绿；资料页双账号正常改名通过，null清空待服务端联调 |
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
