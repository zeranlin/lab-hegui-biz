# hegui-agent

`hegui-agent` 是政府采购行业招标文件合规性审查生产线的 Agent 封装层。

它只做一件事：接收一个待审查招标文件，读取 `config/hegui.yaml` 指向的只读 LLM Wiki 知识，调用隔离 Codex 运行时完成审查，并把审查报告和运行记录输出到 `outputs/`。

## 使用方式

```bash
agents/hegui-agent/hegui_cli.py '<待审查文件相对路径>'
```

待审查文件路径基于 `config/hegui.yaml` 中的 `wiki_home` 解析。执行器只校验文件存在于只读 LLM Wiki 内，文件画像、品类路由和专项动作由 LLM Wiki 审查协议决定。

## 运行时

`hegui_cli.py` 默认使用独立运行时：

```text
runtime/bin/core-proxy-cli
runtime/config
runtime/home
```

隔离环境需要将真实模型配置和鉴权信息放入 `runtime/config/`。业务审查不依赖使用者本机的 Codex 配置、规则、技能和缓存。

## 输出

```text
outputs/对应分类/xxx-审查报告.md
outputs/对应分类/xxx-AI调度运行记录.md
```

## V0.1 范围

- 只支持单文件招标文件审查。
- 只读取 `lab-hegui-llm` 既有知识。
- 不修改、不更新、不反哺 LLM Wiki。
- 不生成知识更新申请、人工复盘记录或来源笔记。
- 必须生成审查报告和 AI 调度运行记录。
- 不对 LLM Wiki 运行 `./lint`、`./ingest`、`./query` 等维护命令。
- 不得修改 `raw/招标文件/` 原始文件。
- 不得使用外部 V2、标准答案或人工标注反推风险点。
