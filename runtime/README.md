# runtime

本目录用于 `lab-hegui-biz` 的隔离环境运行时依赖。

目标状态：`hegui_cli.py` 是业务入口，Codex 是 `hegui_cli.py` 调用的 CLI 可执行运行时，在隔离环境中优先调用本目录下的 Codex CLI。知识库不放在本目录，由 `config/hegui.yaml` 指向 `lab-hegui-llm`。

推荐放置位置：

```text
runtime/
  bin/
    core-proxy-cli
```

`runtime/bin/core-proxy-cli` 可以是实际二进制，也可以是指向隔离环境内 Codex CLI 的可执行脚本。

当前项目按隔离部署目标只保留最小运行时：

```text
runtime/bin/core-proxy-cli      # hegui_cli.py 优先调用的执行入口
runtime/config/                 # 独立 CODEX_HOME 配置目录
runtime/home/                   # 独立 HOME，运行时自动创建
runtime/VERSION                 # 来源和版本信息
```

`hegui_cli.py` 默认使用：

```bash
CODEX_HOME=runtime/config
HOME=runtime/home
```

隔离环境需要将实际 `config.toml` 和鉴权信息放入 `runtime/config/`，不要依赖使用者本机 `~/.codex`。`runtime/home/` 用于阻断底层 CLI 读取使用者本机 HOME 下的规则、技能和缓存。

打包隔离环境时，应使用：

```bash
agents/hegui-agent/bin/package-isolated
```

不要使用只打包版本控制文件的方式作为隔离包来源，否则可能遗漏本地 runtime 二进制或未纳入版本控制的大文件。
