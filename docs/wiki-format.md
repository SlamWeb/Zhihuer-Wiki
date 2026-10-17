# Wiki 文件契约 V2

这是当前 Agent 直接构建和查阅的默认输出格式。Agent 负责语义提取、分类和组织；程序负责格式、引用、原话与文件完整性核验。无需先执行聚类或旧 V1 job 流程。

## 文件布局

```text
wiki/
  index.json                规范的两层导航及全部条目归属
  entries.json              规范条目数组
  sources.json              按 doc_id 索引的来源元数据
  sources/*.md              完整原文正文，保持原样
  index.md                  以下由 render 生成
  topics/<topic_id>.md
  pages/<page_id>.md         该页全部条目的完整卡片
  AGENTS.md                 搬走文件夹后的查阅入口
```

三个 JSON 与原文是规范资产，Markdown 导航是派生视图。修改归属后重新 render。V2 不要求每条一个文件，也不要求概览或合并全库的阅读版。完整可运行示例见[虚构样例](../samples/wiki-v2/index.json)，可同时检查其条目、元数据与原文文件。

## index.json

```json
{
  "schema": "zhihuer_wiki.navigation.v2",
  "author": {"id": "demo-author", "name": "虚构样例作者"},
  "topics": [
    {
      "id": "work",
      "title": "工作与选择",
      "scope": "职业选择、工作边界；关键词：工作、加班、拒绝",
      "pages": [
        {
          "id": "work-boundaries",
          "title": "什么情况下拒绝额外工作",
          "scope": "额外任务与时间边界；关键词：周末、临时任务、拒绝",
          "entry_ids": ["entry-0001"]
        }
      ]
    }
  ]
}
```

导航固定为主题 → 具体问题页两层；页引用稳定条目 ID。每个条目完整归属到一个主问题页，不因不同观点另建同题页。主题和页的 `scope` 写清对象、覆盖范围、边界及检索关键词；标题不能单独承担范围说明。

主题和页使用稳定的安全 ASCII ID：以字母或数字开头，只含字母、数字、下划线和连字符，不使用 Windows 保留名称。主题和页 ID 在整个导航中唯一（也不能仅大小写不同），所有页共用 `pages/`。简单小写字母、数字、连字符便于搬迁。

作者、条目与原文的逻辑 ID 不直接用于文件名，可以保留已有标识，按大小写精确引用且不含控制字符。不要为了目录整理重新编号来源或已存在的条目。

`doc_id` 是逻辑来源标识，不是文件名，可以保留原有身份（例如 `zhihu:answer:123`）。不要直接拿包含冒号的 doc_id 当 Windows 文件名；在 `sources.json` 的 `path` 中单独映射到安全路径，例如 `sources/zhihu-answer-123.md`。重组导航不改变来源身份。

## entries.json

```json
[
  {
    "entry_id": "entry-0001",
    "doc_id": "doc-0001",
    "published_at": null,
    "quote": "周末的临时任务，我会先问清楚为什么不能等到周一。",
    "paraphrase": "面对周末临时任务，先确认其紧迫性。",
    "context": "虚构演示句；针对周末临时工作，不等于拒绝所有额外工作。"
  }
]
```

`quote` 是对应原文中可精确匹配的连续原话，不能改字、拼接省略号或用转述代替。需要不连续证据时保留为独立条目或选择包含必要语境的连续原文。`context` 保留对象、条件、例外、引用关系及不确定性；`paraphrase` 不加强原话。每条指向真实来源；不同时间、条件或相反观点保留独立 ID。`published_at` 为 ISO 日期/时间字符串或 JSON `null`，与对应来源元数据一致，不编造缺失日期。

## sources.json 与原文

```json
{
  "doc-0001": {
    "doc_id": "doc-0001",
    "title": "虚构样例：周末工作",
    "published_at": null,
    "url": "",
    "path": "sources/doc-0001.md",
    "text_sha256": "<完整原文文件字节的 64 位小写 SHA-256>"
  }
}
```

哈希是说明占位符，不能直接通过核验；实际值参见完整样例。`url` 无来源链接时用空字符串，不捏造地址。字典键与记录内 `doc_id` 一致。`path` 是相对 Wiki 根目录的 `sources/<安全文件名>.md`；文件名以 ASCII 字母或数字开头，只含字母、数字、下划线、连字符和点，以 `.md` 结尾，不使用 Windows 保留名称。来源路径不能仅大小写不同。

每份来源正文以 UTF-8 原样保存，包含原 Markdown；元数据放在 `sources.json`，不要给原文另加 frontmatter、标题、日期说明或摘要。`text_sha256` 是**文件实际 UTF-8 字节**的 SHA-256；换行、BOM、空格变化都会影响哈希。导入已有 UTF-8 Markdown 可直接复制字节。先保存完整源文件，再从这份文件提取引文和计算哈希。不能通过改原文让捏造引文通过核验。

## 渲染、核验与含义边界

从项目根目录运行，Python 标准库即可，无需安装包或调用模型：

```powershell
python scripts/wiki_folder.py render --wiki ./outputs/demo/wiki
python scripts/wiki_folder.py validate --wiki ./outputs/demo/wiki
```

渲染生成导航与完整条目卡，包括原话、转述、条件、doc_id、标题、日期及原文链接。核验检查引用、归属、路径、日期、哈希、引文与派生文件。失败时保留工作稿，按报告修复后重跑；通过前不宣称发布完成。

精确匹配只能证明“文字存在于这份原文”，不能证明本人立场、语境理解、语义覆盖或分类合理。Agent 仍需审读遗漏、引述对象、反讽和历史差异。页数、条目数及核验通过均不构成生成质量或 RAG 收益证据。
