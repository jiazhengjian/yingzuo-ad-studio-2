# 放行路径重数

日期：2026-10-07。只读，没有改 `app/` 或 `frontend/`。对照的是当前 `main`（`e3cbe19`），不是审计写稿时的 `2c7d729`。`b9348c1` 之后，综合检查会留下状态：`passed`、`repaired`、`not_ready`、`conservative_fallback`。`not_ready` 不能出图。

审计第 3 节列了 13 条「失败之后仍可能标成可以出图」。按现在的代码重数：**仍能在综合检查没有通过时让图片生成开始的，是 6 类。** 审计里另外几条不再自己把方案送去出图。下面先给这个数，再把每一处调用点写上文件和行号。

「送到确认」指方案被写成当前方案，项目状态变成 `awaiting_generate`。商家看到的是可以确认后再准备图片。「出图开始」指 `pipeline._generate` 通过出图闸门，去准备场景图和角色图。两件事现在不是一回事：`not_ready` 的方案仍会进等待确认，但出图会被拦住。

## 1. 和审计的 13 条怎么对

| 审计里的那条 | 现在 |
| --- | --- |
| 1. 检查没过，只剩状态衔接，拿掉之后照样放行 | 还在。状态写成 `conservative_fallback`，出图闸门放行。 |
| 2. 综合检查没打完，用一份还能用的稿放行；网关对同一种错误再放行一次 | 策划函数里还在。有检查记录但没通过时写成 `not_ready`，出图被拦。没有可绑定的检查时写成 `conservative_fallback`，出图放行。网关那条按 `compiled_plan` 再放行的代码还在，但这个异常上没有这个字段，当前走不到。 |
| 3. 修订作废（`RevisionKept`）时放行更早的稿，或放行当前这份 | 还在。更早的稿若检查没通过，出图被拦；若那份稿没有检查记录，出图放行。 |
| 4. 镜头工艺窄返修失败后记下继续 | 不再自己放行。记一笔之后继续，后面仍会做综合检查。 |
| 5. 背景人物改写没返回可用稿 | 不再自己放行。循环继续，后面仍会做综合检查。 |
| 6. 镜头文字改写编译失败，用改之前的稿继续 | 不再自己放行。丢掉改写，后面仍会做综合检查。 |
| 7. 状态、衣服、四视图、内心独白只记警告 | 不再自己放行。警告记下之后仍去做综合检查。 |
| 8. 末镜时长挪不动就保持，可以带着长末镜放行 | 不是检查旁路。时长挪不动不硬停；方案仍要过后面的放行。硬门名单里本来就没有「末镜必须大约 2 秒」。 |
| 9. 末镜还写着推近，硬门不拦 | 不是检查旁路。`story_ending_issues` 写明残留推近不算失败。 |
| 10. `skip_review` 为真就放行，不做综合检查 | 函数还在，只给离线重排。那个脚本不写数据库，不会把项目变成等待确认。 |
| 11. 检查点里某步已完成就交回旧结果，进度也不再报 | 还在，但不是放行口。已完成且输入没变的阶段直接交回旧结果。 |
| 12. 重试是否丢掉分镜，靠错误文案里的几句中文 | 还在，但不是放行口。文案对不上，就会重放已完成的分镜。 |
| 13. 占位稿被编译并放行 | 主路上已经挡住。`release` 和渐进策划返回处都会拒绝占位稿。半截回复交回的上一份方案不查是不是占位稿。 |

审计那 13 条里，现在还能在检查没通过时让出图开始的，是第 1 条，加上第 2、3 条里「没有通过记录就保守放行」的那一支。第 4 到第 7 条不再自己放行。第 8、9 条不是检查旁路。第 10 条不落库。第 11、12 条仍会跳过重算。第 13 条在主路上被挡住。

