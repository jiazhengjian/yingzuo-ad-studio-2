# 源码导出与离线检查

仅需 Python 3.10+ 标准库，无网络、无模型调用，不读取项目的私有环境文件。以下命令从项目根目录执行。

    python3 scripts/check_source.py
    python3 scripts/test_source_tools.py
    python3 scripts/export_source.py

导出生成新的 ZIP，默认位于项目 .local/exports/source-UTC时间.zip；解压后直接是项目根目录结构。已有输出不会覆盖。可指定项目外的新 ZIP 路径：

    python3 scripts/export_source.py --output ../ad-studio-source.zip

--root 可以指定要检查或导出的源码根目录。项目内只允许输出到 .local/exports；导出不会修改源码，也不会递归包含已有导出。遇到检查错误时拒绝产出压缩包。

白名单定义在 source_policy.py，只有以下文件可导出：

- app、frontend、tests、docs、scripts、.github 中允许的文本源码、配置和文档扩展名。
- 根目录 Markdown/reStructuredText 文档与 Python/shell/command 源文件。
- 明确列出的根依赖清单与锁文件、项目配置、LICENSE/NOTICE、启动文件、Git 配置和 .env.example。模板也进行内容检查，不能填写真实密钥。

始终排除 .env 及其备份、data、.local、demo、work、outputs、.git、虚拟环境、node_modules、dist/build、缓存、数据库、日志、密钥文件和图片/音视频/压缩包。源目录中的符号链接会被拒绝。新增源码类型应先审阅并修改白名单；不要为了省事改成复制整个目录。

默认检查只覆盖将要导出的源码，不会因为本地正常存在 .env、data 或安装依赖而失败。准备提交时，先把计划发布的文件加入 Git 索引，再运行：

    python3 scripts/check_source.py --tracked

此模式检查全部 Git 索引路径，也拒绝被误追踪的私有文件、构建结果、媒体和白名单之外的文件；它检查工作区现有内容，不检查整个 Git 历史。CI 使用这个模式。

所有检查结果只输出相对文件路径和规则名，不显示匹配行、密钥内容或底层错误详情。检查覆盖常见用户目录绝对路径、私钥块、若干明显 token 格式和静态密钥赋值；仅认可明确的 TEST_ONLY、test-secret-placeholder、sk-test 等测试占位形式，不豁免整个 tests 目录。

这是启发式检查，不能发现所有编码、拆分或新格式的密钥，也不能自动判断第三方代码许可、个人信息或文档真实性。假测试值应显式带测试标记；真实凭据绝不能使用这些前缀来绕过检查。发布前仍需人工核对 ZIP 文件清单、文档和代码来源。本脚本不创建许可证，也不发布到 GitHub。

checks.yml 在 Ubuntu、Python 3.12、Node 22 上执行索引检查、这三个脚本自检、pytest、前端 Node 离线回归与生产构建。依赖安装需要网络；检查与测试不配置真实供应商密钥。macOS 专用旧配音测试按现有测试条件跳过。此 CI 文件只有在 GitHub 实际运行后，才能报告 Linux 验证成功。
