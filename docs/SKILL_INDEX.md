# Skill 与实际调用索引

更新日期：2026-10-03。本文说明应用实际接线，不把文件存在、注册可用或计划审核通过称为能力已执行。版本唯一维护在 [registry.json](../app/skills/registry.json)，具体请求以保存的来源记录和哈希为准。

## 谁决定调用

由 Python 工作流选择阶段，[runtime_skills.py](../app/runtime_skills.py) 按注册表加载该阶段允许的文件，[Gateway.decide](../app/providers/gateway.py) 将内容放入同一次模型请求的上下文。模型在入口可提出 `reply / clarify / directions / plan` 等允许的决定，不能自行读取任意 Skill、增加审核轮次或提交媒体。

入口的意图、澄清、反馈解释和商品理解写在策略主提示里，与声音方向同一次使用。它不是额外运行的主 Agent，也不负责调用其他模型。修改这些文字不会改变程序的确认、预算和状态机。

## 四层职责

| 层 | 主要维护内容 | 实际位置 |
| --- | --- | --- |
| 系统提示 | 本次角色、输入数据边界、Schema/字段协议、允许修改范围与输出约束 | `planning.py`、`ad_script.py`、`direction_review.py`、`progressive_planning.py` 的当前阶段 system 常量 |
| Skill 主文件及阶段参考 | 创作步骤、表达方法、声音和画面判断、当前阶段交接 | `app/skills/ads-video-*/SKILL.md` 及其 `references/` |
| 共用参考 | 多阶段需要保持一致的商品事实边界、广告表达定义、分类脚本方法，以及按类型注入的一份 playbook | 下表 `shared/` 文件；playbook 只加载与当前类型匹配的一份 |
| Python | 阶段调度、状态、确认、版本、确定性校验、预算、回执与付费去重 | `gateway.py`、`pipeline.py`、`db.py`、`progressive_planning.py`、`planner_runtime.py` 等 |

同一条广告方法确定主要维护位置。生成和审核分别说明如何使用它；字段权限可以在 system 中简短重申，程序仍执行强制校验。旧适配器的冻结提示保留兼容用途，不作为当前规则的维护位置。

商家来源编号属于程序接口：`evidence_references.py` 在新策略请求中给已过滤的原文来源分配短编号，收到后按精确表恢复真实ID；`validate_brief` 继续检查逐字摘录、否定限定和原图身份，并一次汇总可确定的事实引用错误。该处理不新增 Skill、创作规则或模型阶段，也不把历史AI内容升格为商家事实。

| 共用参考 | 主要负责 | 自动注入阶段 |
| --- | --- | --- |
| [product-evidence.md](../app/skills/shared/product-evidence.md) | 商品主张、图片观察与创作假设的依据边界；本轮广告必需内容与可选资料的区别及逐候选覆盖 | `strategy`、`direction_review` |
| [ad-form-contract.md](../app/skills/shared/ad-form-contract.md) | 三种主要表达方式及创作意图/影片概念的共同约定 | `strategy`、`direction_review` |
| [script-expression.md](../app/skills/shared/script-expression.md) | 沿已选类型展开脚本；画面与声音分工、有效同述与机械动作播报、角色及局部出镜的表达方法。不编价格只写在这里 | `ad_script`、`storyboard`、`plan_refinement`、`plan_review` |
| [ad-form-playbooks/index.md](../app/skills/shared/ad-form-playbooks/index.md) | 推荐类型时的三种骨架短索引 | 仅 `ad_form=auto` 的 `strategy`、`direction_review` |
| [brand-tvc.md](../app/skills/shared/ad-form-playbooks/brand-tvc.md) | 产品展示的开场、节拍、短旁白、镜头和英雄定格。产品片大约 15 秒 2–3 句只写在这里 | 该类型的策略、写稿、分镜、审核 |
| [scene-story.md](../app/skills/shared/ad-form-playbooks/scene-story.md) | 剧情／场景的处境、一个包袱、短对白和定格 | 该类型的策略到综合审核 |
| [presenter-pitch.md](../app/skills/shared/ad-form-playbooks/presenter-pitch.md) | 人物讲解的直视开场、讲加演示和更高台词密度 | 该类型的策略到综合审核 |

