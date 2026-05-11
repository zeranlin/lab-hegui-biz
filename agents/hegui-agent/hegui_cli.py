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
    return len(re.findall(r"^###\s+风险\s*\d+\s*[.．、：:]", report, flags=re.MULTILINE))


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


ENTRY_GUIDE = "wiki/00-入口/外部执行主体招标文件审查指引.md"


def normalize_wiki_ref(ref: str) -> str | None:
    ref = ref.split("|", 1)[0].split("#", 1)[0].strip()
    if not ref:
        return None
    if ref == "wiki/index":
        return "wiki/index.md"
    if ref.startswith("wiki/"):
        return ref if ref.endswith(".md") else f"{ref}.md"
    if ref == "AGENTS.md":
        return ref
    return None


def extract_wiki_refs(text: str) -> list[str]:
    refs: list[str] = []
    for match in re.finditer(r"\[\[([^\]]+)\]\]", text):
        ref = normalize_wiki_ref(match.group(1))
        if ref:
            refs.append(ref)
    for match in re.finditer(r"`((?:AGENTS\.md|wiki/[^`]+?)(?:\.md)?)`", text):
        ref = normalize_wiki_ref(match.group(1))
        if ref:
            refs.append(ref)
    return refs


def is_allowed_knowledge_page(rel: str) -> bool:
    if rel in {"AGENTS.md", "wiki/index.md", ENTRY_GUIDE}:
        return True
    allowed_prefixes = (
        "wiki/10-法规依据/",
        "wiki/15-行业基础/",
        "wiki/20-知识点/",
        "wiki/25-风险审查点/",
        "wiki/30-风险库/",
        "wiki/60-提示词/",
        "wiki/70-审查协议/",
        "wiki/90-模板/",
    )
    return rel.startswith(allowed_prefixes)


