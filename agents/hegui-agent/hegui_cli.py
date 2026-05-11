#!/usr/bin/env python3
"""Government procurement compliance review production-line executor."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def read_simple_yaml_value(path: Path, key: str) -> str | None:
    if not path.is_file():
        return None
    prefix = f"{key}:"
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith(prefix):
            value = stripped[len(prefix) :].strip()
            return value.strip("\"'")
    return None


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot_tree(root: Path, out: Path) -> None:
    rows: list[str] = []
    for path in sorted(root.rglob("*")):
        relative_parts = path.relative_to(root).parts
        if any(part.startswith(".") for part in relative_parts) or not path.is_file():
            continue
        stat = path.stat()
        relative = path.relative_to(root).as_posix()
        rows.append(f"{file_digest(path)}  {relative}")
        rows.append(f"{relative} {stat.st_size} {int(stat.st_mtime)}")
    out.write_text("\n".join(rows) + "\n", encoding="utf-8")


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def read_auth_key(codex_home: Path) -> str:
    auth_file = codex_home / "auth.json"
    if not auth_file.is_file():
        return ""
    data = json.loads(auth_file.read_text(encoding="utf-8"))
    return str(data.get("OPENAI_API_KEY") or "")


def read_toml_string(path: Path, key: str) -> str:
    if not path.is_file():
        return ""
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=\s*['\"]([^'\"]+)['\"]")
    for line in path.read_text(encoding="utf-8").splitlines():
        match = pattern.match(line)
        if match:
            return match.group(1)
    return ""


def extract_file_text(target_path: Path) -> str:
    suffix = target_path.suffix.lower()
    if suffix == ".pdf":
        result = subprocess.run(
            ["pdftotext", str(target_path), "-"],
            check=True,
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return result.stdout
    if suffix in {".doc", ".docx"}:
        result = subprocess.run(
            ["textutil", "-convert", "txt", "-stdout", str(target_path)],
            check=True,
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return result.stdout
    if suffix in {".txt", ".md"}:
        return target_path.read_text(encoding="utf-8")
    raise ValueError(f"unsupported file type: {suffix}")


def line_number_text(text: str) -> str:
    return "\n".join(f"{index:04d}: {line}" for index, line in enumerate(text.splitlines(), start=1))


def count_risks(report: str) -> int:
    patterns = [
        r"^#{2,3}\s+风险\s*\d+",
        r"^#{2,3}\s+\d+[.．、]\s+",
        r"^###\s+风险\s*\d+[.：:]",
    ]
    starts: set[int] = set()
    for pattern in patterns:
        for match in re.finditer(pattern, report, flags=re.MULTILINE):
            starts.add(match.start())
    return len(starts)


def output_category(target: str) -> str:
    parts = Path(target).parts
    if len(parts) >= 2 and parts[0] == "raw":
        return parts[1]
    return "通用"


def make_run_output_dir(biz_home: Path, category: str, now: datetime) -> tuple[Path, str]:
    timestamp = now.strftime("%Y-%m-%d_%H-%M-%S")
    base_rel = Path("outputs") / category / timestamp
    output_dir = biz_home / base_rel
    if not output_dir.exists():
        output_dir.mkdir(parents=True, exist_ok=False)
        return output_dir, base_rel.as_posix()

    for index in range(1, 100):
        rel = Path("outputs") / category / f"{timestamp}-{index:02d}"
        output_dir = biz_home / rel
        if not output_dir.exists():
            output_dir.mkdir(parents=True, exist_ok=False)
            return output_dir, rel.as_posix()
    raise RuntimeError(f"cannot allocate output directory for {timestamp}")


def collect_direct_knowledge(wiki_home: Path, category: str) -> str:
    pages = [
        "wiki/00-入口/外部执行主体招标文件审查指引.md",
        "wiki/70-审查协议/知识驱动审查执行规范.md",
        "wiki/70-审查协议/政府采购招标文件业务审查流水线.md",
        "wiki/70-审查协议/政府采购招标文件审查协议.md",
        "wiki/20-知识点/政府采购招标文件画像.md",
        "wiki/20-知识点/知识分层与路由规则.md",
        "wiki/20-知识点/政府采购逐章审查矩阵.md",
        "wiki/70-审查协议/风险原子化规则.md",
        "wiki/70-审查协议/质量门规则.md",
        "wiki/15-行业基础/政府采购专项场景画像.md",
        "wiki/25-风险审查点/风险审查点总览.md",
        "wiki/90-模板/审查记录模板.md",
        "wiki/90-模板/AI调度运行记录模板.md",
    ]
    if category == "物业管理":
        pages.extend(
            [
                "wiki/20-知识点/物业管理动作化审查包.md",
                "wiki/70-审查协议/物业管理审查动作协议.md",
                "wiki/15-行业基础/物业管理服务采购背景.md",
            ]
        )
    chunks: list[str] = []
    for rel in pages:
        path = wiki_home / rel
        if path.is_file():
            chunks.append(f"\n\n# {rel}\n\n{path.read_text(encoding='utf-8')}")
    if category != "物业管理":
        for folder in ("wiki/25-风险审查点", "wiki/10-法规依据"):
            root = wiki_home / folder
            if root.is_dir():
                for path in sorted(root.glob("*.md")):
                    rel = path.relative_to(wiki_home).as_posix()
                    chunks.append(f"\n\n# {rel}\n\n{path.read_text(encoding='utf-8')}")
    return "".join(chunks)


def read_wiki_page(wiki_home: Path, rel: str) -> str:
    path = wiki_home / rel
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def read_wiki_pages(wiki_home: Path, pages: list[str]) -> str:
    chunks: list[str] = []
    for rel in pages:
        text = read_wiki_page(wiki_home, rel)
        if text:
            chunks.append(f"\n\n# {rel}\n\n{text}")
    return "".join(chunks)


def risk_review_point_catalog(wiki_home: Path) -> str:
    root = wiki_home / "wiki/25-风险审查点"
    if not root.is_dir():
        return ""
    rows: list[str] = []
    for path in sorted(root.glob("*.md")):
        rel = path.relative_to(wiki_home).as_posix()
        title = path.stem
        rows.append(f"- [[{rel}]] {title}")
    return "\n".join(rows)


def law_catalog(wiki_home: Path) -> str:
    root = wiki_home / "wiki/10-法规依据"
    if not root.is_dir():
        return ""
    rows: list[str] = []
    for path in sorted(root.glob("*.md")):
        rel = path.relative_to(wiki_home).as_posix()
        title = path.stem
        rows.append(f"- [[{rel}]] {title}")
    return "\n".join(rows)


def stage_file(path: Path, title: str, content: str) -> None:
    path.write_text(f"# {title}\n\n{content.rstrip()}\n", encoding="utf-8")


def chat_text(base_url: str, api_key: str, model: str, prompt: str, max_tokens: int = 8000) -> tuple[str, dict]:
    chat = post_chat_completion(base_url, api_key, model, prompt, max_tokens=max_tokens)
    message = (chat.get("choices") or [{}])[0].get("message") or {}
    return str(message.get("content") or "").strip(), chat.get("usage") or {}


def post_chat_completion(base_url: str, api_key: str, model: str, prompt: str, max_tokens: int = 16000) -> dict:
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "你是政府采购招标文件合规审查生产线。只输出审查报告 Markdown，不要输出解释性前言。",
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "max_tokens": max_tokens,
        "stream": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    request = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=900) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"chat completion failed: {exc.code} {detail}") from exc


def direct_chat_review(
    target: str,
    target_path: Path,
    biz_home: Path,
    wiki_home: Path,
    codex_home: Path,
) -> int:
    config_toml = codex_home / "config.toml"
    base_url = read_toml_string(config_toml, "base_url")
    model = read_toml_string(config_toml, "model")
    api_key = read_auth_key(codex_home)
    if not base_url or not model or not api_key:
        print("direct chat config incomplete", file=sys.stderr)
        return 1

    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    review_date = now.strftime("%Y-%m-%d")
    review_time = now.strftime("%H:%M:%S CST")
    category = output_category(target)
    output_dir, output_rel_dir = make_run_output_dir(biz_home, category, now)

    stem = target_path.stem
    extract_rel = f"{output_rel_dir}/{stem}-抽取文本.txt"
    report_rel = f"{output_rel_dir}/{stem}-审查报告.md"
    run_rel = f"{output_rel_dir}/{stem}-AI调度运行记录.md"
    extract_path = biz_home / extract_rel
    report_path = biz_home / report_rel
    run_path = biz_home / run_rel

    raw_text = extract_file_text(target_path)
    numbered_text = line_number_text(raw_text)
    extract_path.write_text(numbered_text + "\n", encoding="utf-8")

    knowledge = collect_direct_knowledge(wiki_home, category)
    risk_catalog = risk_review_point_catalog(wiki_home)
    laws = law_catalog(wiki_home)
    report_prompt = f"""请对待审政府采购文件执行一次完整合规性审查，并直接输出最终审查报告 Markdown。

