"""
长文档分块策略

两种模式：
  1. 文章型：按 Markdown 标题语义边界分块，超限时滑动切割
  2. 表格型：按行分批，每批携带表头

带记忆的分块：每块携带前序块已抽取的实体摘要，让 LLM 能跨块建立关系。
"""
import re
from dataclasses import dataclass
from loguru import logger

CHUNK_MAX_CHARS = 6000   # 单块最大字符数
MEMORY_MAX_CHARS = 800   # 记忆摘要最大字符数
TABLE_BATCH_ROWS = 50    # 表格分块每批最大行数

_TABLE_SEPARATOR = re.compile(r'^\|[-| :]+\|$')  # Markdown 表格分隔符行


def _normalize_table_row(row: str) -> str:
    """去除 Markdown 表格行中每个单元格的填充空格，大幅减少宽表的字符浪费。"""
    cells = [c.strip() for c in row.split("|")]
    return "| " + " | ".join(cells[1:-1]) + " |"


@dataclass
class Chunk:
    text: str
    chunk_index: int
    total_chunks: int
    memory_context: str = ""
    is_table: bool = False
    table_header: str = ""


# ── 文档类型判断 ────────────────────────────────────────────────────────

def _is_table_heavy(text: str) -> bool:
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return False
    table_lines = sum(1 for l in lines if l.strip().startswith("|") or " | " in l)
    return table_lines / len(lines) > 0.4


# ── 文章型分块 ──────────────────────────────────────────────────────────

def _split_by_headings(text: str) -> list[str]:
    heading_pattern = re.compile(r'^#{1,3}\s+.+', re.MULTILINE)
    positions = [m.start() for m in heading_pattern.finditer(text)]

    if not positions:
        return [text]

    segments = []
    for i, pos in enumerate(positions):
        end = positions[i + 1] if i + 1 < len(positions) else len(text)
        segments.append(text[pos:end].strip())

    if positions[0] > 0:
        preamble = text[:positions[0]].strip()
        if preamble:
            segments.insert(0, preamble)

    return [s for s in segments if s]


def _slide_split(text: str, max_chars: int, overlap: int = 200) -> list[str]:
    if len(text) <= max_chars:
        return [text]

    chunks = []
    start = 0
    while start < len(text):
        end = start + max_chars
        if end >= len(text):
            chunks.append(text[start:])
            break

        boundary = -1
        for sep in ["。\n", "！\n", "？\n", "\n\n", "。", "！", "？", "\n"]:
            pos = text.rfind(sep, start + max_chars // 2, end)
            if pos != -1:
                boundary = pos + len(sep)
                break

        if boundary == -1:
            boundary = end

        chunks.append(text[start:boundary])
        start = max(start + 1, boundary - overlap)

    return chunks


def chunk_article(text: str) -> list[str]:
    segments = _split_by_headings(text)
    chunks = []
    for seg in segments:
        if len(seg) <= CHUNK_MAX_CHARS:
            chunks.append(seg)
        else:
            chunks.extend(_slide_split(seg, CHUNK_MAX_CHARS))
    return chunks


# ── 表格型分块 ──────────────────────────────────────────────────────────

def _extract_table_header(text: str) -> tuple[str, list[str]]:
    lines = text.splitlines()
    table_lines = [l for l in lines if l.strip().startswith("|") or " | " in l]

    if not table_lines:
        return "", lines

    header = _normalize_table_row(table_lines[0])
    # 跳过分隔符行（如 |---|---|）
    data_lines = [_normalize_table_row(l) for l in table_lines[1:] if not _TABLE_SEPARATOR.match(l.strip())]
    return header, data_lines


def chunk_table(text: str) -> list[tuple[str, str]]:
    header, data_lines = _extract_table_header(text)

    if not data_lines:
        return [("", text)]

    batches = []
    current_rows: list[str] = []
    current_chars = len(header) + 1  # 表头字符数作为基准

    for line in data_lines:
        line_chars = len(line) + 1  # +1 for newline
        # 超过行数限制 或 加上这行会超过字符限制（且当前批次非空）
        if (len(current_rows) >= TABLE_BATCH_ROWS or
                (current_rows and current_chars + line_chars > CHUNK_MAX_CHARS)):
            batches.append((header, "\n".join(current_rows)))
            current_rows = []
            current_chars = len(header) + 1
        current_rows.append(line)
        current_chars += line_chars

    if current_rows:
        batches.append((header, "\n".join(current_rows)))

    avg_rows = len(data_lines) / len(batches) if batches else 0
    logger.info(
        f"[Chunking] 表格分块: {len(data_lines)}行 → {len(batches)}块 "
        f"({avg_rows:.0f}行/块), 表头{len(header)}字符, "
        f"数据行均长{sum(len(l) for l in data_lines[:100]) / min(100, len(data_lines)):.0f}字符"
    )
    return batches


# ── 记忆上下文 ──────────────────────────────────────────────────────────

def build_memory_context(extracted_entities: list[dict]) -> str:
    if not extracted_entities:
        return ""
    lines = [
        f"- {e['entity_type']}: {e['entity_name']}"
        for e in extracted_entities
        if e.get("entity_name") and e.get("entity_type")
    ]
    summary = "\n".join(lines)
    if len(summary) > MEMORY_MAX_CHARS:
        summary = summary[:MEMORY_MAX_CHARS] + "\n...（更多实体已省略）"
    return summary


# ── 统一分块入口 ────────────────────────────────────────────────────────

def split_document(text: str) -> list[Chunk]:
    """
    对文档做智能分块，返回 Chunk 列表。
    调用方在迭代时逐步填充 memory_context。
    """
    # 去掉 YAML frontmatter
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4:].lstrip("\n")

    if not text.strip():
        return []

    if _is_table_heavy(text):
        batches = chunk_table(text)
        chunks = [
            Chunk(
                text=rows,
                chunk_index=i,
                total_chunks=len(batches),
                is_table=True,
                table_header=header,
            )
            for i, (header, rows) in enumerate(batches)
        ]
        logger.info(f"[Chunking] 表格型分块: {len(chunks)} 块, 首块表头 {len(batches[0][0]) if batches else 0} 字符")
        return chunks
    else:
        raw_chunks = chunk_article(text)
        logger.info(f"[Chunking] 文章型分块: {len(raw_chunks)} 块, 总字符 {sum(len(c) for c in raw_chunks)}")
        if len(raw_chunks) == 1:
            return [Chunk(text=raw_chunks[0], chunk_index=0, total_chunks=1)]
        return [
            Chunk(text=c, chunk_index=i, total_chunks=len(raw_chunks))
            for i, c in enumerate(raw_chunks)
        ]