当前多出来、审计没有单独编号、但确实能出图的，是下面第 5、6 类：关掉渐进策划时的旧声画路径，以及手工改方案。半截回复交回上一稿，审计写在第 2 条的网关挽救里，这里单独算一类，因为另一条 `compiled_plan` 挽救已经走不到。

## 2. 出图闸门

出图前只看这一处：`app/pipeline.py:852-855` 调用 `app/progressive_planning.py:1214-1225` 的 `images_blocked_reason`。

- `review_status` 是 `passed`、`repaired` 或 `conservative_fallback`：放行。
- 方案上没有 `review_status` 这个字段：放行。注释写的是更早的方案照旧。
- `not_ready`：拦住，提示「方案检查还没有通过」。

确认按钮本身不看检查。`app/main.py:333-346` 的生成接口只建任务。拦住发生在 worker 真正出图时。

方案写成等待确认，有两个落库口，它们自己都不看检查：

- 对话任务：`app/db.py:1406-1417`，`finish_decision` 在 `kind == "plan"` 时把项目设成 `awaiting_generate`。入口是 `app/pipeline.py:729-735`。
- 直接改方案，或非对话的旧策划任务：`app/db.py:222-238` 的 `update_plan`。调用在 `app/main.py:322-331` 和 `app/pipeline.py:746-754`。

## 3. 现在仍能在检查没通过时出图的 6 类

### 1. 只剩状态衔接

`app/progressive_planning.py:2195-2217`。综合检查跑过，`ready` 是 false。若失败项只剩状态衔接，先试一次道具交接；没有改动就把这一项拿掉。拿掉之后没有别的问题时，检查记录留在方案上，状态写成 `conservative_fallback`，然后 `release`。出图闸门放行。

### 2. 综合检查没有完成，留下的稿又没有「已通过」的记录

`app/progressive_planning.py:1625-1646`。错误码是 `planning_review_incomplete`，而且手里有一份能用、又不是占位稿的方案时，直接收下：

- 那份稿的检查 `ready` 为真：状态是 `passed` 或 `repaired`。这不是绕过。
- 有检查项但没通过：状态是 `not_ready`。会进等待确认，出图被拦。
- 没有可绑定的检查：删掉检查记录，状态是 `conservative_fallback`。出图放行。这是绕过。

五个调用点都进这个分支：

- `app/progressive_planning.py:2117-2118` 综合检查那一次调用失败
- `app/progressive_planning.py:2122-2124` 审核记录修了三轮仍不完整
- `app/progressive_planning.py:2138-2140` 已经修过一次，记录仍无效
- `app/progressive_planning.py:2154-2156` 修正审核记录的那一次调用失败
- `app/progressive_planning.py:2160-2162` 没能保住原来的失败项

没有能用的稿时，这个分支不会放行，改走普通失败。网关里还有一段 `app/providers/gateway.py:2950-2970`，想在同一种错误上再放行 `compiled_plan`。`ProgressivePlanningError`（`app/progressive_planning.py:38-49`）没有这个字段，当前不会进入。

### 3. 失败时收下更早的稿，而那份稿没有通过的检查

这些路最后都到 `release`（`app/progressive_planning.py:1496-1520`）。`release` 会拒绝占位稿、堆卖点和剧情硬门，然后用 `_ensure_review_status`（`1472-1494`）补状态。已经写过的状态保持不变。没有状态时：检查 `ready` 为真就写成通过；有检查项但没通过就写成 `not_ready`（出图被拦）；否则写成 `conservative_fallback`（出图放行）。

会这样收下一份稿的调用点：