你只能使用本提示中提供的 LLM Wiki 知识、风险审查点目录、法规依据目录和待审文件原文。不得读取外部标注、标准答案、历史审查记录，不得修改 LLM Wiki，不得对 LLM Wiki 运行维护命令。

请在内部按照 LLM Wiki 要求完成：文件画像、知识路由、动作清单、逐动作执行、风险原子化、质量门反查、报告生成。最终只输出审查报告，不要输出解释性前言。

报告顶部必须包含：
类型:: 审查记录
状态:: AI 审查版
审查日期:: {review_date}
审查时间:: {review_time}
审查人:: AI 审查
外部标注使用:: 否
LLM Wiki修改:: 否
LLM Wiki维护命令:: 否

报告必须包含：
1. 审查摘要
2. 文件画像
3. 知识路由和动作状态
4. 风险点清单
5. 已审查未列风险
6. 待补证/待确认
7. 文件位置
8. 质量门结果

文件位置只能使用以下相对路径：
- 原始文件：{target}
- 抽取文本：{extract_rel}
- 运行记录：{run_rel}

每条风险必须保留：结论类型、风险等级、原文位置、原文内容、问题说明、关联动作ID、关联审查点、审查依据、修改建议、是否需要人工复核。
风险标题必须统一使用三级标题格式：### 风险 1：风险标题、### 风险 2：风险标题。
风险必须根据 LLM Wiki 风险审查点和待审文件原文判断；不得把执行器当作审查知识来源。
原文内容只能是原文摘录。
不得出现绝对路径。

