"""flows.chunking（文档分块）的单元测试。"""
from flows.chunking import (
    _extract_table_header,
    _is_table_heavy,
    _normalize_table_row,
    _split_by_headings,
    build_memory_context,
    chunk_article,
    chunk_table,
    split_document,
)


class TestNormalizeTableRow:
    def test_strips_cell_padding(self):
        assert _normalize_table_row("|  a  |  b  |") == "| a | b |"

    def test_already_clean(self):
        assert _normalize_table_row("| a | b |") == "| a | b |"


class TestIsTableHeavy:
    def test_table_detected(self):
        text = "| a | b |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |"
        assert _is_table_heavy(text) is True

    def test_article_not_table(self):
        assert _is_table_heavy("这是普通文本。\n没有表格。") is False


class TestSplitByHeadings:
    def test_no_headings(self):
        assert _split_by_headings("普通文本") == ["普通文本"]

    def test_with_headings(self):
        text = "# H1\naaa\n## H2\nbbb"
        assert _split_by_headings(text) == ["# H1\naaa", "## H2\nbbb"]

    def test_preamble(self):
        text = "intro\n# H1\nbody"
        assert _split_by_headings(text) == ["intro", "# H1\nbody"]


class TestChunkArticle:
    def test_short_unchanged(self):
        assert chunk_article("短文本") == ["短文本"]

    def test_long_splits(self):
        chunks = chunk_article("a" * 13000)
        assert len(chunks) > 1


class TestExtractTableHeader:
    def test_basic(self):
        text = "| a | b |\n|---|---|\n| 1 | 2 |"
        header, data_lines = _extract_table_header(text)
        assert header == "| a | b |"
        assert data_lines == ["| 1 | 2 |"]


class TestChunkTable:
    def test_small_single_batch(self):
        text = "| a | b |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |"
        batches = chunk_table(text)
        assert len(batches) == 1
        header, rows = batches[0]
        assert header == "| a | b |"
        assert "| 1 | 2 |" in rows
        assert "| 3 | 4 |" in rows

    def test_row_limit_splits(self):
        header = "| a |\n|---|\n"
        rows = "".join(f"| {i} |\n" for i in range(55))
        batches = chunk_table(header + rows)
        assert len(batches) == 2  # 50 + 5


class TestBuildMemoryContext:
    def test_empty(self):
        assert build_memory_context([]) == ""

    def test_basic(self):
        entities = [{"entity_type": "Drug", "entity_name": "阿司匹林"}]
        assert build_memory_context(entities) == "- Drug: 阿司匹林"

    def test_skips_invalid(self):
        entities = [
            {"entity_type": "", "entity_name": "X"},
            {"entity_type": "Drug", "entity_name": ""},
            {"entity_type": "Drug", "entity_name": "阿司匹林"},
        ]
        assert build_memory_context(entities) == "- Drug: 阿司匹林"

    def test_truncate_long(self):
        entities = [{"entity_type": "Drug", "entity_name": f"药{i:03d}"} for i in range(100)]
        result = build_memory_context(entities)
        assert "更多实体已省略" in result


class TestSplitDocument:
    def test_frontmatter_stripped(self):
        text = "---\nfoo: bar\n---\n正文"
        chunks = split_document(text)
        assert len(chunks) == 1
        assert chunks[0].text == "正文"

    def test_empty_returns_empty(self):
        assert split_document("   ") == []

    def test_article_type(self):
        chunks = split_document("普通文本内容")
        assert len(chunks) == 1
        assert chunks[0].is_table is False

    def test_table_type(self):
        text = "| a | b |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |"
        chunks = split_document(text)
        assert chunks[0].is_table is True
        assert chunks[0].table_header == "| a | b |"