`shared/` 是应用共用参考，不是独立可调用 Skill。其内容在 `stage_skills.shared_references` 中传入模型，完整版本和哈希由 `shared_reference_trace` 单独追溯并进入请求签名，不额外产生模型请求。`script-expression` 进入展开与审核阶段，不注入入口、方向审核或旧分拆审核。类型 playbook 按 `ad_form` 只注入一份：明确类型用全文；`auto` 在方向选定前只用短索引，选定 `film_concept.structure` 后的写稿、分镜和审核改注入对应全文。未带类型的历史调用不注入 playbook。

## 当前阶段映射

| 程序阶段 | 实际模型指令 | 说明 |
| --- | --- | --- |
| 入口 `strategy` | 主提示里的入口判断与商品理解 + Sound Direction（含原创意策略、`film-concept.md`、`direction-comparison.md`）；商品依据与广告形式合同。类型未定时加短索引 | 一次请求，可回复/澄清，或先写六个短概念再展开三个有效方向；程序只记录贪心选择 |
| `direction_review` | Plan Review 主文件 + `direction-review.md`；两份共用参考 | 一次独立审核，只修没有依据的主张，不改写创意身份 |
| `ad_script` | Script Planning（含原台词的时长、声画和示例）+ `script-expression` | 同一次请求完成内容稿和台词 |
| `line_rewrite`（只在台词检查命中时发生，传输阶段仍是 `ad_script`） | Script Planning 主文件与台词示例 | 只重写列出的句子；不加载 `timed-script.md`、时长参考、声画参考或类型 playbook |
| `storyboard` | Storyboard Direction + `coordinated-storyboard.md`，以及 Script Planning 主文件与台词示例；`script-expression` 一次 | 可直接联合起稿，或协调既有内容稿；时轴由程序派生 |
| `plan_refinement` | Storyboard Direction + `coordinated-storyboard.md`；`script-expression` | 仅按实际失败稿与程序范围修正 |
| `shot_craft_rewrite` | Storyboard Direction + `shot-literal.md` | 只改点名镜头的字面写法 |
| `plan_review` | Plan Review + `plan-review.md`；`script-expression` | 独立审查程序排时后的完整最终方案，只审不改。没看没听就不能声称口型已经验证，只写在这里 |

开启联合起稿时，方向选择后为文案与分镜同稿 → 程序排时 → 综合审核，正常两次模型请求；关闭开关保留内容稿 → 分镜协调 → 综合审核。正常新方向仍是生成与方向审核两次请求；没有因为拆参考增加调用。有限返修、窄台词收尾和审核记录恢复的次数由程序约束，具体运行结果见 [VALIDATION.md](VALIDATION.md)，不在各 Skill 重复维护预算数字。

`content_draft`、`speech_draft`、`legacy_storyboard`、`script_review`、`alignment_review` 已从加载表删除，对应附录也不再保留。默认关闭的内容初稿、旁白初稿，以及旧适配器的分镜和分拆审核如果仍被调用，会改用上表现有阶段的资源：初稿用 `ad_script`，旧分镜用 `storyboard`，旧审核用 `plan_review`。直接按这些旧名字取 Skill 会失败。Reference Research 与 Video Review 仍不进入默认流程。

## 编译模板与尚未接入能力

| 资源 | 接入方式 | 能说明什么 |
| --- | --- | --- |
| Asset Prompts | `compiler_template()` 读取场景/角色模板，`design.py` 填入实际信息 | 程序编译图片提示词，未让文字模型执行整篇 Skill |
| Video Prompts | `video_prompt.py` 读取视频模板，提交前冻结实际请求 | 程序编译视频提示词；旧冻结请求继续使用原版本 |
| Reference Research | `pending` | 只有方法文件，未接产品检索工具 |
| Video Review | `pending` | 只有方法文件，未接实际音视频语义质检 |

模型阶段加载器拒绝 compiler 类型；模板只能通过编译接口使用。模板记录的 `loaded_sections`、`consumed_resources` 和 `content_sha256` 对应实际使用的那个模板，完整 Skill 来源另记 `source_content_sha256`。主 SKILL.md 留作编译职责说明，不会发送给媒体模型。执行详情将其标为“提示词模板”。

