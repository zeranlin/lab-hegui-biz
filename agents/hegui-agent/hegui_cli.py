#!/usr/bin/env python3
"""Government procurement compliance review production-line executor."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree
from zoneinfo import ZoneInfo
from zoneinfo import ZoneInfoNotFoundError


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


def read_auth_key(config_dir: Path) -> str:
    auth_file = config_dir / "auth.json"
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


def local_now() -> datetime:
    try:
        return datetime.now(ZoneInfo("Asia/Shanghai"))
    except ZoneInfoNotFoundError:
        return datetime.now().astimezone()


def extract_docx_text(target_path: Path) -> str:
    namespaces = {
        "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    }
    paragraphs: list[str] = []
    with zipfile.ZipFile(target_path) as docx:
        document_xml = docx.read("word/document.xml")
    root = ElementTree.fromstring(document_xml)
    for paragraph in root.findall(".//w:p", namespaces):
        parts: list[str] = []
        for node in paragraph.iter():
            tag = node.tag.rsplit("}", 1)[-1]
            if tag == "t" and node.text:
                parts.append(node.text)
            elif tag == "tab":
                parts.append("\t")
            elif tag == "br":
                parts.append("\n")
        text = html.unescape("".join(parts)).strip()
        if text:
            paragraphs.append(text)
    return "\n".join(paragraphs)


def extract_file_text(target_path: Path) -> str:
    suffix = target_path.suffix.lower()
    if suffix == ".pdf":
        try:
            result = subprocess.run(
                ["pdftotext", str(target_path), "-"],
                check=True,
                text=True,
                encoding="utf-8",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("PDF text extraction requires `pdftotext` in PATH.") from exc
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"PDF text extraction failed: {exc.stderr.strip()}") from exc
        return result.stdout
    if suffix == ".docx":
        return extract_docx_text(target_path)
    if suffix == ".doc":
        raise RuntimeError("Legacy .doc is not supported cross-platform. Please convert it to .docx first.")
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


def estimate_tokens(text: str) -> int:
    # Chinese-heavy prompt rough estimate. Used for budgeting and run records only.
    return round(len(text) / 1.5)


def budget_wiki_pages(wiki_home: Path, pages: list[str], char_budget: int) -> tuple[str, list[str]]:
    chunks: list[str] = []
    loaded: list[str] = []
    seen: set[str] = set()
    used = 0
    for rel in pages:
        rel = normalize_wiki_ref(rel) or rel
        if rel in seen or not is_allowed_knowledge_page(rel):
            continue
        seen.add(rel)
        text = read_wiki_page(wiki_home, rel)
        if not text:
            continue
        chunk = f"\n\n# {rel}\n\n{text}"
        if chunks and used + len(chunk) > char_budget:
            continue
        chunks.append(chunk)
        loaded.append(rel)
        used += len(chunk)
    return "".join(chunks), loaded


CORE_EXECUTION_PAGES = [
    ENTRY_GUIDE,
    "wiki/70-审查协议/知识驱动审查执行规范.md",
    "wiki/70-审查协议/政府采购招标文件业务审查流水线.md",
    "wiki/70-审查协议/政府采购招标文件审查协议.md",
    "wiki/20-知识点/知识分层与路由规则.md",
    "wiki/20-知识点/政府采购逐章审查矩阵.md",
    "wiki/70-审查协议/风险原子化规则.md",
    "wiki/70-审查协议/质量门规则.md",
    "wiki/25-风险审查点/风险审查点总览.md",
    "wiki/90-模板/审查记录模板.md",
    "wiki/90-模板/AI调度运行记录模板.md",
]


PROFILE_PAGES = [
    ENTRY_GUIDE,
    "wiki/20-知识点/政府采购招标文件画像.md",
    "wiki/15-行业基础/政府采购专项场景画像.md",
    "wiki/60-提示词/招标文件画像提示词.md",
]


def domain_pages_from_profile(profile: str, target: str) -> list[str]:
    text = f"{target}\n{profile}"
    pages: list[str] = []
    if "物业" in text:
        pages.extend(
            [
                "wiki/15-行业基础/物业管理服务采购背景.md",
                "wiki/20-知识点/物业管理动作化审查包.md",
                "wiki/70-审查协议/物业管理审查动作协议.md",
            ]
        )
    return pages


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
    except urllib.error.URLError as exc:
        raise RuntimeError(f"chat completion connection failed: {exc.reason}") from exc
    except TimeoutError as exc:
        raise RuntimeError("chat completion timed out") from exc


def direct_chat_review(
    target: str,
    target_path: Path,
    biz_home: Path,
    wiki_home: Path,
    config_dir: Path,
) -> int:
    config_toml = config_dir / "config.toml"
    base_url = read_toml_string(config_toml, "base_url")
    model = read_toml_string(config_toml, "model")
    api_key = read_auth_key(config_dir)
    if not base_url or not model or not api_key:
        print("direct chat config incomplete", file=sys.stderr)
        return 1

    now = local_now()
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

    usages: list[tuple[str, dict]] = []
    prompt_stats: list[tuple[str, int, int]] = []
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

    core_knowledge, core_pages = budget_wiki_pages(wiki_home, CORE_EXECUTION_PAGES, char_budget=52000)

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

本阶段核心 LLM Wiki 知识：
{core_knowledge}
"""

    profile_knowledge, profile_pages = budget_wiki_pages(wiki_home, PROFILE_PAGES, char_budget=26000)

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
    prompt_stats.append(("01-文件画像", len(profile_prompt), estimate_tokens(profile_prompt)))
    profile, usage = chat_text(base_url, api_key, model, profile_prompt, max_tokens=1000)
    usages.append(("01-文件画像", usage))
    stage_file(profile_path, "01-文件画像", profile)

    domain_pages = domain_pages_from_profile(profile, target)
    route_knowledge, route_pages = budget_wiki_pages(
        wiki_home,
        [
            *CORE_EXECUTION_PAGES,
            "wiki/20-知识点/政府采购招标文件画像.md",
            "wiki/15-行业基础/政府采购专项场景画像.md",
            *domain_pages,
        ],
        char_budget=62000,
    )
    route_catalog = f"""## 风险审查点目录

{risk_review_point_catalog(wiki_home)}

## 法规依据目录

{law_catalog(wiki_home)}
"""

    route_prompt = f"""你是政府采购招标文件合规审查生产线的外部执行主体。

请执行入口指引中的环节二：知识路由。本阶段不读取同一项目历史记录，不输出风险清单，不生成最终报告。
报告和中间产物不得出现绝对路径。

本次文件位置：
- 原始文件：{target}
- 抽取文本：{extract_rel}
- 输出目录：{output_rel_dir}

路由阶段 LLM Wiki 知识：
{route_knowledge}

可选知识目录：
{route_catalog}

已生成文件画像：
{profile}

只输出 `02-知识路由表` Markdown 内容。不得输出风险清单，不得生成最终报告。
必须说明每个调用知识页的调用原因、适用层级、是否必读和执行状态。
每个知识页请尽量使用相对于 LLM Wiki 的稳定路径。
"""
    prompt_stats.append(("02-知识路由表", len(route_prompt), estimate_tokens(route_prompt)))
    route, usage = chat_text(base_url, api_key, model, route_prompt, max_tokens=6000)
    usages.append(("02-知识路由表", usage))
    stage_file(route_path, "02-知识路由表", route)

    routed_refs = extract_wiki_refs(route)
    action_knowledge, action_pages = budget_wiki_pages(
        wiki_home,
        [*CORE_EXECUTION_PAGES, *domain_pages, *routed_refs],
        char_budget=62000,
    )

    actions_prompt = f"""你是政府采购招标文件合规审查生产线的外部执行主体。

请执行入口指引中的环节三：动作清单。本阶段只生成动作，不输出风险详情，不生成最终报告。
报告和中间产物不得出现绝对路径。

本次文件位置：
- 原始文件：{target}
- 抽取文本：{extract_rel}
- 输出目录：{output_rel_dir}

动作清单阶段 LLM Wiki 知识：
{action_knowledge}

文件画像：
{profile}

知识路由表：
{route}

请执行入口指引中的环节三：动作清单。

只输出 `03-动作清单` Markdown 内容。不得输出风险详情，不得生成最终报告。
动作必须来自知识路由结果、逐章矩阵、通用审查协议和命中的品类动作协议。
"""
    prompt_stats.append(("03-动作清单", len(actions_prompt), estimate_tokens(actions_prompt)))
    actions, usage = chat_text(base_url, api_key, model, actions_prompt, max_tokens=8000)
    usages.append(("03-动作清单", usage))
    stage_file(actions_path, "03-动作清单", actions)

    action_refs = extract_wiki_refs(actions)
    review_knowledge, review_pages = budget_wiki_pages(
        wiki_home,
        [*CORE_EXECUTION_PAGES, *domain_pages, *routed_refs, *action_refs],
        char_budget=62000,
    )

    action_exec_prompt = f"""你是政府采购招标文件合规审查生产线的外部执行主体。

请根据入口指引、文件画像、知识路由表和动作清单，执行环节四：逐动作执行。
风险判断只能来自已路由知识、动作清单和待审文件原文；不得使用外部标注、标准答案、历史审查记录或执行器内置审查知识。
报告和中间产物不得出现绝对路径。原文内容字段只能放待审文件原文。

逐动作执行阶段 LLM Wiki 知识：
{review_knowledge}

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
    prompt_stats.append(("04-动作执行记录", len(action_exec_prompt), estimate_tokens(action_exec_prompt)))
    action_exec, usage = chat_text(base_url, api_key, model, action_exec_prompt, max_tokens=14000)
    usages.append(("04-动作执行记录", usage))
    stage_file(action_exec_path, "04-动作执行记录", action_exec)

    atomized_prompt = f"""你是政府采购招标文件合规审查生产线的外部执行主体。

