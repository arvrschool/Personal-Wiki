from __future__ import annotations

import re


DEFAULT_FALLBACK_CATEGORY = "其他"


CATEGORY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("多模态", ("multimodal", "vision-language", "vision language", "vlm", "cross-modal", "text-image")),
    ("语言模型", ("large language model", "llm", "transformer", "instruction tuning", "prompt", "in-context learning", "rag")),
    ("视觉", ("vision", "image", "visual", "video", "segmentation", "detection", "recognition", "diffusion", "self-supervised")),
    ("机器人/控制", ("robot", "robotic", "control", "policy", "planning", "navigation", "trajectory", "manipulation")),
    ("语音/音频", ("speech", "audio", "voice", "music", "acoustic")),
    ("3D/图形", ("3d", "gaussian splatting", "nerf", "rendering", "point cloud", "mesh", "scene reconstruction")),
    ("医学/生物", ("medical", "clinical", "biomedical", "protein", "genomic", "eeg", "mri", "healthcare")),
    ("理论/基础", ("theoretical", "theory", "analysis", "generalization", "convergence", "proof", "kernel", "optimization")),
    ("系统/效率", ("system", "serving", "inference", "efficient", "compression", "quantization", "distillation", "latency", "throughput")),
)


COMMON_CONCEPT_HINTS: dict[str, str] = {
    "Large Language Model": "以大规模语料预训练为基础的通用语言模型范式。",
    "Transformer": "基于自注意力机制的序列建模架构。",
    "Vision-Language Model": "联合处理视觉与语言信号的多模态模型。",
    "Multimodal Learning": "联合建模多个模态并学习共享表征的方法。",
    "Self-Supervised Learning": "利用数据自身结构构造监督信号的学习范式。",
    "Contrastive Learning": "通过正负样本对比学习判别性表征的方法。",
    "Masked Autoencoder": "通过掩码重建任务学习高质量表征的方法。",
    "World Model": "用于建模环境动态、预测未来状态的内部模型。",
    "Diffusion Model": "通过逐步去噪过程生成样本的概率生成模型。",
    "Retrieval-Augmented Generation": "将外部检索结果并入生成流程的知识增强方法。",
    "Instruction Tuning": "通过指令数据微调使模型更好遵循任务描述的训练方式。",
    "In-Context Learning": "在上下文示例条件下完成新任务的推理能力。",
    "Fine-tuning": "在预训练模型基础上针对特定任务继续训练。",
    "Linear Probing": "冻结主模型后只训练线性头以评估表征质量。",
    "Reinforcement Learning": "基于交互奖励信号优化策略的学习范式。",
    "Robot Learning": "面向机器人感知、控制与策略学习的方法集合。",
    "3D Gaussian Splatting": "用可微高斯表示进行高效三维场景重建与渲染的方法。",
    "NeRF": "用神经辐射场表示场景并进行新视角合成的方法。",
    "Point Cloud": "以离散三维点集合表示几何结构的数据形式。",
}


CONCEPT_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Large Language Model", ("large language model", "large language models", "llm", "llms")),
    ("Transformer", ("transformer", "transformers")),
    ("Vision-Language Model", ("vision-language model", "vision language model", "vlm", "vlms")),
    ("Multimodal Learning", ("multimodal learning", "multimodal", "cross-modal")),
    ("Self-Supervised Learning", ("self-supervised learning", "self supervised learning")),
    ("Contrastive Learning", ("contrastive learning",)),
    ("Masked Autoencoder", ("masked autoencoder", "masked autoencoders", "mae", "maes")),
    ("World Model", ("world model", "world models")),
    ("Diffusion Model", ("diffusion model", "diffusion models")),
    ("Retrieval-Augmented Generation", ("retrieval-augmented generation", "retrieval augmented generation", "rag")),
    ("Instruction Tuning", ("instruction tuning", "instruction-tuning")),
    ("In-Context Learning", ("in-context learning", "in context learning")),
    ("Fine-tuning", ("fine-tuning", "finetuning", "fine tuning")),
    ("Linear Probing", ("linear probing", "linear probe")),
    ("Reinforcement Learning", ("reinforcement learning", "rl")),
    ("Robot Learning", ("robot learning", "robotic learning")),
    ("3D Gaussian Splatting", ("3d gaussian splatting",)),
    ("NeRF", ("nerf",)),
    ("Point Cloud", ("point cloud", "point clouds")),
)


_GENERIC_PREFIX_WORDS = {
    "a",
    "an",
    "the",
    "towards",
    "rethinking",
    "revisiting",
    "understanding",
    "improving",
    "scaling",
    "survey",
}


def _clean_list(items: list[str], max_items: int | None = None) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in items:
        value = re.sub(r"\s+", " ", str(item or "").strip(" -:;,"))
        if not value:
            continue
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(value)
        if max_items is not None and len(cleaned) >= max_items:
            break
    return cleaned