`registry_metadata()`（健康接口所用）标明 `evidence_kind=registration`、`mode`、`availability` 和来源哈希。`active / available` 表示已接线可使用，`pending` 表示待接入；这些都不是某任务已调用或已成功的证据。模型加载、共用参考和模板编译各有真实请求追溯，历史记录不补造新字段或当前版本。

## 旧体系兼容

旧 [`ad_expression`](../app/legacy_skills/ad_expression/SKILL.md) 原样移到 `app/legacy_skills/ad_expression/`，保留 manifest、历史 ID `ad_expression`、版本及内容哈希。它是旧应用方法包，不属于标准 Skill 注册目录，不能将其缺少 frontmatter 视作当前 Skill 格式。

只有未使用注册阶段体系的旧适配器读取其正文。当前流程保留 `historical_schema_version` 元数据兼容旧脚本字段，已解除对旧正文的无用读取。旧存档、方向、方案、图片、视频和冻结请求不因此重写。

## 维护与验收

修改方法时先确认该阶段实际注入的文件，再更新资源所属版本。共享修改更新共享参考版本；输出字段、权限和程序行为有变化时同时检查代码。检查注册名称/frontmatter、资源存在和路径边界、阶段隔离、实际发送内容与追溯哈希，不只检查 Markdown 外观。

程序回归使用无生产配置的干净导出和替身供应商；检查新流程不读取旧正文、共享规则实际随请求进入且只出现一次、编译模板不混入模型 Skill、选择与确认/版本保护仍生效。离线通过不能证明真实模型生成质量；当前已验收方向作为对照保留，不因整理规则重新生成或覆盖。

第一、二阶段已由用户暂时接受，第三阶段已由用户暂时通过，文案自然度后置；第四阶段收紧审核证据、返修交接与失败分类。前一轮仅整理 Skill 格式与调用关系；本轮进一步接入 `script-expression`，不增加模型阶段，也不改成主 Agent 动态挑选 Skill。

人物声音延续的执行权限由 Python 合同维护：`production.speech_delivery` 缺省或为 `visible_only` 时仍要求说话全程可见；只有显式 `on_and_off_screen` 才允许已确认的同一 `speaker_id` 延续到商品或局部画面。综合审核逐句检查所有相交镜头的 `shot_visibility`，整片至少有一处同一人物可见开口，未知可见性不得通过。`appearance=partial_only` 明确只露手、头发或身体局部，仍是角色，不能充当 `character_dialogue` 的唯一说话者；旧记录不回填这两个字段。

谁说话、画外和内心独白只在 Sound Direction 决定。脚本、分镜和审核沿用已选形式。角色参考的横版版式只在图片提示词；视频提示词仍能认出旧的 2×2 四格。没看没听就不能声称口型已经验证，只写在 Plan Review。第三阶段的工程检查与真实文本检查分开记录，见 [STAGE3_ACCEPTANCE.md](STAGE3_ACCEPTANCE.md) 和 [VALIDATION.md](VALIDATION.md)，不能把接线完成标成三类内容或成片全部通过。

## 来源与改编

下表先列用户提供的本地 `tvc-ai-director-main` 文档快照；本轮新增的 `tvc-director-main` 为另一来源，单独记录在后表。远端 URL 和 commit 未核验，不据文件夹名称推测。仅改编所列方法，不复制整套专业制作流程、不引入其 Python 导出脚本或第三方素材。完整 MIT 许可见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

“每镜必须新增信息”不是来源的普遍原文要求，本套规范也不将它强制化。中文台词示例、跨镜语义契约、三方向选择及本产品确认流程由映作按需求整理；TVC 是方法参考，不是广告效果证据。

