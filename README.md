<div align="center">

# 映作 Ad Studio

让你的商品，拥有自己的镜头。

<p>
  <img alt="Python 3.10+" src="https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white">
  <img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white">
  <img alt="React 19" src="https://img.shields.io/badge/React-19-149ECA?logo=react&logoColor=white">
  <img alt="Vite 6" src="https://img.shields.io/badge/Vite-6-646CFF?logo=vite&logoColor=white">
</p>

</div>

映作是给国内中小电商商家用的本地商品广告工作台，一次一个人，跑在自己的电脑上。首页上传商品原图（JPG、PNG 或 WebP，最多 3 张），时长在 10–60 秒之间，默认 20 秒。广告类型三选一：`brand_tvc` 产品展示、`scene_story` 剧情、`presenter_pitch` 人物讲解。首页点中某一类后，对话里第一条消息上方会带上对应标签，例如 `@ 产品展示`。

澄清、选方向和改方案都走底部同一个输入框，问题卡挂在这个框上。资料够了会给出三个方向，点一下卡片就是选定。选定之后才写这一套台词和分镜；卡片还在、还没点的时候，不预先写任何一条。画面说明里的「第几句」按嘴里实际说出的句子数，不按镜头号。商品图跟你发出去的那一条消息走。

左边是对话和当时的执行进度，右边画布放商品、场景和视频。正在生成参考图时，画布先不展示视频区。

## 示例成片

仓库里目前只有这一条公开示例，点开即可播放。

| 示例 | 成片 |
| --- | --- |
| 产品展示 | [产品展示.mp4](docs/demo/产品展示.mp4) |

## 工作流程

澄清、出方向、台词和分镜这几步，可以由模型在回复里安排下一步。没有写下一步时，选定方向后先写台词，再写分镜。只写出台词、还没分镜时，会再定一次下一步，不会把没排完的画面交给你。排时间、检查方案，以及出图、出视频这些要花钱的步骤，由程序按状态往下走。模型不能跳过检查，也不能在确认之前出图或出视频。

```mermaid
flowchart LR
  clarify[澄清] --> directions[方向]
  directions --> choose[选定]
  choose --> lines[台词与分镜]
  lines --> check[检查]
  check --> plan[确认方案]
  plan --> images[出图]
  images --> pictures[确认图]
  pictures --> video[出视频]
```

确认方案之后才出图。确认图片之后才出视频。

出图复用你上传的商品原图，另出一张场景图；有出镜人物时，每位一张角色参考，声明的状态画在这张参考上。这些图片确认之后，才按 9:16 提交视频。新方案使用视频模型的中文原声。受理结果还不清楚时，不会自动再付一次费。

## 功能特性

| 功能 | 现在 |
| --- | --- |
| 广告类型 | 产品展示 `brand_tvc`、剧情 `scene_story`、人物讲解 `presenter_pitch`。首页选中后，第一条消息上有类型标签。 |
| 三个方向 | 给出三个方向，哪一条都由你点选。点选前不预写台词和分镜（`PLANNER_SPECULATE_RECOMMENDED` 默认关）。 |
| 检查 | 方向和最终方案都会看台词、画面和时间。合适的方案才会请你确认。 |
| 先图后视频 | 确认方案后出场景图和角色参考；确认图片后才提交视频。 |
| 进度 | 左侧对话展示实际执行记录，可刷新恢复、取消和下载。 |
| 成片 | 中文原声，按 9:16、720×1280、24fps 合成 MP4。结尾不另加文字卡，也不自动烧录字幕。 |
| 句子与图片 | 「第几句」按说出的句子计。商品图挂在发出它的那条消息上。生成参考图时画布不展示视频区。 |
| 创作方法 | `app/skills/` 有 8 份规范 Skill：6 份已接到运行，参考检索和成片质检两份还没接到运行里。 |
| 余额检查 | 还没接到运行里。 |
| 埋点 | 还没接到运行里。 |
| Hooks | 还没接到运行里。 |
| 外部爆款检索 | 还没接到运行里。 |
| P3 图片语义回检 | 还没接到运行里。 |

策略来自本地整理的方法，不是实时案例检索。来源记录可以追溯商家摘录和原图引用，不能代替商品说法是否真实、图片观察是否准确，也不能代替这条广告是否有效。

## 技术栈

| 层 | 内容 |
| --- | --- |
| 工作台 | React 19、TypeScript、Vite 6、lucide-react |
| 服务 | FastAPI、Uvicorn，只监听 `127.0.0.1`，单 worker |
| 数据 | SQLite（`data/studio.sqlite3`）和本地媒体目录 |
| 文本策划 | `.env.example` 写的是土豆 `gpt-6-sol`。不设置 `PLANNER_PROVIDER` 时，代码默认 DeepSeek `deepseek-flash` |
| 图片 | 代码默认 `gpt-image-2-all`。没有同名环境变量 |
| 视频 | 代码默认 `wan3.0`，中文原声。没有 `VIDEO_MODEL` 环境变量 |
| 合成 | `imageio-ffmpeg` 提供的 FFmpeg。成片规格 720×1280、24fps |
| 创作方法 | 8 份 Skill 见 [Skill 索引](docs/SKILL_INDEX.md)。`shared/` 是共用参考，不算单独的一份 Skill |

