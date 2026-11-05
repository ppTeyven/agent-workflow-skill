# agent-workflow-skill

> 通用 Agent 工作流方法论 —— **查证 → 量化定位 → 视觉确认 → 交付**
>
> 一份**自包含、可独立分发**的 Cursor Agent Skill。不绑定任何项目，不依赖本机任何 rule / handbook。

---

## 它解决什么问题

大多数 Agent 的失败不是"不会写代码"，而是**方法上的四个偏差**：

| 偏差 | 表现 | 后果 |
|---|---|---|
| **用数字代替眼睛** | 像素指标达标就宣布"验收通过" | 画面从来没被真正看过，用户看到的还是错的 |
| **闷头全做完** | 用户只要 1 项，Agent 做了 8 项 | 烧掉 8 倍 token，5 项是白做的 |
| **乱起 agent** | 起了几个"探索性" agent，用不上也停不掉 | 协调成本超过收益，还留下僵尸进程 |
| **token 无意识消耗** | 整份 Read 2MB bundle、13 次 sleep 轮询、逐帧读图 | 又慢又贵，还查不出原因 |

这份 skill 把这四条纪律收敛成一套**可执行、可验证**的流程。

---

## 核心原则

### 1. 定位用数字，验收用眼睛

| 阶段 | 手段 | 判定者 |
|---|---|---|
| **定位根因** | 数值探针（像素指纹 / 连通域 / 直方图 / 帧时间） | Agent，数字即证据 |
| **验收效果** | **截图 + Agent 亲眼看** | **必须亲眼看，数字不可替代** |

**指标达标 ≠ 视觉合格。** 数字只能排除"明显没变化"，排除不了"变化是错的"。

### 2. 任务清单必须让用户裁剪

长任务（≥5 个子任务）开工前列清单，**逐项**向用户确认「继续 / 取消 / 降级」：

```
1. [必须] 修复 A —— 为什么值得做 / 不做的后果 / 成本估算
2. [建议] 补 B   —— 同上
❓ Q1｜逐项确认：1 做 / 2 做 / 3 跳过？
```

**用户说"修这个"通常只授权了那一项。** 同批次顺手发现的其他改进要列成候选项，不要直接做。
被否决的项不反复复活。清单状态对用户可见。

### 3. Agent 必须有明确产出，用完即停

> **起 agent 前先回答：这个 agent 产出的东西，我拿到后能直接做什么？**
> 答不上来，就别起。

- 只有「天然独立、互不共享上下文、合并后无交叉验证价值」的子任务才并行
- 并发 **≤ 3**
- **禁止轮询等待** —— 一次给足上下文让它跑完，结束统一收结果
- 前台不重复 agent 已经在做的调查，已委派就只做协调

### 4. Token 成本自检

| 大户 | 量化 | 规避 |
|---|---|---|
| 大文件整份 Read / 宽 Grep | 返回无语义长行，成本 10 倍以上 | 脚本切片，单次输出 ≤ 20 处命中 |
| 反复失败重试 | 同参数调用 ≥ 3 次 | **4 次失败就停**，报告现象 + 最可能的下一步 |
| 全量日志 / 多 MB JSON | 单次输出 > 20,000 字符 | 摘要进对话（≤30 行），原始落文件 |
| 每个 agent 重读项目 | agent 输入含重复背景 | 给足上下文，或让它只读指定文件 |
| 截图/frame 序列逐张读 | N 张图 = N 倍视觉 token | 只读关键帧（首/末/异常） |

---

## 快速开始

### 安装（Cursor）

把整个目录放进用户级 skills 目录：

```powershell
# Windows
Copy-Item -Recurse agent-workflow-skill "$env:USERPROFILE\.cursor\skills\agent-workflow"

# macOS / Linux
cp -r agent-workflow-skill ~/.cursor/skills/agent-workflow
```

也可以直接用 GitHub 仓库安装（Cursor 支持从仓库拉取 skill）。

安装后，Cursor 会自动在匹配任务时调用它。也可手动触发：

```
用 agent-workflow skill 帮我验收这个页面
```

### 只取脚本用

四个脚本都是**独立可跑**的，不需要安装这个 skill 就能用：

```powershell
# 1. 校验 HTML 里内联 <script type="module"> 的语法（改压缩产物后的必过门禁）
node scripts/check_syntax.mjs path/to/template.html

# 2. 抓 WebGL 真实帧（preserveDrawingBuffer=false 时截屏会拿到空帧）
python scripts/capture_frame.py --out-dir shots --port 8765
#   页面侧：
#   fetch('http://127.0.0.1:8765/shot?name=f01', {method:'POST',
#         body: JSON.stringify({dataUrl: canvas.toDataURL('image/png')})});

# 3. A/B 并排对比 + 关键区域裁切放大
python scripts/crop_compare.py --left before.png --right after.png \
    --region 30,40,70,80 --scale 3 --out ab.png --sep magenta

# 4. 会话 token 成本画像
python scripts/budget_report.py --input transcript.jsonl --out report.md
```

---

## 目录结构

```
agent-workflow-skill/
├── SKILL.md                        # 主文档（入口）—— 快速扫读即可
├── references/
│   ├── visual-verification.md     # 视觉验收细则：怎么截、截哪里、怎么判断
│   ├── task-hygiene.md             # 任务清单裁剪、agent 生命周期、交付纪律
│   ├── token-discipline.md         # token 预算、消耗大户、工具选择决策树
│   └── perf-playbook.md            # 性能排查手册（GPU 同步、shader 编译、加载期标定）
├── scripts/
│   ├── check_syntax.mjs            # 校验 HTML 内联 module script 语法
│   ├── capture_frame.py            # 本地 HTTP 接收端，抓 WebGL 真实帧
│   ├── crop_compare.py             # A/B 对比 + 裁切放大
│   └── budget_report.py            # token 成本画像报告
├── LICENSE                         # MIT
└── .gitignore
```

**设计取向**：主文档保持可快速扫读，具体技术细节放 `references/`，可执行工具放 `scripts/`。
所有脚本路径均参数化，无任何硬编码绝对路径。

---

## 与本机既有资产的关系

本 skill 与 `~/.cursor/skills/engineering-workflow/`、`~/.cursor/rules/token-efficiency-and-visual-verification.mdc`、
`~/.cursor/agent-handbooks/02-agent-collaboration-rules.md` 是**互补而非重复**的关系：

- 前两者提供**规则**（自动生效、不可分发、彼此分散）—— 本 skill 是**包**（自包含、可独立分发、有配套脚本）。
- 本 skill **新增**了两块既有资产未覆盖的内容：**任务授权边界**（清单逐项确认）与 **agent 生命周期与并发纪律**。

详细对照表见 `SKILL.md` 的「与既有本地资产的关系」一节。

---

## 许可

MIT —— 见 [LICENSE](LICENSE)。