LLM Wiki 知识：
{knowledge}

风险审查点目录：
{risk_catalog}

法规依据目录：
{laws}

待审文件，已加行号：
{numbered_text}
"""
    report, usage = chat_text(base_url, api_key, model, report_prompt, max_tokens=16000)
    if not report:
        print("pipeline returned empty report", file=sys.stderr)
        return 1

    report_path.write_text(report.rstrip() + "\n", encoding="utf-8")
    risk_count = count_risks(report)

    run_record = f"""类型:: AI调度运行记录
状态:: 已完成
项目名称:: {stem}
执行日期:: {review_date}
执行时间:: {review_time}
执行人:: AI 审查
外部标注使用:: 否
LLM Wiki修改:: 否
LLM Wiki维护命令:: 否

# {stem} - AI调度运行记录

## 1. 基本信息

| 字段 | 内容 |
| --- | --- |
| 原始文件 | {target} |
| 抽取文本 | {extract_rel} |
| 审查报告 | {report_rel} |
| 运行记录 | {run_rel} |
| 本次输出目录 | {output_rel_dir} |
| 文件分类 | {category} |
| 运行模式 | hegui_cli.py 单次知识驱动审查 |
| 运行时LLM | {model} |
| 风险点数量 | {risk_count} |

## 2. 执行边界

- 只读取本项目文件、本次指定招标文件，以及 config/hegui.yaml 中 wiki_home 指向的只读 LLM Wiki。
- 未读取外部标注、标准答案、人工批注或同一项目既有审查记录。
- 未修改 raw/ 下原始文件。
- 未修改 LLM Wiki。
- 未对 LLM Wiki 运行 ./lint、./ingest、./query 或其他维护命令。