旧的 `ad_expression` 只留着读历史记录，不参与当前流程。

## 目录结构

```text
.
├── app/                  后端、供应商适配、Skill 与共用参考
├── frontend/             React 工作台
├── tests/                替身供应商下的测试
├── docs/                 公开文档；docs/demo/产品展示.mp4 为示例成片
├── scripts/              源码检查与导出
├── .github/workflows/    索引检查、测试与前端构建
├── .env.example          配置模板，不含密钥
├── requirements.txt
├── requirements-dev.txt
├── start.command         macOS 上启动已构建的本机服务
├── AGENTS.md
└── CONTRIBUTING.md
```

`.env`、`data/`、`.runtime/`、`.local/` 只留在本机，不提交、不打包。数据库里的媒体是绝对路径，搬走时要连数据库和媒体一起备份，并重定位引用。

## 快速开始

### 环境

需要 Python 3.10+、Node.js 20.19+。源码使用 POSIX 文件锁，请在 macOS 或 Linux 上运行。完成下面的安装后，macOS 也可以双击 `start.command`。关掉终端就会停掉本机服务。

保持 `127.0.0.1` 和单个 worker。同一份数据目录不要同时开两个进程，也不要把服务直接暴露到公网。

### 安装

在项目根目录执行：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
cd frontend
npm ci
npm run build
cd ..
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8787 --workers 1 --no-access-log
```

打开 <http://127.0.0.1:8787>。

### 填写连接

打开 <http://127.0.0.1:8787/setup>，或按 `.env.example` 在本机写一份 `.env`。设置页只保存密钥，不把密钥回显到页面。改供应商或模型后重启服务。

示例文件里的文本策划是 `PLANNER_PROVIDER=tudou`、`PLANNER_MODEL=gpt-6-sol`。`TUDOU_API_KEY` 同时用于文本、图片和视频，需要这把密钥具备相应模型权限。没有写 `PLANNER_PROVIDER` 时，代码仍用 DeepSeek，不会自行改到土豆。

图片和视频调用可能产生费用。没有配好连接时，界面提示缺少配置，不会用示例视频充当生成结果。

### 前端热更新

后端已经在 8787 运行时，另开一个终端：

```sh
cd frontend
npm run dev
```

Vite 把 `/api` 和 `/media` 转到本机 8787。连接设置仍从后端的 `/setup` 打开。

## 配置

变量只写名称和用途。真实密钥放在本机 `.env` 或设置页，不要写进仓库。本地 `.env` 会覆盖同名环境变量。带 `#` 的行在 `.env.example` 里是注释，不生效。

