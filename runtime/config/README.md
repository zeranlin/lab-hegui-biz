# runtime/config

本目录是 `core-proxy-cli` 的独立 `CODEX_HOME`。

`hegui_cli.py` 默认以如下方式调用底层 CLI：

```bash
HOME=runtime/home CODEX_HOME=runtime/config runtime/bin/core-proxy-cli ...
```

因此隔离环境不会读取使用者本机的 `~/.codex`，也不会读取使用者本机 HOME 下的规则、技能和缓存。

`hegui_cli.py` 调用真实任务时会附加 `--ignore-rules`，避免加载使用者本机规则文件。不要使用 `--ignore-user-config`，否则底层 CLI 也会忽略本目录中的 `config.toml`。

## 配置文件

真实环境需要提供：

```text
runtime/config/config.toml
runtime/config/auth.json
```

`config.toml` 用于配置模型、provider、profile 和项目权限；`auth.json` 或环境变量用于鉴权。

本仓库只提供模板，不提交真实密钥。