def collect_entry_driven_knowledge(wiki_home: Path, max_pages: int = 180) -> tuple[str, list[str]]:
    queue = ["AGENTS.md", "wiki/index.md", ENTRY_GUIDE]
    visited: set[str] = set()
    ordered: list[str] = []
    chunks: list[str] = []

    while queue and len(ordered) < max_pages:
        rel = queue.pop(0)
        if rel in visited:
            continue
        visited.add(rel)
        if not is_allowed_knowledge_page(rel):
            continue
        path = wiki_home / rel
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        ordered.append(rel)
        chunks.append(f"\n\n# {rel}\n\n{text}")
        if rel == "wiki/index.md":
            continue
        for next_ref in extract_wiki_refs(text):
            if next_ref not in visited and next_ref not in queue:
                queue.append(next_ref)

    return "".join(chunks), ordered


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
    profile_rel = f"{output_rel_dir}/{stem}-01-文件画像.md"
    route_rel = f"{output_rel_dir}/{stem}-02-知识路由表.md"
    actions_rel = f"{output_rel_dir}/{stem}-03-动作清单.md"
    action_exec_rel = f"{output_rel_dir}/{stem}-04-动作执行记录.md"
    atomized_rel = f"{output_rel_dir}/{stem}-05-原子风险清单.md"
    quality_rel = f"{output_rel_dir}/{stem}-06-质量门检查表.md"
    report_rel = f"{output_rel_dir}/{stem}-审查报告.md"
    run_rel = f"{output_rel_dir}/{stem}-AI调度运行记录.md"
    extract_path = biz_home / extract_rel
    profile_path = biz_home / profile_rel
    route_path = biz_home / route_rel
    actions_path = biz_home / actions_rel
    action_exec_path = biz_home / action_exec_rel
    atomized_path = biz_home / atomized_rel
    quality_path = biz_home / quality_rel
    report_path = biz_home / report_rel
    run_path = biz_home / run_rel

    raw_text = extract_file_text(target_path)
    numbered_text = line_number_text(raw_text)
    extract_path.write_text(numbered_text + "\n", encoding="utf-8")

    knowledge, knowledge_pages = collect_entry_driven_knowledge(wiki_home)
    usages: list[tuple[str, dict]] = []
    stage_paths = [
        ("01-文件画像", profile_rel),
        ("02-知识路由表", route_rel),
        ("03-动作清单", actions_rel),
        ("04-动作执行记录", action_exec_rel),
        ("05-原子风险清单", atomized_rel),
        ("06-质量门检查表", quality_rel),
        ("07-AI审查记录", report_rel),
        ("08-运行记录", run_rel),
    ]

    shared_context = f"""你是政府采购招标文件合规审查生产线的外部执行主体。

本次执行必须由 LLM Wiki 入口指引驱动：先读取入口指引，再按入口指引、流水线、执行规范、质量门和模板要求执行。
执行器只负责调度和落文件，不提供任何审查知识；风险判断只能来自 LLM Wiki 知识和待审文件原文。

边界：
- 不得使用外部标注、标准答案、人工批注或同一项目历史审查记录。
- 不得修改 LLM Wiki。
- 不得对 LLM Wiki 运行 ./lint、./ingest、./query 或其他维护命令。
- 报告和中间产物不得出现绝对路径。
- 原文内容字段只能放待审文件原文。

本次文件位置：
- 原始文件：{target}
- 抽取文本：{extract_rel}
- 输出目录：{output_rel_dir}

入口指引动态装载的 LLM Wiki 知识：
{knowledge}
"""

    profile_knowledge = read_wiki_pages(
        wiki_home,
        [
            ENTRY_GUIDE,
            "wiki/20-知识点/政府采购招标文件画像.md",
            "wiki/60-提示词/招标文件画像提示词.md",
        ],
    )
    source_context = f"""{shared_context}
待审文件，已加行号：
{numbered_text}
"""

    profile_prompt = f"""你是政府采购招标文件合规审查生产线的外部执行主体。

请先按 LLM Wiki 入口指引执行文件画像。本阶段只做画像，不识别风险，不生成报告。
不得使用外部标注、标准答案、历史审查记录或执行器内置审查知识。
报告和中间产物不得出现绝对路径。

本次文件位置：
- 原始文件：{target}
- 抽取文本：{extract_rel}
- 输出目录：{output_rel_dir}

画像阶段 LLM Wiki 知识：
{profile_knowledge}

待审文件，已加行号：
{numbered_text}

请执行入口指引中的环节一：文件画像。

只输出 `01-文件画像` Markdown 内容。不得输出风险清单，不得生成最终报告。
必须满足入口指引中 `01-文件画像` 的字段和通过条件；未见字段写 `未见`，不确定字段写 `待确认`。
"""
    profile, usage = chat_text(base_url, api_key, model, profile_prompt, max_tokens=1000)
    usages.append(("01-文件画像", usage))
    stage_file(profile_path, "01-文件画像", profile)

    route_prompt = f"""{shared_context}

已生成文件画像：
{profile}

请执行入口指引中的环节二：知识路由。

只输出 `02-知识路由表` Markdown 内容。不得输出风险清单，不得生成最终报告。
必须说明每个调用知识页的调用原因、适用层级、是否必读和执行状态。
"""
    route, usage = chat_text(base_url, api_key, model, route_prompt, max_tokens=6000)
    usages.append(("02-知识路由表", usage))
    stage_file(route_path, "02-知识路由表", route)

    actions_prompt = f"""{shared_context}

文件画像：
{profile}

知识路由表：
{route}

请执行入口指引中的环节三：动作清单。

只输出 `03-动作清单` Markdown 内容。不得输出风险详情，不得生成最终报告。
动作必须来自知识路由结果、逐章矩阵、通用审查协议和命中的品类动作协议。
"""
    actions, usage = chat_text(base_url, api_key, model, actions_prompt, max_tokens=8000)
    usages.append(("03-动作清单", usage))
    stage_file(actions_path, "03-动作清单", actions)

    action_exec_prompt = f"""你是政府采购招标文件合规审查生产线的外部执行主体。

请根据入口指引、文件画像、知识路由表和动作清单，执行环节四：逐动作执行。
风险判断只能来自已路由知识、动作清单和待审文件原文；不得使用外部标注、标准答案、历史审查记录或执行器内置审查知识。
报告和中间产物不得出现绝对路径。原文内容字段只能放待审文件原文。

文件画像：
{profile}

知识路由表：
{route}

动作清单：
{actions}

待审文件，已加行号：
{numbered_text}

请执行入口指引中的环节四：逐动作执行。

只输出 `04-动作执行记录` Markdown 内容。不要生成最终报告。
每个动作都必须有状态、读取范围、原文位置或未命中原因；命中和待确认动作必须形成候选风险或说明待确认原因。
"""
    action_exec, usage = chat_text(base_url, api_key, model, action_exec_prompt, max_tokens=14000)
    usages.append(("04-动作执行记录", usage))
    stage_file(action_exec_path, "04-动作执行记录", action_exec)

    atomized_prompt = f"""{shared_context}

文件画像：
{profile}

知识路由表：
{route}

动作清单：
{actions}

动作执行记录：
{action_exec}

请执行入口指引中的环节五：风险原子化。

只输出 `05-原子风险清单` Markdown 内容。不要生成最终报告。
必须按 LLM Wiki 风险原子化规则拆分候选风险；每个风险必须能反链来源动作和关联审查点。
"""
    atomized, usage = chat_text(base_url, api_key, model, atomized_prompt, max_tokens=12000)
    usages.append(("05-原子风险清单", usage))
    stage_file(atomized_path, "05-原子风险清单", atomized)

    quality_prompt = f"""{shared_context}

文件画像：
{profile}

知识路由表：
{route}

动作清单：
{actions}

动作执行记录：
{action_exec}

原子风险清单：
{atomized}

请执行入口指引中的环节六：质量门反查。

只输出 `06-质量门检查表` Markdown 内容。不要生成最终报告。
必须检查入口指引列出的最低质量门；如风险数量偏低，必须执行异常低风险数量反查并记录反查范围和结论。
"""
    quality, usage = chat_text(base_url, api_key, model, quality_prompt, max_tokens=8000)
    usages.append(("06-质量门检查表", usage))
    stage_file(quality_path, "06-质量门检查表", quality)

    report_prompt = f"""你是政府采购招标文件合规审查生产线的外部执行主体。

请基于已经生成并通过质量门检查的前六个中间产物，执行入口指引中的环节七：报告生成。
不得重新自由发挥，不得使用外部标注、标准答案、历史审查记录或执行器内置审查知识。
不得修改 LLM Wiki，不得对 LLM Wiki 运行维护命令。报告中不得出现绝对路径。

本次文件位置：
- 原始文件：{target}
- 抽取文本：{extract_rel}
- 输出目录：{output_rel_dir}

文件画像：
{profile}

知识路由表：
{route}

动作清单：
{actions}

动作执行记录：
{action_exec}

原子风险清单：
{atomized}

质量门检查表：
{quality}

请执行入口指引中的环节七：报告生成，输出 `07-AI审查记录` Markdown 内容。

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
- 文件画像：{profile_rel}
- 知识路由表：{route_rel}
- 动作清单：{actions_rel}
- 动作执行记录：{action_exec_rel}
- 原子风险清单：{atomized_rel}
- 质量门检查表：{quality_rel}
- 运行记录：{run_rel}

每条风险必须保留：结论类型、风险等级、原文位置、原文内容、问题说明、关联动作ID、关联审查点、审查依据、修改建议、是否需要人工复核。
风险标题必须统一使用三级标题格式：### 风险 1：风险标题、### 风险 2：风险标题。
风险必须根据 LLM Wiki 风险审查点和待审文件原文判断；不得把执行器当作审查知识来源。
原文内容只能是原文摘录。
不得出现绝对路径。
"""
    report, usage = chat_text(base_url, api_key, model, report_prompt, max_tokens=16000)
    usages.append(("07-AI审查记录", usage))
    if not report:
        print("pipeline returned empty report", file=sys.stderr)
        return 1

    report_path.write_text(report.rstrip() + "\n", encoding="utf-8")
    risk_count = count_risks(report)

    usage_rows = "\n".join(
        f"| {name} | {usage.get('prompt_tokens', '')} | {usage.get('completion_tokens', '')} | {usage.get('total_tokens', '')} |"
        for name, usage in usages
    )
    stage_rows = "\n".join(f"| {name} | {path} |" for name, path in stage_paths)
    knowledge_rows = "\n".join(f"- {page}" for page in knowledge_pages)

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
| 文件画像 | {profile_rel} |
| 知识路由表 | {route_rel} |
| 动作清单 | {actions_rel} |
| 动作执行记录 | {action_exec_rel} |
| 原子风险清单 | {atomized_rel} |
| 质量门检查表 | {quality_rel} |
| 审查报告 | {report_rel} |
| 运行记录 | {run_rel} |
| 本次输出目录 | {output_rel_dir} |
| 文件分类 | {category} |
| 运行模式 | hegui_cli.py 入口指引驱动流水线 |
| 运行时LLM | {model} |
| 风险点数量 | {risk_count} |

## 2. 执行边界

- 只读取本项目文件、本次指定招标文件，以及 config/hegui.yaml 中 wiki_home 指向的只读 LLM Wiki。
- 未读取外部标注、标准答案、人工批注或同一项目既有审查记录。
- 未修改 raw/ 下原始文件。
- 未修改 LLM Wiki。
- 未对 LLM Wiki 运行 ./lint、./ingest、./query 或其他维护命令。

## 3. 中间产物清单

| 环节 | 产物 |
| --- | --- |
{stage_rows}

## 4. 调用知识清单

{knowledge_rows}

## 5. 模型交互说明

本次按入口指引执行分阶段知识驱动流水线，返回用量：

| 阶段 | prompt_tokens | completion_tokens | total_tokens |
| --- | ---: | ---: | ---: |
{usage_rows}

## 6. 质量门结果

- 已按入口指引生成质量门检查表：{quality_rel}
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
    print(f"中间产物目录: {output_rel_dir}")
    print("质量门结果: 已生成质量门检查表，详见审查报告和运行记录")
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
