# PKU Treehole Digest

一个面向个人使用的 Windows 桌面工具：按两次报告之间的时间范围读取北大树洞新帖，通过 DeepSeek API 按可修改的个人画像完成分类、相关度评分和摘要，并生成本地 HTML 报告。

## 主要功能

- 首次扫描最近 24 小时，以后从报告目录中最新的时间戳继续扫描，不设固定帖子数量上限。
- 常规精选数量以扫描总数约 10% 为中心，根据相关度分布自适应调整。
- 评论数不小于 20 或收藏数不小于 20 的本时段新帖进入独立高热度分区。
- 不复查历史报告中的普通帖子；只有收藏夹中的洞号会被刷新并提示新回复。
- HTML 包含目录、复制洞号、原帖与完整评论弹窗，以及与桌面程序同步的折叠式收藏夹。
- 桌面收藏页支持通过洞号直接添加、刷新、查看、移动和移除收藏帖。

## 隐私说明

- 账号、密码和 DeepSeek API Key 使用系统凭据管理器保存，不写入项目文件。
- 会话、收藏数据与本地接口令牌保存在 `%LOCALAPPDATA%\PKUTreeholeDigest`。
- `个人画像.yaml`、HTML 报告、抓取数据、缓存和构建产物均被 `.gitignore` 排除。
- 待分类的帖子正文和少量评论会发送给配置的 DeepSeek API。使用者应自行评估学校规则、平台条款和数据处理风险。
- 不要把报告、真实个人画像、会话文件、截图或真实树洞内容提交到公开仓库。

## 安装与运行

需要 Windows 和 Python 3.11 或更高版本。

```powershell
cd 项目代码
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item ..\个人画像.example.yaml ..\个人画像.yaml
.\.venv\Scripts\pythonw.exe -m pku_treehole_digest.gui
```

首次打开后填写北大学号、IAAA 密码和 DeepSeek API Key，再按自己的需求修改个人画像。个人画像默认收起。

## 构建无控制台 EXE

```powershell
cd 项目代码
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --onefile --windowed --name "树洞日报" --paths ".\src" --distpath ".." --workpath ".\build\pyinstaller" --specpath ".\build" ".\scripts\treehole_gui_entry.py"
```

构建出的 EXE 不包含账号、密码、API Key、个人画像、收藏或报告。运行时仍需保留仓库的 `项目代码` 目录以及自行创建的 `个人画像.yaml`。

## 测试

```powershell
cd 项目代码
.\.venv\Scripts\python.exe -m pytest -q
```

## 发布前检查

在 GitHub Desktop 的 Changes 页面确认不存在个人画像、日报、虚拟环境、密钥、Token、学号、邮箱、手机号、收藏数据、会话状态或真实树洞内容。