| 本地来源相对路径 | SHA-256 |
| --- | --- |
| `LICENSE` | `e557607fdf95b9592a88a08660287d0ad4f3e1d8ee6020a1fb5962a4a5bd8097` |
| `tvc-ai-director/SKILL.md` | `2113b93c1c84b23c960e543ffd54a19dc2ca6e74275b6ec5ab3720d5c8df6c30` |
| `tvc-ai-director/references/category-playbooks.md` | `8b2ff93b1f7c826d73099737bfed229643d37a8aa5af5925f28f35bb5571cc52` |
| `tvc-ai-director/references/prompt-style-guide.md` | `b4fd33000d08ac60e347693e2ec1eed2eb5773c1f390a778d207242b6fbb5e30` |
| `tvc-ai-director/references/tvc-quality-bar.md` | `73884723e71e92916cac3522236b6c488e6a962073ff5497e08b3e25dbf58879` |
| `tvc-ai-production-coordinator/SKILL.md` | `9a0356a7bc2474c4b55e80643a4fdb54ca6123d89880ab304b7d7449dba931d1` |
| `tvc-ai-shot-prompter/references/image-prompt-method.md` | `7f717ba7ffdc15f12d20a04df44c740e828c94f3d010543f2b427d762322adb4` |
| `tvc-ai-shot-prompter/references/video-prompt-method.md` | `80d0baeec4b6a66355096e7aabacf1399d0e0c029677d34c027b1cdcc6d8605f` |
| `tvc-brief-strategist/SKILL.md` | `031c6bedb75d40e9956a13eb970b8e501c2abb14b290ce3b2ee0ca38ef76d031` |
| `tvc-client-review-interpreter/SKILL.md` | `162b8a2955bbf33e9b0a2db9c988003e54a84138895be884c482bd7405da1e89` |
| `tvc-concept-developer/SKILL.md` | `c15c7e335e23a9600433601021ee99a1d7a5c8269c0fafff02a8975e8ec33e36` |
| `tvc-concept-developer/references/proposal-logic.md` | `5164e09e50e57c1dd9c681503f20eeb7e755c914d25f29f17f05428d7f233d06` |
| `tvc-director-treatment/SKILL.md` | `825eb1c48a8383e055cf596a448e1d2f267899c16c546784edb8f5525b4c3738` |
| `tvc-director-treatment/references/visual-development.md` | `3dae76263c8b73c334f8cb9bafb0ab3719b3ccd6d8078e99c3caf34b22a71db9` |
| `tvc-editing-director/SKILL.md` | `67ed37618a07cf5d2e822a9f0cf6d47268235dd3164fff34759b08ea11bf9fbd` |
| `tvc-storyboard-writer/SKILL.md` | `ecaee13dbfb58ea4e99edcedc75d9b62dd9db02c7b1ad02ca0622f2ac466a0d9` |

`tvc-director-main`（Copyright (c) 2026 Ethan Wang）用于第二阶段产品视觉方法：产品世界、使用或品牌语境的相互关系，以及产品视觉、两种世界交替、生活方式三类结构，重新编写为映作的方向合同。来源的内部拆解、功能图示、固定幕次和视觉隐喻均受映作事实约束，未把它们视为商品证据；不引入九宫格流程、额外资产确认、来源所述模型能力或完整分镜模板。来源 `treatment.md` 提供的是方法文档，不是已验证模型效果。

| `tvc-director-main` 本地来源相对路径 | SHA-256 |
| --- | --- |
| `LICENSE` | `d5ddbca6d673d5f02b30ce0e576db65c36d46ef9f2f7bd78f84558452055a068` |
| `references/treatment.md` | `9692d6efab17b8311d870a9811211c24c218a98f187a268a6d1057fd80ec6cd3` |
| 用户提供的摘录汇编 `tvc-director-examples.md`（含说明与逐字摘录，不是上游单文件） | `c970a937bc60e4c0a2e6d88c168062cddc1eb04ca0b65884d99d476eaa8943c1` |

摘录用于类型 playbook 的时长模板、出镜比例、镜头词汇和阶段结构，并从 16:9/Seedance 改写为 9:16/wan3.0。摘录没有文案例子，playbook 中的台词为映作原创。分镜与视频提示词另改编了七要素、字面画面、分类型出镜、视频内英雄定格和同一次请求内的多阶段运动；不引入九宫格，不多生成图，不让模型画 Logo。

第四阶段 Plan Review 1.11.1 只为新审核的 `supported_by_cut` 增加省略动作、实际切点与前后画面原文证据。代码核验位置和引用来源；模型仍需独立判断动作语义。无省略不增加必填，旧绑定审核保持可读。错误分类、次数及当前容量由 Python 维护，不写成重复的 Skill 业务规则。
