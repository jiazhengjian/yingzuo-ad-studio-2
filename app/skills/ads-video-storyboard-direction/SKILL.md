---
name: ads-video-storyboard-direction
description: 将广告内容、台词和动作协调成可执行分镜，安排商品呈现、连续状态和跨镜声画关系。用于 storyboard 与 plan_refinement；镜头文字返工只加载 shot_craft_rewrite，不生成逐镜关键帧。
---

# Ads Video Storyboard Direction

按本轮阶段与输出 Schema 选择唯一分支，不能混用两种合同。新内容稿是否可协调、时轴是否固定由实际输入决定，不根据历史文件或作者自述猜测。

- `storyboard`、常规 `plan_refinement` 使用 [内容与分镜协调](references/coordinated-storyboard.md)。初次分镜输出 `DirectorDraft`；返修只按本次 Schema 输出可编辑的 `ad_script` 与有序 `shots/coverage`，固定字段由程序保留并组合完整候选。若本次明确为最终台词收尾且 Schema 只含 `narrations`，则只返回全部原有非空台词的 `beat_id/narration`，不输出内容稿或镜头；其他字段均由程序保留，不能改变固定台词或商品依据。程序随后计算实际时轴，再独立综合审核。不得输出旧 Plan 的 `segments` 或 `speech_spans`。
- `shot_craft_rewrite` 使用 [镜头字面写法](references/shot-literal.md)。只改点名镜头的 `description` 和 `camera_move`，不改台词、时长、出镜标记或其他镜头。这不是一轮分镜返修。

遵守商品事实、用户明确固定内容、选中故事核心、人物/声音与总片长。实际动作、物体依附、消耗状态和语音先后须成立；时间省略须对应真实切点及其前后状态，入镜切点不能省略该镜开始后才发生的动作。优先减少未固定的动作，使实际画面在片长内成立，目的说明和下调估时不能替代内容修改。只产生文字分镜与素材引用，不新增用户确认或逐镜关键帧；图片和原生音频的实际质量仍待相应验收。

有内容初稿时接续脚本规划；联合起稿时直接使用本阶段共享的分类表达方法，并与脚本规划里的台词方法同次形成实际台词和镜头。TVC 参考的改编范围和哈希见 `registry.json`。