请执行入口指引中的环节五：风险原子化。本阶段只处理动作执行记录中的候选风险。
不得使用外部标注、标准答案、历史审查记录或执行器内置审查知识。不得出现绝对路径。

风险原子化阶段 LLM Wiki 知识：
{review_knowledge}

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
    prompt_stats.append(("05-原子风险清单", len(atomized_prompt), estimate_tokens(atomized_prompt)))
    atomized, usage = chat_text(base_url, api_key, model, atomized_prompt, max_tokens=12000)
    usages.append(("05-原子风险清单", usage))
    stage_file(atomized_path, "05-原子风险清单", atomized)

    quality_prompt = f"""你是政府采购招标文件合规审查生产线的外部执行主体。

请执行入口指引中的环节六：质量门反查。
不得使用外部标注、标准答案、历史审查记录或执行器内置审查知识。不得出现绝对路径。

质量门阶段 LLM Wiki 知识：
{review_knowledge}

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
    prompt_stats.append(("06-质量门检查表", len(quality_prompt), estimate_tokens(quality_prompt)))
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
    prompt_stats.append(("07-AI审查记录", len(report_prompt), estimate_tokens(report_prompt)))
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
    prompt_size_rows = "\n".join(f"| {name} | {chars} | {tokens} |" for name, chars, tokens in prompt_stats)
    stage_rows = "\n".join(f"| {name} | {path} |" for name, path in stage_paths)
    knowledge_pages = [*profile_pages, *route_pages, *action_pages, *review_pages]
    knowledge_rows = "\n".join(f"- {page}" for page in dict.fromkeys(knowledge_pages))

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

本次按入口指引执行分阶段知识驱动流水线。执行器按阶段装载知识包，避免每轮重复注入全量 LLM Wiki。

Prompt 大小估算：

| 阶段 | 字符数 | 粗略token估算 |
| --- | ---: | ---: |
{prompt_size_rows}

模型返回用量：

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

    biz_home = Path(__file__).resolve().parent.parent.parent
    config_file = biz_home / "config/hegui.yaml"
    configured_wiki = read_simple_yaml_value(config_file, "wiki_home")
    wiki_setting = os.environ.get("HEGUI_WIKI_HOME") or configured_wiki
    wiki_home = Path(wiki_setting) if wiki_setting else biz_home.parent / "lab-hegui-llm"
    if not wiki_home.is_absolute():
        wiki_home = (biz_home / wiki_home).resolve()
    output_root = biz_home / "outputs"
    config_dir = biz_home / "config"

    if not wiki_home.is_dir():
        print("wiki home not found", file=sys.stderr)
        return 1
    wiki_home = wiki_home.resolve()

    output_root.mkdir(parents=True, exist_ok=True)
    if not (config_dir / "config.toml").is_file() or not (config_dir / "auth.json").is_file():
        print("llm config not found: config/config.toml and config/auth.json are required", file=sys.stderr)
        return 1

    resolved_target = resolve_target(target_input, biz_home, wiki_home)
    if isinstance(resolved_target, str):
        print(resolved_target, file=sys.stderr)
        return 1
    target, target_path = resolved_target

    return direct_chat_review(target, target_path, biz_home, wiki_home, config_dir)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