- `app/progressive_planning.py:1653-1661` 普通 `stop`，默认收下更早的能用稿。没有能用的稿就报错，不放行。
- `app/progressive_planning.py:1852-1853` 分镜结构修完仍不像分镜，但手里有更早的真稿。
- `app/progressive_planning.py:1948-1950` 这次编译出来的是占位稿，但手里有更早的真稿。占位稿本身不会放行（`1947-1953`）。
- `app/progressive_planning.py:2260-2263` 返修作废，放行更早记下的稿。
- `app/progressive_planning.py:2264-2272` 没有更早的稿时，声音和剧情硬门过了就放行当前这份。当前这份往往刚检查失败；记录若在稿上，状态会是 `not_ready`，出图被拦。记录不在这份对象上时，会写成 `conservative_fallback`，出图放行。
- `app/progressive_planning.py:2273-2274` 上面 `stop` 和直接收下的稿，都从这里进 `release`。
- `app/progressive_planning.py:2185-2188` 检查已经通过，但这份是占位稿，改放更早的真稿。更早那份若没有通过记录，同样会保守放行。

普通 `stop` 会走到「有旧稿就收下」的调用点：

- `1829` 分镜校正超过 24 轮
- `1856` 结构修复用尽，又没有更早的真稿（有真稿时已在 `1853` 收下）
- `1859`、`1862` 方案结构不完整
- `1876` 按对白重出分镜失败
- `1881` 无人声方向被加了台词
- `1885` 固定台词没留住
- `1928` 不是容量问题的合同失败
- `1936` 容量没有改善
- `1945` 容量返修作废之后再停
- `1952` 只剩下占位稿
- `2223` 台词收尾已经用过，问题还在
- `2243`、`2247`、`2249` 台词收尾失败或没有改动
- `2254` 同一组问题没有改善

这些点里，没有更早的真稿就不会放行。有真稿、而那份稿没有通过的检查时，出图会开始。

`refine` 里 `accept=False` 的 `stop`（`1679`、`1681`、`1785`、`1816`、`1821`）以及 `1719`、`1725`、`1814` 的 `RevisionKept`，自己不放行。调用方若是综合检查后的返修，会落到上面的 `2260`。若是容量返修，会落到 `1945`。若是商品依据映射，`1916` 封上之后继续，不放行。若是镜头工艺，`2039` 记下之后继续，不放行。

### 4. 策划回复没收完，交回上一份方案

`app/providers/gateway.py:2921-2944`。这一轮回复在语法上没收完，而且上一份方案有成片段落时，不经过 `release`，直接返回「方案已准备好」：

- `2927-2937` 上一份没有状态时，写成 `conservative_fallback`。出图放行。已经是 `not_ready` 的保持 `not_ready`，出图被拦。
- `2938-2944` 这份方案连结构校验都过不了时，删掉检查记录，强制写成 `conservative_fallback`。原来的 `not_ready` 也会被盖掉。出图放行。这里不查是不是占位稿。

### 5. 渐进策划关掉时的旧声画路径

默认开着渐进策划（`app/config.py:23`）。关掉时，或者方向选择关掉且没有已选方向时，`app/providers/gateway.py:2535` 的条件不成立，策划走旧路：脚本检查加声画检查。

`app/providers/gateway.py:2911-2919` 只有声画检查 `ready` 为真才返回方案。这条路不写 `plan_review`，也不写 `review_status`。出图闸门看到没有这个字段就放行（`app/progressive_planning.py:1221-1222`）。它绕过的是现在这条综合检查，不是「完全没有检查」。

非对话的旧任务在 `app/pipeline.py:746-754` 调用 `gateway.plan`，再 `update_plan`。方案从哪来，仍是上面这些策划出口。

### 6. 手工改方案

`app/main.py:322-331` 把请求体里的方案交给 `update_plan`。这里不看综合检查。改完的方案若没有 `review_status`，出图闸门放行。若改完仍是 `not_ready`，出图被拦。

## 4. 会把方案送到确认、但出图被拦住的

综合检查跑完且 `ready` 为真：`app/progressive_planning.py:2173-2191`。状态是 `passed` 或 `repaired`。这是正路，不绕过。

检查跑完但没通过，而且失败项不只是状态衔接：返修用尽或修订作废之后，若留下的稿上有检查记录，状态是 `not_ready`（见第 2、3 类里的那一支）。`app/providers/gateway.py:2564-2573` 仍用「广告方案已准备好，确认后再准备图片」收下它，`finish_decision` 把它写成等待确认。商家点生成之后，`images_blocked_reason` 才拦住。占位稿在这里会被再拒一次（`2564-2568`）。

