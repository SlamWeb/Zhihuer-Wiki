# 从知乎主页到 Persona Wiki

## 两个执行入口，同一份任务

`build USER --agent codex` 抓取后通过已登录的 Codex CLI 执行每个模型任务。
`build USER --agent manual` 只准备任务，由当前 Agent 读取输入、完成推理并提交。
两者共用批次、Schema、原话核验和导出器；没有任何 LLM HTTP provider 或 API key 配置。

## 1. 抓取与快照

回答、文章和想法先落成 Markdown、profile.json 和 manifest.jsonl。公开端点优先，
必要时浏览器 fallback 使用本地登录状态。`--max-items` 是全局篇数上限。
缺失类型或平台限制记录在 crawl-report.json；不把“抓到了几篇”写成“抓到了全量”。
显示昵称不是唯一身份，输入主页的 URL token。

构建在 `evidence/` 保存原文快照，`.work/` 保存来源元数据与任务状态。内部 doc ID 用于链接，原始知乎
content ID、URL、发布时间和更新日期留在来源记录。长文只是切任务输入，完整原文不截尾。
同一工作区保持输入快照不变；新材料使用新的工作区，本版还不做局部语义更新。

## 2. 完整提取条目

默认一批最多十篇、约20k估算输入token。达到任一限制就切批，长文可跨批。
并行性属于独立任务，不要求先知道上一批的主题目录。每个文档分段必须明确返回
处理结果，即使没有可提炼条目也返回空列表，不允许静默遗漏。

每条条目保留：

- 转述，具体而不磨平人物表达。
- 原样引用的一段或几段原话。
- 原文doc ID、时间、条件/语境、topic_hint。
- 一个可引用的条目ID。

程序核对引文能在对应原文找到，不让模型把改写后的句子冒充原话。作者引用别人、
讽刺、假设和否定不应误当自己的立场。此语义判断仍由Agent负责，字符串核验不能替代。

## 3. 候选预分组与完整细分类

小于或等于200条直接细分类。较大语料先用本地多语言embedding与KMeans
形成候选堆；模型可替换为本地路径。每个候选堆继续切成最多100条、约20k输入token的任务。
输入包括每条的原话与条件，不是只看代表条目。

`--grouping tfidf` 明确使用字符TF-IDF词面向量；`none`跳过候选聚类。
实际后端写入检查点，失败不会偷偷换成其他方式。K不是最终主题数。

Agent按具体对象或问题划分子页，可拆分候选簇。不凑十页、不均衡人数、不因为
立场相反而另建一个问题页。每条恰好一个主归属，次级入口引用同一个ID。
时间不明标记未知，不凭较晚就断言人物改口。

## 4. 目录与概览

全局整理把完整子页挂在大主题下面，不删除或合并原始条目；同一父主题下的
几个具体问题保持独立。目录超过当前预算时保留工作区并报告，不能截断后冒充全量。
跨批出现同义子页时，本版允许多个页并列，后续再做带证据的合并。

概览按原始条目生成，每条概括关联支持它的条目ID。大语料分批产生来源绑定的
概括集合，不宣称它已经是一份经全局冲突裁定的完整人格。原始条目与时间差异仍可阅读。

## 5. 文件与链接

```text
outputs/<作者>/
  raw/                         爬取结果，原文 Markdown
  crawl-report.json            本次抓取范围与限制
  .work/
    state.json                 阶段、设置和接受记录
    jobs/                      prompt、input、schema、response
  evidence/                    不变的原文快照、接受响应、规范条目
  derived/                     候选分组、子页、主题与概览中间产物
  wiki/
    index.md                   人物与主题入口
    overview.md                带条目依据的人物概括
    topics/                    父级主题，指向具体子页
    subpages/                  具体问题，按日期展示条目入口
    entries/                   转述、原话、条件与原文链接
    sources/                   原文档案及来源元数据
    readable.md                可直接阅读的合并版本
```

Markdown相对链接随整个wiki文件夹搬迁。一个原文可能支持多个条目，原文旁的
`<doc_id>-links.md`提供这些条目的回链；原文档案保持完整。从感兴趣的原话可读原文，
再沿来源关联读其他条目。
查阅时由Agent使用现有文件工具逐层导航，具体规则见[查阅说明](reading.md)。
本项目没有内置独立搜索运行器；取证与停止由当前Agent按说明判断。

阅读版保留原话和时间供模型参考；它不是带随机样本的在线浏览器，也不保证
任何规模都适合一次放进模型上下文。完整产物始终保留在文件里。

## 文件任务协议

```powershell
python -m zhihuer_wiki next --workspace outputs/demo
python -m zhihuer_wiki submit --workspace outputs/demo --job extract-0001 --response RESPONSE.json
python -m zhihuer_wiki status --workspace outputs/demo
python -m zhihuer_wiki validate --workspace outputs/demo
```

`next`提供prompt_path、input_path、schema_path、response_path；Agent完整读输入后
写响应。Schema约束有效ID及字段；validator再检查内容和全覆盖。相同已接受结果
重复提交不产生新的条目；不同结果不能覆盖已接受任务。失败响应保留，任务可修正重试。
最终全部核验通过才发布Wiki；未完成时不交付“完成”状态。

## 来源

爬虫改编自 [PersonClone](https://github.com/SlamWeb/PersonClone) 的公开API、Playwright
fallback和Markdown模块，作为独立副本分发，运行时不依赖PersonClone安装。
离线原文档案、人物知识页和按需展开的组织思路参考
[Karpathy 的 LLM Wiki 构想](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)。
此项目是面向人物材料的实现取舍，不把这些构想当成已验证的人物模拟质量保证。
