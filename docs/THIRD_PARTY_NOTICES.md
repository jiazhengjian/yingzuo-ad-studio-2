# 第三方许可与改编说明

## TVC AI Director Skill Suite

参考由用户提供的本地 `tvc-ai-director-main` 文档快照，未核验远端仓库地址或 commit，不据目录名推测 GitHub URL。历史广告规则的来源与范围见 [STRATEGY_SOURCES.md](STRATEGY_SOURCES.md)；本轮十份 Ads Video Skill 的来源文件、SHA-256 与范围见 [SKILL_INDEX.md](SKILL_INDEX.md) 和 `app/skills/registry.json` 中逐项 `source_refs`。

映作有选择地改编了阶段分流、商品与创意的关系、真实参考、视觉约定、镜头目的、声画节奏、素材职责、连续性和返工方法，重新编写为适合本产品的输入输出。中文台词例子、跨镜语义、本产品三方向选择及确认流程是本项目的整理与实现。没有安装第三方全局技能，没有引入整个仓库、第三方素材或其 TSV/Excel 导出脚本；也未从该来源引入案例数据库、搜索实现或声音一致性检测实现。

本轮不采用固定十问、强制剧情/高潮、默认多张视觉开发图或每镜关键帧。“每镜必须新增信息”不作为来源原文要求，也不作为 Ads Video Skill 的通用规则；有目的的展示、停留与静默均可成立。

以下保留该快照附带的完整 MIT 许可及作者版权声明，适用于借鉴与改编部分；它不为其他来源材料授权，也不替代映作自身尚未确定的项目许可证。

```text
MIT License

Copyright (c) 2026 Guangjun

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## TVC Director

参考用户提供的 `tvc-director-main/references/treatment.md` 本地快照，将产品世界与品牌世界、三种产品呈现结构及核心视听概念的方法重写为映作第二阶段的契约与提示。文件来源及 SHA-256 记录在技能注册表；没有运行其工具或引入媒体，没有将默认品类建议或特定供应商限制当作本项目能力。未核验此快照的远端 commit。品牌世界的功能、身份、感官三条路径，以及概念层的视觉隐喻，是对该 treatment 的改编，不是照抄；不引入九宫格，也不把结尾字卡写进方向。

同一作者的时长模板、产品出镜、镜头词汇和阶段结构，另改编自用户提供的摘录汇编 `tvc-director-examples.md`（含汇编说明与逐字摘录，SHA-256 `c970a937bc60e4c0a2e6d88c168062cddc1eb04ca0b65884d99d476eaa8943c1`）。来源默认 16:9 与 Seedance；映作改为 9:16 与 wan3.0，不引入宫格流程或让模型绘制 Logo。该摘录没有文案或旁白例子，类型 playbook 中的中文台词例子为映作原创。本轮台词技能里的好坏对照同样是映作原创，摘录没有可供改写的文案例子。这些 playbook 按广告类型注入，不是把来源的固定秒表原样当作唯一时长。

分镜这一轮继续改编同一摘录：先写视频脉络再派生镜头，每镜用能拍到的主体、动作、景别、运镜、光线、产品状态、环境和台词落点来写。比喻留在方向，不写进镜头和视频提示词。产品出镜按类型，产品展示大约八成、剧情大约一半、人物讲解大约七成，不是把来源的七成宫格铁律原样搬来。英雄定格写在视频镜头里，不另加字卡，也不让模型画 Logo。长镜头的多阶段只出现在同一次视频提示词里。不引入九宫格，不多生成图片。

```text
MIT License

Copyright (c) 2026 Ethan Wang

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
