<h1 align="center">Zhihuer-Wiki</h1>

<p align="center"><strong>把一个知乎博主，变成一份人物 Wiki。</strong></p>
<p align="center">让你已有的 Agent 组织材料，交付能直接读、搜和追溯原文的文件夹。</p>

![知乎主页、原文与带出处的人物 Wiki](docs/assets/cover.png)

**推荐入口已改为 Agent 直接建库。** 给 Agent 材料和文件规范，由它理解、归类和整理；项目提供可搬走的格式、虚构示例和校验工具。聚类、向量数据库、额外的模型服务都不是这个入口的前提。

人物 → 主题 → 子页 → 完整条目与原文入口。**打开子页就能看到这一页的全部条目、doc ID、日期和原文链接**，无需为了拿到出处再逐个打开条目文件。每篇完整原文独立保存，转述不能取代原文。

## 交给你正在使用的 Agent

```powershell
git clone https://github.com/SlamWeb/Zhihuer-Wiki.git
cd Zhihuer-Wiki
```

在这个文件夹打开 Codex 或其他能读写文件的 Agent，发送：

```text
读取 .agents/skills/zhihuer-wiki/SKILL.md。
把 https://www.zhihu.com/people/你的URL-token 的公开材料整理为人物 Wiki，
按 docs/wiki-format.md 的 V2 规范输出到 outputs/你的URL-token/wiki。
先处理最多10篇原文，保留完整来源、日期、代表原话和适用条件。
分类前先理解整批材料；不要默认走旧版聚类任务，也不要每几个条目就新建一个碎页。
完成后生成阅读页、校验，并给我 index.md。
```

也可以直接提供已有 Markdown：

```text
读取这个仓库的 zhihuer-wiki Skill。
用 ./my-raw 下已有原文，按 V2 规范构建 ./outputs/demo/wiki。
由你完成条目提取和全局归类，保留完整原文；生成阅读页并校验。
```

这里使用的是**当前 Agent**，不额外启动一个模型运行器。Agent 的额度与模型使用成本仍取决于你自己的环境。

抓取可以复用本项目已有爬虫；已有材料可以跳过抓取。涉及登录时由用户在本机完成，登录态留在本机。详细步骤见 [Agent 建库说明](docs/build-with-agent.md)。

## 交付长什么样

```text
wiki/
  AGENTS.md                 搬走文件夹后仍能找到的查阅规则
  index.md                  人物入口，所有主题及范围
  index.json                主题、子页与条目归属
  entries.json              全部规范条目，稳定 entry_id / doc_id
  sources.json              原文目录：标题、日期、URL、路径、哈希
  topics/
    work.md                 该主题的全部子页与范围
  pages/
    choosing-work.md        所属条目的完整原话、转述、条件、日期、来源
  sources/
    source-0001.md          完整原文，正文不被摘要覆盖
```

`index.json`、`entries.json`、`sources.json` 是机器可核验的记录；Markdown 是它们的阅读视图。Agent 负责理解和分类，脚本负责检查引用与归属、生成一致的阅读页。

完整字段、最小 JSON 示例和验收要求见 [V2 文件规范](docs/wiki-format.md)。可以直接打开 [虚构 Wiki 示例](samples/wiki-v2/index.md) 看最终效果。

分类按材料规模和问题范围调整。大批材料先建立整体目录，再在后续批次复用、合并或补充；不按输入批次机械创建一组新主题。页数和每页条目数是阅读设计，不是必须填满的配额。

## 只用 Python 就能生成与校验阅读页

Agent 写好三份 JSON 和完整原文后运行：

```powershell
python scripts/wiki_folder.py render --wiki outputs/你的URL-token/wiki
python scripts/wiki_folder.py validate --wiki outputs/你的URL-token/wiki
```

这两个命令只需要 Python 3.11+ 标准库，不安装 embedding、不调用模型，也不做语义分类。可以先在公开虚构示例上核验：

```powershell
python scripts/wiki_folder.py validate --wiki samples/wiki-v2
```

校验会检查条目是否遗漏或重复归属、原话是否存在于对应全文、条目日期是否与来源记录一致、原文哈希及相对路径和阅读页是否有效。来源标题、日期和 URL 由 Agent 如实登记，脚本不会联网认证这些元数据，也不能证明分类和转述在语义上完全正确。

## 让 Agent 查阅或继续对话

```text
用 ./outputs/你的URL-token/wiki 回答我的问题。
先读 AGENTS.md 和 index.md，再打开相关主题、子页和需要的完整原文。
已经完整读过且仍在上下文中的材料可以复用，只为具体信息缺口继续找。
不要把目录、搜索回执、调试字段或检索过程塞进最终回答的参考材料。
```

普通文件读取、目录列表和文本搜索就够用，不需要独立工具服务器。Agent 可以直接跳到搜索命中的子页，不必每次走完全部层级。

多轮对话按当前任务判断：已有原文充分就复用；只缺一部分就补那部分；换问题时重新检查覆盖范围。改写与致谢通常不新增取证。**同一个大主题不等于材料足够，不要求每轮重凑原文篇数。**

需要生成回答时，选中的原文只携带有用的标题、日期与全文；检索日志、排名、内部状态留在工作记录里。需要出处时再保留相应文件引用。具体规则见 [查阅与多轮复用](docs/reading.md)。

## 旧版自动运行器

已有 V1 工作区仍可使用 `python run.py ...` 或 `python -m zhihuer_wiki build ...`，按 `next / submit / resume` 完成原来的任务。这个兼容入口仍有候选聚类与小批分类，导出 `subpages/`、`entries/` 和 `readable.md`；**它不会自动变成上面的 V2 格式**。

V1 的安装、抓取登录、断点恢复和原有参数见 [旧版工作流](docs/workflow.md)。使用 Agent 新建 V2 时，按本页和 Skill 的文件规范工作即可。

## 边界与验证

- 发布的是文件格式、Agent 工作说明与校验器。主题质量和取证决策依赖实际使用的 Agent。
- 原文、生成库、浏览器登录态和模型缓存留在用户本机。仓库只包含明确标注的虚构样例。
- 新资料可以由 Agent 在现有目录上整理，保留稳定 ID 与旧证据；在新版本文件夹校验后交付。旧的已核验原文不要原地覆盖。
- 格式校验通过不代表人物模拟优于 RAG，也不代表还原了作者本人。这里没有内置在线回答服务。

[文件规范](docs/wiki-format.md) · [Agent 建库](docs/build-with-agent.md) · [查阅规则](docs/reading.md) · [Agent Skill](.agents/skills/zhihuer-wiki/SKILL.md) · [项目契约](SPEC.md) · [验证记录](docs/verification.md)

爬虫和人物 Wiki 的工程来源：[PersonClone](https://github.com/SlamWeb/PersonClone)。
