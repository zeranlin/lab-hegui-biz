# lab-hegui-biz

本项目是政府采购行业招标文件合规性审查生产线。

它不保存、不反哺长期知识资产。长期知识库位于 `lab-hegui-llm`，本项目通过 `config/hegui.yaml` 指向该只读 LLM Wiki。

## 目录

```text
agents/hegui-agent/      # hegui_cli.py、提示词和 manifest
outputs/                 # 审查报告和运行记录输出目录
config/hegui.yaml        # 指向 LLM Wiki 的配置
config/config.toml       # LLM 服务和模型配置
config/auth.json         # 本地鉴权配置
```

## 审查

```bash
python agents/hegui-agent/hegui_cli.py '<待审查文件相对路径>'
```

入口只接收待审查文件，文件画像、品类路由和专项动作由 LLM Wiki 审查协议决定。

默认要求 `lab-hegui-biz` 和 `lab-hegui-llm` 位于同一上级目录。如果知识库不在默认位置，可以设置 `HEGUI_WIKI_HOME` 指向只读 LLM Wiki。

## 产物

```text
outputs/物业管理/xxx-审查报告.md
outputs/物业管理/xxx-AI调度运行记录.md
```