`release` 自己还会拒绝、因而根本到不了确认的：占位稿（`1497-1501`）、产品片旁白还在堆卖点（`1502-1506`）、剧情硬门（`1508-1514`）。

## 5. 不放行、但会跳过重算

检查点：`app/planning_session.py:80-81`。同一阶段、同一输入、状态已是完成，就交回旧结果，不再请求模型。进度在 `app/providers/gateway.py:2033-2039`：这一步已完成时，连「正在分镜 / 正在综合检查」都不报。

重试丢掉分镜：`app/db.py:421-431` 调用 `app/story_logic.py:997-1006` 的 `planning_rerun_storyboard`。认的是错误文案里的几句中文（`972-978`），不是按阶段认。对得上就丢掉分镜及它后面的步骤。对不上，就只重开最后一步；分镜那一步若已完成，会被重放。这本身不把方案标成可以出图。重放之后若再走进第 2、3 类，才会在没有新检查的情况下出图。

离线跳过检查：`app/progressive_planning.py:2055-2056`，由 `scripts/replan_offline.py:207-208`、`275` 传入。`_ensure_review_status` 在 `skip_review` 时不写状态（`1481-1482`）。脚本说明写明不写回数据库（`replan_offline.py:208`）。

## 6. 当前硬门

放行时真正会拒绝方案的，是下面这些。没有新增，也没有把警告算进去。剧情六道加声音一道、再加产品片堆卖点，是 8 道。占位稿是后来加上的结构拒绝，单独写在表外。

剧情只在广告类型是剧情时检查，函数是 `app/story_logic.py:1103-1132` 的 `story_plan_issues`。`release`（`1508-1514`）、循环里的 `refuse_story_structure`（`1566-1574`）、记下候选时（`1539-1543`）都会用它。

| 硬门 | 商家看到的话 | 在哪判定 |
| --- | --- | --- |
| 对白还在说卖点 | 剧情对白还在说卖点 | `story_logic.py:1124-1129`，毛病名来自台词检查里的「在说卖点」 |
| 人物看着镜头 | 剧情人物看着镜头 | `story_logic.py:1122-1123` |
| 末镜英雄定格 | 剧情末镜写成了英雄定格 | `story_logic.py:1112-1114`，原文在 `story_ending_issues`（`175-176`） |
| 对着镜头举产品 | 剧情末镜人物对着镜头举着产品 | `story_logic.py:1112-1114`，原文在 `177-178` |
| 商标朝向镜头 | 剧情末镜把商标朝向镜头 | `story_logic.py:1118-1119` |
| 口号或链接 | 剧情末镜在喊口号或链接 | `story_logic.py:1120-1121` |

声音是另一道，三种广告类型各一句，函数是 `app/line_quality.py:1187-1204` 的 `voice_identity_issues`。循环里 `refuse_mismatched_voice`（`progressive_planning.py:1559-1564`）在记候选之前就停。检查通过之后（`2174`）和修订作废、准备放行当前稿时（`2270`）会再看一次。`release` 自己不再看声音。

| 硬门 | 商家看到的话 | 在哪判定 |
| --- | --- | --- |
| 声音和类型不一致 | 口播被写成了旁白；产品展示被写成了对口型推销；剧情被写成了整条旁白 | `line_quality.py:1198-1203` |

产品片堆卖点在 `release`（`1502-1506`）以及循环里（`1989-1994`、`2165-2172`、`2264-2269`）拒绝，话是「产品片旁白还在堆卖点」。判定用 `brand_stack_remaining` 和 `brand_visual_claim_overflow`。

