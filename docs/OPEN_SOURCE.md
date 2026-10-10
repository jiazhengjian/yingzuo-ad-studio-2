# 公开仓库准备状态

本目录已按源码、文档和本地私有数据分离。整理工作不等于已发布开源项目；尚未设置 GitHub 远端。自有源码已选定 MIT 许可证，全文见仓库根目录 [LICENSE](../LICENSE)。

## 可以进入源码仓库

- `app/`：后端与供应商适配。
- `frontend/`：前端源码、依赖清单与锁文件；不含安装依赖、构建文件。
- `tests/`：使用替身供应商的验证代码。
- `docs/`：现状、架构、规划与外部资料索引。
- `scripts/`、`.github/`：发布范围检查、源码导出和 CI。
- README、协作说明、启动脚本、空值 `.env.example`、依赖清单与根目录 `LICENSE`。

## 仅本机保留

| 位置 | 内容 |
|---|---|
| `.env` | 当前运行密钥，仅后端读取 |
| `data/` | 项目、对话、生成记录、商品图、场景图与成片 |
| `.runtime/` | 服务日志、进程记录 |
| `.local/demo/` | 历史真实样片和检查图片 |
| `.local/archive/` | 旧版 README、PRD、竞品报告、原验收记录 |
| `.local/research/` | 研究过程、抽帧、分析脚本与内部备份 |
| `.local/references/` | 用户提供的第三方参考项目与竞品材料 |
| `.local/exports/` | 按允许列表导出的源码副本 |

上述私有路径已加入 Git 忽略。源码导出脚本采用允许列表；普通整目录压缩不会遵循 Git 忽略，不能当作公开源码包。

## 许可证

自有源码已选定 MIT 许可证，许可文本在仓库根目录 [LICENSE](../LICENSE)。第三方组件和参考材料仍遵循各自许可，不能用本文件代替。

## 发布前尚需确定

- GitHub 仓库名、所有者与远端地址；本轮不创建远端、不推送。
- `docs/demo/` 有三条 README 公开示例。产品展示用 `产品展示.mp4`、`产品展示.gif`、`产品展示_poster.jpg`；剧情用 `story.mp4`、`story.gif`、`story_poster.jpg`；人物讲解用 `talking.mp4`、`talking.gif`、`talking_poster.jpg`。历史真实样片仍留在本机。源码导出包不包含这些音视频、动图和静帧。

## 第三方依赖与参考

Python 依赖记录在 `requirements*.txt`，前端依赖记录在 `frontend/package*.json`。源码包不包含依赖安装目录、FFmpeg 可执行文件或完整第三方项目；安装后的第三方组件遵循各自许可。

研究索引见 [REFERENCES.md](REFERENCES.md)。本轮没有把新发现的 Skill 正文或第三方实现并入应用。后续采用时，应记录具体来源、版本和原许可，不能把网站或视频素材的授权等同于某个 GitHub 仓库的许可。
