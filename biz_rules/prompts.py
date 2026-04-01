"""
==========================================================
  大模型 Prompt 模板
  ★ 药学研究生专属文件 ★ —— 修改提示词就改这里
==========================================================

【使用说明】
- 每个函数返回一段"喂给 Qwen 的提示词"字符串。
- 想调整 AI 提取效果？直接改函数里的中文提示词即可。
- 不需要懂任何框架，纯文本操作。
"""


def get_structured_extraction_prompt(markdown_text: str) -> str:
    """
    结构化信息提取 Prompt。
    把 Markdown 全文喂给 Qwen，让它提取出关键字段。

    参数:
        markdown_text: 已解析好的 Markdown 全文

    返回:
        拼好的 prompt 字符串
    """
    prompt = f"""你是一名专业的医药信息提取助手。请从以下文档中提取结构化信息。

## 要求
1. 仔细阅读文档内容，提取以下字段。
2. 如果某个字段在文档中找不到，返回空字符串 ""。
3. 必须严格按照 JSON 格式返回，不要添加任何额外文字。

## 需要提取的字段
- title: 文档标题
- publish_date: 发布日期（格式尽量统一为 YYYY-MM-DD）
- category: 文档分类（如：通告、公告、指南、文献综述等）
- summary: 100字以内的摘要
- drug_name: 涉及的药品名称
- approval_number: 批准文号（如有）
- manufacturer: 生产企业（如有）
- keywords: 3-5个关键词，以列表形式返回

## 文档内容
{markdown_text}

## 返回格式（严格 JSON）
{{
    "title": "",
    "publish_date": "",
    "category": "",
    "summary": "",
    "drug_name": "",
    "approval_number": "",
    "manufacturer": "",
    "keywords": []
}}"""
    return prompt


def get_summary_prompt(markdown_text: str) -> str:
    """
    纯摘要生成 Prompt（备用，当只需要摘要时使用）。

    参数:
        markdown_text: 已解析好的 Markdown 全文

    返回:
        拼好的 prompt 字符串
    """
    prompt = f"""请为以下医药文档生成一段100字以内的中文摘要，抓住核心要点：

{markdown_text}

请直接返回摘要文本，不要加任何前缀。"""
    return prompt