占位稿不是上面这 8 道。`is_degenerate_plan`（`progressive_planning.py:1228-1242`）拒绝两种样子：内置的两句套话，以及镜头文案彼此相同或每镜都有「放在画面中间，光线稳定」。`release`（`1497-1501`）和渐进策划返回处（`gateway.py:2564-2568`）会挡。

下面这些有函数或有文字，放行不拿它们当硬门。这次没有把它们改成硬门。

| 检查 | 现在怎么处理 |
| --- | --- |
| 剧情末镜把家用物品带出门 | `story_ending_issues` 会算出（`story_logic.py:179-181`）。`story_plan_issues` 只留下英雄定格和举产品（`1112-1114`），不拦。 |
| 每个好处要有一镜看得见的技法 | `proof_highlight_issues`（`story_logic.py:239-248`）。主循环不调用，只有测试调用。 |
| 同框衣服和发型要分开 | `cast_distinction_issues`（`story_logic.py:1076-1091`）。主循环不调用，只有测试调用。 |
| 在念画面、第三人称解说 | `story_explainer_remaining`（`line_quality.py:1166-1176`）。给改写用，改不掉也不硬停。 |
| 点题 | `scrub_story_purposes` 只改镜头 purpose。不是硬停。 |
| 床上的人、背景人物 | `background_issues` 最多请模型改一次（`progressive_planning.py:2002-2010`）。改不了就继续。 |
| 状态声明对不上、衣服没写进外观、状态图和镜头不一致、角色四视图里出现商品 | 只记警告（`2072-2102`）。 |
| 镜头工艺 | 改一次文字，再窄返修一次；失败就记下继续（`2016-2054`）。 |
| 末镜大约 2 秒、末镜不要推近 | 时长挪不动就保持。推近不进硬门（`story_logic.py:163-164`）。 |
| 状态配对 | `state_pairing_issues` 固定返回空列表（`story_logic.py:611-613`）。 |

## 7. 旧技能附录有没有被加载

四个候选都还有代码会读，不能当死文件删。默认配置下，前三份不进当前这条渐进策划；第四份默认关着，打开开关就会加载。

| 文件 | 会不会被读到 | 在哪 |
| --- | --- | --- |
| `app/skills/ads-video-storyboard-direction/references/legacy-storyboard.md` | 会。渐进策划关掉时，分镜阶段改走 `legacy_storyboard`，注册表把这份参考放进该阶段。默认渐进策划开着，这条不走。 | `app/planner_stage.py:26-27`；`app/providers/gateway.py:2060-2061` 传入 `legacy_storyboard=not progressive_planning_enabled`；`app/skills/registry.json:313-316` |
| `app/skills/ads-video-plan-review/references/script-review.md` | 会。阶段名是 `script_review` 时，注册表带上这份参考。这个阶段在第 5 类旧路上，默认渐进策划不进入。 | `app/providers/gateway.py:2581-2604`；`registry.json:445-447` |
| `app/skills/ads-video-plan-review/references/alignment-review.md` | 会。阶段名是 `alignment_review` 时加载。同样只在第 5 类旧路。 | `app/providers/gateway.py:2810-2833`；`registry.json:449-451` |
| `app/skills/ads-video-voiceover-draft/SKILL.md` | 会，但要打开 `SPEECH_DRAFT_ENABLED`。默认是关（`app/config.py:26`）。打开之后，台词阶段改成 `speech_draft`，加载这份技能。 | `app/planner_stage.py:18-19`；`app/providers/gateway.py:2458-2459`；`registry.json:491-503` |

加载方式是 `app/runtime_skills.py:147-165` 的 `stage_skill_context`：阶段名对上、技能状态是 active，就把该阶段在注册表里点名的资源读进模型上下文。

编排技能 `app/skills/ads-video-orchestrator/SKILL.md:13` 仍写「每轮最多三项」。代码是最多 4 题，顺序为广告类型、卖点、拍给谁、去向，剧情不留去向（`app/ad_form.py:210-247`，`fixed = fixed[:4]`）。这次没有改技能文件。
