# 策划规则与事实边界

第一版 `app/strategy.py` / `app/strategy_rules.json` 是内部策划模块，不执行广告检索、案例下载、模型调用或视频生成。它把可追溯商品事实、未确认事项、创意假设和创意方向分开保存。商家仍使用现有对话与图片确认流程，不新增营销报告或确认步骤。

## 规则来源

以下来源按研究时核验的 commit 固定版本。中文规则、六类划分和校验代码为本项目重新编写，没有复制完整 SKILL、案例脚本、媒体或第三方执行代码，也没有把第三方仓库作为运行时依赖。

| 来源与版本 | 本轮借鉴的内容 | 明确不采用的推论 |
| --- | --- | --- |
| [Corey Haines / marketingskills](https://github.com/coreyhaines31/marketingskills/blob/5b2c0007766c6a1cf1d53fd8fc73e979e0821022/skills/ad-creative/references/creative-roadmap.md)，`5b2c0007766c6a1cf1d53fd8fc73e979e0821022` | 概念的受众、动机、表达角度与来源；开场需要后续兑现；事实与猜测分开 | 持续投放、浏览量或经验规则不能证明转化效果 |
| [Motion / creative-strategy-skills](https://github.com/motion-team/creative-strategy-skills/blob/8e467a3547dcd2118e4d08fa58099553677d52ff/creative-strategy-engine/SKILL.md)，`8e467a3547dcd2118e4d08fa58099553677d52ff` | 先确定受众与情境，再确定角度与表达机制；方向变化不只更换运镜 | 文档中“为什么有效”是方法解释，不是经过验证的因果规律 |
| [Creatify-AI / video-ad-reverse-engineer](https://github.com/Creatify-AI/video-ad-reverse-engineer/blob/d5704e992393546da73bb173dab39867f30b28f6/references/output-template.md)，`d5704e992393546da73bb173dab39867f30b28f6` | 镜头目的、声音与展开/收束结构，区分观察与推断 | 有拆解模板不等于已经观看或分析过某条爆款广告 |

同时参考了 Corey 的 [hook-system](https://github.com/coreyhaines31/marketingskills/blob/5b2c0007766c6a1cf1d53fd8fc73e979e0821022/skills/ad-creative/references/hook-system.md) 与 [edit-anatomy](https://github.com/coreyhaines31/marketingskills/blob/5b2c0007766c6a1cf1d53fd8fc73e979e0821022/skills/video/references/edit-anatomy.md)，以及 Motion 的 [creative-mechanics](https://github.com/motion-team/creative-strategy-skills/blob/8e467a3547dcd2118e4d08fa58099553677d52ff/creative-mechanics/SKILL.md)。上述资料都可能讨论字幕、CTA 或真人表演；本产品沿用用户已确定的无强制字幕、无独立尾字、不依赖真人口型路线，不照搬这些制作要求。

三组来源均为 MIT。核验版本的许可原文分别为 [Corey MIT](https://github.com/coreyhaines31/marketingskills/blob/5b2c0007766c6a1cf1d53fd8fc73e979e0821022/LICENSE)、[Motion MIT](https://github.com/motion-team/creative-strategy-skills/blob/8e467a3547dcd2118e4d08fa58099553677d52ff/LICENSE)、[Creatify MIT](https://github.com/Creatify-AI/video-ad-reverse-engineer/blob/d5704e992393546da73bb173dab39867f30b28f6/LICENSE)。若以后引入其代码或实质性文本，应同时保留对应版权和许可声明；公开资料的许可不等于其中引用的广告、图片、音乐或人物素材已获授权，也不替代本项目许可证。

## 数据契约

所有公共模型 `extra=forbid`，没有效果评分、预测转化率或“已验证爆款”字段。

- `BriefFact` 保存 `id / text / source_kind / source_id`。`merchant` 文本必须是指定真实用户消息的原文子串，且不能摘掉所在原句的显式否定或不确定限定；`image` 必须引用本轮真实商品原图 ID。事实最多 12 条。
- `ProductBrief` 保存商品名、`food / appliance / general` 品类、受众、情境、事实、未知项与假设。商品名、受众和情境是策划标签，不因进入这些字段就成为经过验证的商品卖点。
- `CreativeDirection` 保存一个规则对应的受众、场景、角度、开场、展开、收束、事实 ID、采用原因和 `production_notes: string[]`。制作备注为 1–8 条，事实引用为 1–12 条。
- `StrategyRecord` 保存 brief、1–3 个方向、选中方向 ID 和实际使用的规则版本/来源。每个方向必须使用不同的 `rule_id`。规则记录是方案自描述快照，旧版本不因规则库升级而无法读取。

`unknowns`、`assumptions` 和旧计划未带来源的 `claims` 不能充当证据。`brief_from_legacy({name, claims})` 只把旧名称保留为标签，将旧说法放入 `unknowns`；要重新作为事实使用，需要从本轮真实消息或原图重新建立引用。这是读取帮助函数，不会自行迁移或修改数据库。

## 调用顺序

```python
brief = validate_brief(
    ProductBrief.model_validate(candidate_brief),
    evidence_sources=actual_user_messages,  # source_id -> 原始用户文本
    asset_ids=original_product_asset_ids,  # 本轮原图，不包含生成图
)
rules = select_rules(brief)
record = build_strategy(brief, candidate_directions, selected_id, rules)
```

调用方必须提供冻结上下文里的真实用户消息和原图 ID；不能把模型回复、旧的无来源 claims 或生成图伪装成它们。`build_strategy` 没有数据库访问权，无法重新检验消息来源，因此不可省略前面的 `validate_brief`。

`select_rules(None)` 返回六条紧凑规则供第一阶段了解可选范围。给定 brief 后，食品默认取感官细节、生活情境和外观揭示三条；电器取外观揭示、生活情境、操作讲解和问题与解决四条；通用品取外观揭示、生活情境和操作讲解三条。商家原话有明确比较对象与维度时，追加比较规则或替换第四条规则。检索并不等于已经满足前提，有条件规则仍在构建方向时验证。

返回规则包含 `id / version / sources / categories / label / mechanism / prerequisites / shot_functions / must_not_claim / production_constraints`。传给 `build_strategy` 的规则必须与本地库一致，模型不能改掉前提后再给自己授权。JSON 中 `category_selection` 控制检索，`prerequisites.merchant_patterns_all` 控制保守文本门，`uncertainty_patterns` 排除不确定或否定陈述，`citation_qualifier_patterns` 检查摘录是否省略完整原句的限定词。更新规则时同步升级规则及总版本；已保存旧记录继续按自己的来源读取，新方案只能使用当前库。

## 校验能证明什么

1. **可确定的结构事实**：来源 ID 存在、商家摘录是原文子串、图片 ID 属于提供的原图集合、引用事实存在、规则适用品类、当前规则版本与来源未被更改、选中方向属于候选。摘录同时逐个检查所在完整句，避免从“不能宣称可以杀菌”中只摘出“可以杀菌”；同一短语另有未丢限定的真实出现可以通过。该检查是保守词形筛查，不理解跨句否定、指代、讽刺或相互矛盾的陈述。
2. **保守的规则前提**：操作方向须引用带明确操作描述的商家事实；问题与解决须引用明确用途或益处；比较须在同一条商家事实中同时出现比较对象与维度。图片观察、未知项、假设或未被该方向引用的另一条事实均不能放行这些规则。检索不到相应文字就退回外观/情境方向或澄清，不自动补事实。
3. **仍需上游策划与后续验收**：原话不等于客观真实；字符串校验不理解图片，也不证明某条引用支持方向里的每个措辞。前提词形只是保守筛查，可能漏过同义表达或误判复杂句。不同规则 ID 和去标点后的重复文本检查能拦住明显换标签，不能证明方向在语义上足够独立。制作限制进入模型输入，不等于生成画面已经遵守。该模块不检查最终视频的音画对应或广告效果。

比较方向尤其不能用 AI 生成画面冒充测试证据。来源里的数字仅作为商家提供的信息保留，不产生“已独立验证”标记。这里没有案例搜索/RAG，没有效果数据库，也没有向商家承诺素材会成为爆款。

## 验证

从项目根目录仅运行 `python -m pytest -q tests/test_strategy.py`。此文件只导入策略模块，不导入 `app.main` 或配置模块，不连接数据库、不读取 `.env`、不发模型或供应商请求。

## 广告内容层：ad_expression 1.3.0

旧适配器的广告表达兼容层由 `app/ad_script.py` 与项目自写的 `app/legacy_skills/ad_expression/` 提供，版本为1.3.0；当前默认阶段流程只保留其历史 Schema 版本，不读取旧方法正文，实际规则与调用关系见 [SKILL_INDEX.md](SKILL_INDEX.md)。这组文件是应用加载的模型输入，不是给 Codex 安装的技能，不执行第三方 SKILL，也不使用 human-writing。运行时显式加载 `SKILL.md`、`references/director_methods.md` 和 `references/examples.md`，返回实际内容哈希及来源身份；新脚本须记录1.3.0，历史版本按原规则与已保存内容读取，不自动替换旧稿。

运行时规则由 `manifest.json` 固定 ID、版本、资源清单和参考来源，加载器只读取 `SKILL.md` 与 `references/examples.md` 两个应用文件。`script_context()` 将实际规则正文和内容摘要值传入脚本编写、脚本审阅阶段，生产流程与文本评估复用同一输入构建函数。它不是只有来源名称的提示，也不表示程序已经检索外部案例。更新正文或例子时应同步升级模块和 manifest 的版本；已保存脚本的版本是历史记录，新建脚本才要求当前版本。

本轮继续使用上文固定提交中的方法，具体接入关系如下。中文说明与正反例均为项目重新编写，没有复制第三方长段文本或真实广告案例。

| 固定版本参考 | 接入的运行时方法 | 应用边界 |
| --- | --- | --- |
| Corey `creative-roadmap.md` | 让受众、动机、商品依据和表达角度共同形成广告核心；资料充分程度决定表达范围 | 不要求商家先手工整理获胜广告语料，不声称创意经过市场验证 |
| Corey `hook-system.md` | 画面与口播分工；开场引出的期待由后续内容承接 | 不照搬强制字幕、外部语料抓取和投放指标结论 |
| Motion `creative-strategy-engine/SKILL.md` | 商品已知特点与具体受众场景的关系；允许愿望、审美和日常使用情境切入 | 不把“痛点优先”当所有品类的强制模板，不从策略框架推导产品功效 |
| Motion `creative-mechanics/SKILL.md` | 用内容节拍安排信息推进，商品在叙事中承担明确作用 | 具体中文台词、审稿检查和工程时长处理由本项目编写 |

### 内容契约

生产方案依次执行 `strategy → ad_script → script_review → storyboard → alignment_review` 五阶段。前三阶段建立并审查商品表达，分镜把定稿落入实际镜头和旁白时段，末阶段独立检查最终计划；文本评估工具仍只覆盖脚本与内容审稿两阶段。

`AdScript` 保存 `core_message / audience_takeaway / product_role / evidence_ids / beats / skill_version`。它是分镜之前的完整内容计划，不用镜头运动代替广告主张。`AdBeat` 保存 `id / purpose / message / visual_role / narration / evidence_ids`，规定传播作用和音画分工，但不提前固定秒数；连续性分组另用 `boundary_before / continuity_group` 描述动作与内容边界。脚本总依据与节拍实际引用的依据一致，选中方向的商品事实需要在内容中落实。1.3.0延续商品表达与演示并重，补充可视商品记忆与逐镜信息增量：已知特点如何形成关注理由须由实际 `narration` 与 `visual_role` 兑现，不能用 `purpose` 自述补足。换景别或连续特写不自动构成信息推进；允许必要的细节停留，不强制每镜头新增一个卖点。常用、放手边、收柜麻烦等泛用习惯不能直接当成该商品的看点。只有图片时仍可建立具体外观与审美偏好的关系，不虚构功能。

`ScriptReview` 返回修订后的完整脚本、`ready` 与六项简短结论：商品关联、受众关联、事实支撑、音画分工、自然表达、叙事推进。审稿可以重排节拍、改写台词和画面作用；服务端保存的商品事实、明确场景和选中方向不能被审稿替换。六项均通过才能给出 `ready=true`；有无法在现有资料内解决的缺口时必须允许未通过。检查结论是公开的短评，不是内部思考链。

稿件阶段允许暂时超出总台词预算或存在未完成表达，以便审稿完整改写。定稿每个非空节拍保留完整话语，含标点与空白最多29字符，并按顺序容纳于实际生成段预算。取消每秒1.8字符的目标和20秒固定句数、节拍数偏好，暂保留每秒2.9字符向下取整的保守工程硬上限：20秒最多58字符。历史 `legacy_10s` 的10秒段最多29字符，例如20、20、12字符三句无法依序放入两个29字符片段；新 `continuity_v1` 以实际2–30秒分组计算时长和整句可容纳性，30秒内优先一次生成，不继续机械拆成10秒。该预算待真实音频验证，不是语速标准或推荐密度。静音节拍允许存在，当前原声方案仍需要至少一句有效话语。

### 分镜与独立旁白

新方案的 `speech_spans` 将整句旁白与切镜分开保存。一个完整句可覆盖同一内容节拍的一组相邻镜头，但不能跨独立生成请求；分镜不等于请求，当前 `continuity_v1` 的单次请求可为2–30秒。每个 `anchor` 保存原句连续短语、对应时间与镜头ID；全部短语必须准确拼回整句。`visual_evidence` 是编排器的计划说明，不是事实证明。镜头描述须自足保留场景、商品与动作；`segment.prompt` 只作场景图的统一视觉上下文，新视频由实际镜头描述与旁白时段编译。

`AVReview` 检查最终广告表达与每个旁白时段。其 `ready` 必须等于整条表达通过且全部时段通过，检查记录与实际 `(segment_id, span_id)` 严格对应。它独立阅读最终镜头、台词与时段，不采信 `purpose` 或 `visual_evidence` 的自我证明；不重写台词或新增事实，也不作为 `Plan` 的新增持久字段。对话改稿重跑五阶段，联动画面与时间。旧版本脚本及缺少 `speech_spans` 的旧计划继续兼容，不自动把历史内容升级或重审。

### 能力边界

结构校验能检查引用存在、节拍唯一性、字数、整句装入片段的可能性和审稿标记自洽。它无法理解每项引用是否支持每个措辞，不能用关键词断言文案“有销售力”。内容与最终音画模型审核均只针对计划，没有生成音频或视频可供实测；通过不能替代样稿比较、成片检查或真实投放反馈。本轮完整测试与真实对照待记录，历史验收保留原范围。此批不建立人工案例库、不增加用户确认层级、不生成媒体，不把旧项目台词自动改成新脚本。

## 本轮 TVC 文档方法改编（2026-09-30）

来源标识：`tvc-ai-director-local-82e4c9b51ddbc9d0`，类型为用户提供的本地 `tvc-ai-director-main` 文件快照。没有已核验的远端仓库 URL 或 commit；`remote_url` 与 `commit` 明确记录为 `null`。下表路径均相对此快照根目录，不包含用户电脑绝对路径。文件名或目录名不等于 GitHub 来源认证。

### 改编范围

| 本地来源文件 | 本轮借鉴方法 | 映作应用位置 |
| --- | --- | --- |
| `tvc-concept-developer/SKILL.md` | 商品提供创意依据；从商品特点建立可理解的关注理由 | `app/ad_script.py`、`app/legacy_skills/ad_expression/` |
| `tvc-storyboard-writer/SKILL.md` | 每个镜头有作用，镜头推进观看理解并保留商品状态 | `app/legacy_skills/ad_expression/references/director_methods.md`、`app/directing.py`、`app/schemas.py`、`app/planning.py`、`app/av_review.py` |
| `tvc-director-treatment/SKILL.md`、`tvc-director-treatment/references/visual-development.md` | 将风格说明变成具体可执行的美术、光线、商品呈现与视觉约束 | `app/directing.py`、`app/design.py`、`app/planning.py` |
| `tvc-ai-shot-prompter/references/video-prompt-method.md`、`tvc-ai-director/references/prompt-style-guide.md` | 对参考商品、动作、摄影视觉、声音与连续性组织可执行生成说明 | `app/video_prompt.py`、`app/design.py`、`app/directing.py` |
| `tvc-ai-director/references/tvc-quality-bar.md` | 画面服务创意、商品可辨识、切换有理由；区分计划与实际素材验收 | `app/ad_script.py`、`app/legacy_skills/ad_expression/`、`app/av_review.py` |

以上是方法改编，中文运行时规则和工程契约由映作重新编写，没有复制整套技能正文、安装第三方技能或引入整个仓库。`gateway.py` / `pipeline.py` 的阶段接线、校验、持久任务及预算属于本项目工程实现，不是该参考仓库提供的代码。未采用第三方两份 TSV/Excel 导出脚本；本轮也未将 PPM、客户审片解释器或通用多模型路由器列为已接入模块。

明确不照搬原方法中的十项必问、缺故事就强补剧情、强制人物／高潮／尾卡、每个导演视觉章节都生成图片、外部案例检索及传统会议交付包。沿用现有商家输入、原始商品图、已确认场景与先检查图片再生成视频的流程，不增加用户确认层级。

### 本地文件内容校验

| 快照相对路径 | SHA-256 |
| --- | --- |
| `LICENSE` | `e557607fdf95b9592a88a08660287d0ad4f3e1d8ee6020a1fb5962a4a5bd8097` |
| `tvc-concept-developer/SKILL.md` | `c15c7e335e23a9600433601021ee99a1d7a5c8269c0fafff02a8975e8ec33e36` |
| `tvc-director-treatment/SKILL.md` | `825eb1c48a8383e055cf596a448e1d2f267899c16c546784edb8f5525b4c3738` |
| `tvc-director-treatment/references/visual-development.md` | `3dae76263c8b73c334f8cb9bafb0ab3719b3ccd6d8078e99c3caf34b22a71db9` |
| `tvc-storyboard-writer/SKILL.md` | `ecaee13dbfb58ea4e99edcedc75d9b62dd9db02c7b1ad02ca0622f2ac466a0d9` |
| `tvc-ai-shot-prompter/references/video-prompt-method.md` | `80d0baeec4b6a66355096e7aabacf1399d0e0c029677d34c027b1cdcc6d8605f` |
| `tvc-ai-director/references/prompt-style-guide.md` | `b4fd33000d08ac60e347693e2ec1eed2eb5773c1f390a778d207242b6fbb5e30` |
| `tvc-ai-director/references/tvc-quality-bar.md` | `73884723e71e92916cac3522236b6c488e6a962073ff5497e08b3e25dbf58879` |

快照标识取已列文件的相对路径与内容哈希，按路径排序拼接后再计算 SHA-256 的前16位；这不是远端 commit。运行时 manifest 保存同一清单，`load_ad_expression_skill()` 返回 `local_source_snapshots`，模型收到的是可追溯的本地参考身份。运行时只加载项目自己的规则文件，不去上述目录读取或执行任何第三方资源。

该快照附带 MIT 许可；作者为 `Copyright (c) 2026 Guangjun`。完整版权及许可文本保留在 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)，只覆盖本次对应来源的改编部分，不替代映作根许可证。


## 第二阶段影片概念补充（2026-10-02）

当前目录版本为1.2.0，共7条规则；新三选允许复用规则及同一主打特点，旧兼容分支的规则差异约束仍保留。上文六条与固定检索顺序描述属于初版。新增 film_concept 与 creative_intent 是选定概念的可交接数据，不新增一轮模型调用。tvc-director 的 treatment 方法来源与SHA见技能注册表，MIT声明见THIRD_PARTY_NOTICES；未执行第三方工作流。

外观揭示1.2.0允许由光影、形态和运动关系组织产品认识，不强制人物或谜题；特点与选择理由1.1.0允许已知功能的视觉概念及必要声音补充，不把意象当性能证据。新审核可以保留来源地纠正既有图片观察，不能改写商家原文或增加事实。