def looks_research_entry(item: dict | None) -> bool:
    item = item or {}
    template_id = (item.get("template_id") or "").lower()
    entry_type = (item.get("entry_type") or "").lower()
    source_kind = (item.get("source_kind") or "").lower()
    source = (item.get("source") or "").lower()
    url = (item.get("url") or item.get("source_url") or "").lower()

    if template_id == "research_paper" or entry_type == "paper":
        return True
    if item.get("arxiv_id") or item.get("pdf_url") or item.get("local_pdf"):
        return True
    if source_kind in {"arxiv", "pdf", "paper"}:
        return True
    if source in {"arxiv", "arxiv_api", "openreview", "semantic_scholar", "semantic scholar", "crossref", "doi", "local_pdf"}:
        return True
    if any(domain in url for domain in ("arxiv.org", "openreview.net", "doi.org", "acm.org", "ieee.org", "springer.com", "sciencedirect.com")):
        return True
    return False


def infer_category(item: dict, fallback: str = DEFAULT_FALLBACK_CATEGORY) -> str:
    category = (item.get("category") or "").strip()
    if category:
        return category
    if not looks_research_entry(item):
        return fallback

    text = " ".join(
        [
            str(item.get("title") or ""),
            str(item.get("abstract") or ""),
            " ".join(item.get("authors") or []),
        ]
    ).lower()
    for label, keywords in CATEGORY_RULES:
        if any(keyword in text for keyword in keywords):
            return label
    return fallback


def _extract_title_prefix(title: str) -> str:
    title = re.sub(r"\s+", " ", (title or "").strip())
    if not title:
        return ""
    for separator in (":", " - ", " | "):
        if separator in title:
            prefix = title.split(separator, 1)[0].strip()
            if prefix:
                return prefix
    return title


def _looks_named_prefix(prefix: str) -> bool:
    words = prefix.split()
    if not 1 <= len(words) <= 6:
        return False
    if prefix in COMMON_CONCEPT_HINTS:
        return True
    
    # Exclude version numbers or arXiv IDs (digits + dots)
    if re.match(r"^\d+[\d\.]*$", prefix):
        return False

    if any(char.isdigit() for char in prefix) or "-" in prefix:
        return True
    if re.search(r"\b[A-Z]{2,}(?:-[A-Z0-9]{1,})*\b", prefix):
        return True
    if words[0].lower() in _GENERIC_PREFIX_WORDS:
        return False
    return False


_GENERIC_TOKENS = {
    "Real-World",
    "Large-Scale",
    "High-Fidelity",
    "Multi-View",
    "End-to-End",
    "State-of-the-Art",
}


def _extract_named_tokens(title: str) -> list[str]:
    patterns = (
        r"\b[A-Z]{2,}(?:-[A-Z0-9]{1,})*\b",
        r"\b[A-Z][a-z0-9]+(?:-[A-Z][a-z0-9]+)+\b",
        r"\b\dD\s+[A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){0,2}\b",
    )
    tokens: list[str] = []
    for pattern in patterns:
        matches = re.findall(pattern, title or "")
        tokens.extend([m for m in matches if m not in _GENERIC_TOKENS])
    return _clean_list(tokens)


def extract_concepts(item: dict, max_items: int = 6) -> list[str]:
    stored = item.get("concepts")
    if isinstance(stored, list):
        return _clean_list([str(v) for v in stored], max_items=max_items)
    if isinstance(stored, str):
        return _clean_list(re.split(r"[,\n;]+", stored), max_items=max_items)
    if not looks_research_entry(item):
        return []

    title = re.sub(r"\s+", " ", str(item.get("title") or "").strip())
    abstract = re.sub(r"\s+", " ", str(item.get("abstract") or "").strip())
    text = f"{title} {abstract}".lower()
    arxiv_id = str(item.get("arxiv_id") or "")
    found: list[str] = []

    prefix = _extract_title_prefix(title)
    if prefix and _looks_named_prefix(prefix):
        if prefix != arxiv_id:
            found.append(prefix)

    found.extend(_extract_named_tokens(title))

    # Filter out exact arxiv_id from tokens too
    found = [f for f in found if f != arxiv_id]

    for display, patterns in CONCEPT_PATTERNS:
        if any(pattern in text for pattern in patterns):
            found.append(display)

    return _clean_list(found, max_items=max_items)


def concept_metadata(concept: str) -> tuple[str, str]:
    hint = COMMON_CONCEPT_HINTS.get(concept, "")
    if re.fullmatch(r"[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2}", concept):
        return "person", hint
    if any(token in concept for token in ("Lab", "Research", "University", "Institute", "AI")):
        return "organization", hint
    if any(char.isdigit() for char in concept) or "-" in concept or re.fullmatch(r"[A-Z0-9-]{2,}", concept):
        return "method", hint
    return "concept", hint