## 3. 调用知识清单

- wiki/00-入口/外部执行主体招标文件审查指引.md
- wiki/70-审查协议/知识驱动审查执行规范.md
- wiki/70-审查协议/政府采购招标文件业务审查流水线.md
- wiki/70-审查协议/政府采购招标文件审查协议.md
- wiki/20-知识点/政府采购招标文件画像.md
- wiki/20-知识点/知识分层与路由规则.md
- wiki/20-知识点/政府采购逐章审查矩阵.md
- wiki/20-知识点/物业管理动作化审查包.md
- wiki/70-审查协议/物业管理审查动作协议.md
- wiki/70-审查协议/风险原子化规则.md
- wiki/70-审查协议/质量门规则.md
- wiki/15-行业基础/物业管理服务采购背景.md
- wiki/15-行业基础/政府采购专项场景画像.md
- wiki/25-风险审查点/风险审查点总览.md
- wiki/90-模板/审查记录模板.md
- wiki/90-模板/AI调度运行记录模板.md

## 4. 模型交互说明

本次直接调用 LLM 完成一次知识驱动审查，返回用量：

| 阶段 | prompt_tokens | completion_tokens | total_tokens |
| --- | ---: | ---: | ---: |
| 单次审查 | {usage.get('prompt_tokens', '')} | {usage.get('completion_tokens', '')} | {usage.get('total_tokens', '')} |

## 5. 质量门结果

- 已要求模型按 LLM Wiki 中的质量门规则执行反查。
- 风险点数量：{risk_count}。
- 外部标注使用：否。
- LLM Wiki修改：否。
- LLM Wiki维护命令：否。
"""
    run_path.write_text(run_record, encoding="utf-8")

    print(f"审查报告路径: {report_rel}")
    print(f"运行记录路径: {run_rel}")
    print(f"风险点数量: {risk_count}")
    print("是否使用外部标注: 否")
    print("是否修改 LLM Wiki: 否")
    print("是否对 LLM Wiki 运行维护命令: 否")
    print("质量门结果: 已要求执行，详见审查报告和运行记录")
    return 0


def build_prompt(agent_dir: Path, target: str, output_root: str = "outputs") -> str:
    category = "由文件画像和知识路由自动判定"
    review_date = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
    system_prompt = read_text(agent_dir / "prompts/system.md")
    task_prompt = read_text(agent_dir / "prompts/review.md")
    run_record_prompt = read_text(agent_dir / "prompts/run-record.md")
    quality_prompt = read_text(agent_dir / "prompts/quality-check.md")
    manifest = read_text(agent_dir / "manifests/review.yaml")

    task_prompt = task_prompt.replace("{{FILE_PATH}}", target)
    task_prompt = task_prompt.replace("{{CATEGORY}}", category)
    task_prompt = task_prompt.replace("{{REVIEW_DATE}}", review_date)
    task_prompt = task_prompt.replace(
        "{{WORKSPACE}}", "本项目文件和 config/hegui.yaml 中 wiki_home 指向的只读 LLM Wiki"
    )
    task_prompt = task_prompt.replace("{{OUTPUT_ROOT}}", output_root)
    system_prompt = system_prompt.replace("{{REVIEW_DATE}}", review_date)

    return f"""{system_prompt}

---

{task_prompt}

---

{run_record_prompt}

---

{quality_prompt}

---

# 执行 Manifest

```yaml
{manifest}
```

---

# 本次任务

请审查文件：`{target}`
文件分类：`{category}`
本次输出目录：`{output_root}`