| 变量 | 用途 |
| --- | --- |
| `TUDOU_API_KEY` | 土豆接口密钥。策划走土豆时，文本、图片和视频共用。 |
| `DEEPSEEK_API_KEY` | DeepSeek 密钥。`PLANNER_PROVIDER=deepseek`，或回退到 DeepSeek 时使用。 |
| `DEEPSEEK_MODEL` | DeepSeek 模型名。不设置时为 `deepseek-flash`。 |
| `PLANNER_PROVIDER` | 文本供应商：`tudou` 或 `deepseek`。示例文件为 `tudou`；不设置时为 `deepseek`。 |
| `PLANNER_MODEL` | 文本模型名。土豆示例为 `gpt-6-sol`。留空时，土豆用 `gpt-6-sol`，DeepSeek 用 `DEEPSEEK_MODEL`。 |
| `JOINT_PLANNING_ENABLED` | 试验开关。为 `true` 时，选定后同一次写文案和分镜。默认 `false`。 |
| `CONTENT_DRAFT_ENABLED` | 试验开关。初稿只写实际台词和画面。默认 `false`。 |
| `SPEECH_DRAFT_ENABLED` | 试验开关。只影响产品展示里的画外音候选。默认 `false`。 |
| `PLANNER_STORYBOARD_EFFORT` | 土豆 `gpt-6-sol` 分镜推理档：`low`、`medium`、`high`。不设置时为 `high`；示例文件写的是 `medium`。 |
| `PLANNER_DIRECTION_REVIEW_EFFORT` | 方向检查的推理档。该阶段仍使用土豆 `gpt-6-sol` 时生效。 |
| `PLANNER_STAGE_ROUTES` | 按阶段把写稿、分镜、检查或调整指到另一个文本模型。不设置则都走主模型。 |
| `PLANNER_FALLBACK_PROVIDER` | 文本超时或指定 HTTP 状态时改用的供应商。 |
| `PLANNER_FALLBACK_MODEL` | 上面这个回退用的模型名。 |
| `PLANNER_FALLBACK_ON` | 何时回退，例如 `timeout`、`504`。 |
| `PLANNER_STREAM` | 文本请求是否走流式：`off`、`on`、`auto`。默认 `off`。 |
| `PLANNER_REQUEST_DEADLINE_SECONDS` | 单次文本等待上限，30–600 秒。不设置则沿用代码里的等待。 |
| `PLANNER_MAX_REFINEMENTS` | 方案调整最多几轮：`0`、`1` 或 `2`。默认 `2`。 |
| `PLANNER_REFINEMENT_FORMAT` | 调整时返回全文或差异：`sparse_or_full`、`sparse`。默认 `sparse_or_full`。 |
| `PLANNER_SPECULATE_RECOMMENDED` | 方向还没点选时，是否预先写推荐方向的文字方案。默认 `false`。 |
| `AD_SPEECH_RATE_BRAND_TVC` | 产品展示的工程容量，单位字/秒。不设置时为 `3.0`。这是容量，不是实测语速。 |
| `AD_BEAT_CHARS_BRAND_TVC` | 产品展示单拍字数上限。不设置时为 `30`。 |
| `AD_SPEECH_RATE_SCENE_STORY` | 剧情的工程容量，单位字/秒。不设置时为 `3.8`。 |
| `AD_BEAT_CHARS_SCENE_STORY` | 剧情单拍字数上限。不设置时为 `38`。 |
| `AD_SPEECH_RATE_PRESENTER_PITCH` | 人物讲解的工程容量，单位字/秒。不设置时为 `4.8`。 |
| `AD_BEAT_CHARS_PRESENTER_PITCH` | 人物讲解单拍字数上限。不设置时为 `48`。 |
| `AD_TAGLINE_ENABLED` | 口号开关。默认 `false`：不写口号，不加结尾字卡。 |
| `AD_STATE_REFERENCES` | 状态参考开关，默认开。当前确认前要的是场景图和角色参考；单独的状态图不是出视频前的必选项。 |
| `IMAGE_CONCURRENCY` | 图片同时提交张数，1–8 的整数。默认 `3`。 |
| `TTS_VOICE` | 只给旧的画外音项目用。新方案用视频模型原声。默认 `Tingting`。 |
| `AD_DATA_ROOT` | 可选。指向另一份私有数据目录。留空就用项目下的 `data/`。 |

示例里注释掉的阶段路由、超时回退、流式输出和预写推荐方向，默认都不生效。它们是可以切换的试验配置。是否更快、文案是否变样，要在本机真实请求上看；离线测试不能代替。字段说明以 `.env.example` 和 [项目现状](docs/PROJECT_STATUS.md) 为准。

## 测试

在不含生产 `.env` 的干净目录里跑，并用临时数据目录，避免测到已有项目：

```sh
AD_DATA_ROOT="$(mktemp -d)" .venv/bin/python -m pytest -q
node --test tests/frontend_*.mjs frontend/tests/*.test.mjs
cd frontend
npm run build
```

这些检查用替身供应商看接口和流程，不能代替真实出片，也不能代替广告是否好用。准备提交时另跑 `python3 scripts/check_source.py`。已做过的核对见 [验证记录](docs/VALIDATION.md)。

## 更多文档

| 文档 | 内容 |
| --- | --- |
| [项目现状](docs/PROJECT_STATUS.md) | 当前源码做到哪一步 |
| [架构](docs/ARCHITECTURE.md) | 模块、确认和付费边界 |
| [路线图](docs/ROADMAP.md) | 阶段计划；还没做的保持还没做 |
| [Skill 索引](docs/SKILL_INDEX.md) | 8 份 Skill 的接入情况 |
| [验证记录](docs/VALIDATION.md) | 已有核对，不代替你自己看成片 |
| [开源准备](docs/OPEN_SOURCE.md) | 哪些目录可以公开 |
| [第三方声明](docs/THIRD_PARTY_NOTICES.md) | 改编来源与对方许可 |
| [资料索引](docs/REFERENCES.md) | 研究参考，还没接到运行里 |
| [策略来源](docs/STRATEGY_SOURCES.md) | 本地创意规则从哪来 |
| [阶段记录](docs/STAGE1_ACCEPTANCE.md) | [1](docs/STAGE1_ACCEPTANCE.md) · [2](docs/STAGE2_ACCEPTANCE.md) · [3](docs/STAGE3_ACCEPTANCE.md) · [4](docs/STAGE4_ACCEPTANCE.md) |
| [导出说明](scripts/README.md) | 源码检查与允许列表导出 |
| [参与开发](CONTRIBUTING.md) | 隔离数据、替身供应商、提交前检查 |

项目许可证还没选定。第三方许可不能代替本项目的许可。
