# ppdocs 知识图谱 — AI 协作规范

本项目使用 ppdocs 知识图谱系统管理项目知识。你拥有一组 MCP 工具来访问项目的架构图谱、文档、任务和代码分析能力。

## 首次对话启动

每次新对话开始时，按以下顺序获取项目上下文：

```
1. kg_status()              → 项目概况（含架构摘要、活跃任务、核心模块列表）
2. kg_flowchart(get, "main") → 需要深入时查看完整架构图
3. kg_workflow()             → 查看可用的标准工作流
4. kg_task(get)              → 查看活跃任务详情
```

通常只需第 1 步就能获得足够的项目上下文开始工作。

## 工具速查

### 理解项目
| 工具 | 用途 |
|:---|:---|
| `kg_status()` | 一键获取项目全貌（推荐首选） |
| `kg_flowchart(get, chartId)` | 查看某张流程图的完整结构 |
| `kg_flowchart(get_node, nodeId, expand:2)` | 深入某个模块，含文档和绑定文件 |
| `kg_flowchart(search, query)` | 按关键词搜索流程图节点 |
| `kg_doc(search, query)` | 搜索节点内嵌文档 |
| `code_smart_context(symbolName)` | 获取代码符号的依赖和关联文档 |
| `code_full_path(symbolA, symbolB)` | 查找两个符号之间的关联路径 |

### 执行任务
| 工具 | 用途 |
|:---|:---|
| `kg_task(create, title, goals, bindTo)` | 开始新任务（绑定到流程图节点） |
| `kg_task(update, content, log_type)` | 记录进度/问题/方案（每步都要记录） |
| `kg_task(archive, summary, solutions)` | 完成任务并归档经验 |
| `kg_workflow(id)` | 获取标准工作流指导（如有匹配的工作流） |

### 回写知识
| 工具 | 用途 |
|:---|:---|
| `kg_flowchart(update_node, nodeId, docSummary, docContent)` | 更新节点文档 |
| `kg_flowchart(bind, nodeId, files:[...])` | 绑定源码文件到节点 |
| `kg_flowchart(batch_add, nodes, edges)` | 新增模块时批量创建节点 |
| `kg_flowchart(create_chart)` | 为复杂模块创建子流程图 |

### 协作与文件
| 工具 | 用途 |
|:---|:---|
| `kg_discuss(create/reply/list)` | 跨 AI 实例讨论 |
| `kg_meeting(join/post/status)` | 多 AI 协作会议 |
| `kg_files(list/read/download)` | 项目文件管理 |
| `kg_ref(list/get/save)` | 外部参考资料管理 |

## 核心原则

1. **先查图谱再看代码** — 在 grep/搜索代码之前，先用 `kg_flowchart(search)` 或 `kg_doc(search)` 查找相关节点，通常能直接定位到关键文件和逻辑说明，减少 80% 的盲目搜索。

2. **每完成一步就 update task** — 使用 `kg_task(update)` 记录每个阶段的进展，遇到问题用 `log_type:"issue"` 记录，找到方案用 `log_type:"solution"` 记录。

3. **改完代码必须回写图谱** — 修改了代码后，检查对应的流程图节点文档是否需要更新。新增文件要 `bind`，逻辑变更要 `update_node` 的 docContent。

4. **子图递归探索** — 节点如果有 `subFlowchart` 字段，说明它有更细粒度的子流程图。理解细节时要递归下探。

## 节点文档模板

创建或更新节点 `docContent` 时，推荐使用以下结构：

```markdown
## 职责
一句话说清本模块的核心职责。

## 输入 / 输出
- 输入: 进入本模块的数据或请求
- 输出: 本模块产出的数据或响应

## 核心逻辑
1. 步骤一
2. 步骤二
3. ...（3-7 步为宜）

## 边界条件
- 它不负责什么（明确排除）
- 错误处理策略

## 关键文件
- path/to/main.go — 主逻辑
- path/to/helper.go — 辅助函数
```

## 错误处理

| 场景 | 处理方式 |
|:---|:---|
| API 超时/网络错误 | 重试一次，仍失败则告知用户 |
| JSON 解析失败 | fallback 到纯文本输出 |
| 找不到节点 | 用 `kg_flowchart(search)` 模糊搜索 |
| 工具调用失败 | 检查参数格式，确保 JSON 数组/对象格式正确 |

## 输出格式

- 逻辑说明用 ASCII 流程图
- 对比分析用 Markdown 表格
- 代码引用注明文件路径和行号
- 方案说明要简洁，避免不必要的冗余