请严格按上述协议执行。所有产物必须写入本次输出目录，不得写入本次输出目录之外，也不得覆盖历史报告。报告和运行记录中不得出现绝对路径。完成后只汇报审查报告路径、运行记录路径、风险点数量、是否使用外部标注、是否修改 LLM Wiki、是否对 LLM Wiki 运行维护命令。
"""


def runtime_env(runtime_home: Path, codex_home: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["HOME"] = str(runtime_home)
    env["CODEX_HOME"] = str(codex_home)

    # macOS may not provide C.UTF-8; force a locale known to exist on the target dev machines.
    for key in ("LC_ALL", "LC_CTYPE", "LANG"):
        if env.get(key) in (None, "", "C", "C.UTF-8"):
            env[key] = "zh_CN.UTF-8"
    return env


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="hegui_cli.py",
        description="审查一个政府采购招标文件，并输出审查报告和运行记录。",
    )
    parser.add_argument("raw_file", help="待审查文件路径")
    return parser.parse_args(argv)


def resolve_target(raw_file: str, biz_home: Path, wiki_home: Path) -> tuple[str, Path] | str:
    target_path = Path(raw_file)
    if target_path.is_absolute():
        resolved = target_path.resolve()
        for root in (biz_home.resolve(), wiki_home.resolve()):
            try:
                rel = resolved.relative_to(root)
            except ValueError:
                continue
            if resolved.is_file():
                return rel.as_posix(), resolved
        return "target must be inside the business project or read-only LLM Wiki"

    if ".." in target_path.parts:
        return "target must be inside the business project or read-only LLM Wiki"
    biz_target = biz_home / target_path
    if biz_target.is_file():
        return target_path.as_posix(), biz_target
    wiki_target = wiki_home / target_path
    if wiki_target.is_file():
        return target_path.as_posix(), wiki_target
    return f"target not found: {raw_file}"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    target_input = args.raw_file

    agent_dir = Path(__file__).resolve().parent
    biz_home = agent_dir.parent.parent
    config_file = biz_home / "config/hegui.yaml"
    configured_wiki = read_simple_yaml_value(config_file, "wiki_home")
    wiki_home = Path(configured_wiki) if configured_wiki else biz_home.parent / "lab-hegui-llm"
    output_root = biz_home / "outputs"
    codex_bin = biz_home / "runtime/bin/core-proxy-cli"
    codex_home = biz_home / "runtime/config"
    runtime_home = biz_home / "runtime/home"

    if not wiki_home.is_dir():
        print("wiki home not found", file=sys.stderr)
        return 1
    wiki_home = wiki_home.resolve()

    output_root.mkdir(parents=True, exist_ok=True)
    codex_home.mkdir(parents=True, exist_ok=True)
    runtime_home.mkdir(parents=True, exist_ok=True)

    if not codex_bin.is_file() or not os.access(codex_bin, os.X_OK):
        print("runtime codex not found", file=sys.stderr)
        return 1

    resolved_target = resolve_target(target_input, biz_home, wiki_home)
    if isinstance(resolved_target, str):
        print(resolved_target, file=sys.stderr)
        return 1
    target, target_path = resolved_target

    if os.environ.get("HEGUI_DIRECT_CHAT") == "1":
        return direct_chat_review(target, target_path, biz_home, wiki_home, codex_home)

    category = output_category(target)
    _, output_rel_dir = make_run_output_dir(biz_home, category, datetime.now(ZoneInfo("Asia/Shanghai")))
    prompt = build_prompt(agent_dir, target, output_rel_dir)

    before = tempfile.NamedTemporaryFile(prefix="hegui-wiki-before.", delete=False)
    after = tempfile.NamedTemporaryFile(prefix="hegui-wiki-after.", delete=False)
    before_path = Path(before.name)
    after_path = Path(after.name)
    before.close()
    after.close()

    try:
        snapshot_tree(wiki_home, before_path)
        result = subprocess.run(
            [
                str(codex_bin),
                "exec",
                "-C",
                str(biz_home),
                "--skip-git-repo-check",
                "--ignore-rules",
                "-s",
                "workspace-write",
                "-",
            ],
            input=prompt,
            text=True,
            encoding="utf-8",
            env=runtime_env(runtime_home, codex_home),
        )
        if result.returncode != 0:
            print(f"review runtime failed with exit code {result.returncode}", file=sys.stderr)
            return result.returncode
        snapshot_tree(wiki_home, after_path)
        if before_path.read_bytes() != after_path.read_bytes():
            print("error: LLM Wiki changed during review; outputs are not trusted", file=sys.stderr)
            return 1
    finally:
        before_path.unlink(missing_ok=True)
        after_path.unlink(missing_ok=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
