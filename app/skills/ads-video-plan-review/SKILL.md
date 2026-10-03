---
name: ads-video-plan-review
description: 按当前模式独立检查方向或实际最终方案，给出证据与问题定位；最终方案区分阻断问题与改进建议，不在审核中改稿，文字通过不等于成片通过。
---

# Ads Video Plan Review

读取本轮阶段、实际待审产物、原始要求、商品来源与能力输入，按 Schema 执行唯一分支：

- `direction_review`：[方向审核](references/direction-review.md)，使用本阶段共享的商品依据与广告形式合同。
- `plan_review`：[最终方案综合审核](references/plan-review.md)，检查程序排时后的完整方案，区分必须阻断的问题与不阻断的改进建议，不悄悄改稿后自行放行。
- 仅兼容旧任务的 `script_review`、`alignment_review` 分别使用 [脚本审核](references/script-review.md)、[声画审核](references/alignment-review.md)；不混入新流程。

作者目的、自述已修好、事实 ID 与结构校验均不能代替内容证据。一次报告可确认的问题与位置，缺输入如实说明。最终方案中的事实、明确要求、必要声画关系和可执行性未满足时必须阻断；普通表达与审美不足按对应方法列建议，不因此判硬项失败，也不以建议掩盖无法确认的必要条件。修订后仍完整检查实际产物，不只检查上次失败项。

模型不安排返修次数或追加调用，这些由程序控制。未实际看听媒体时不声称已验证动作、口型、声线或最终质量，不以额外生图消除文字审核问题。方法来源与哈希见 `registry.json`。
