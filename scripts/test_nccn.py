"""
NCCN 爬虫独立测试脚本。

运行方式：
    .venv/Scripts/python.exe scripts/test_nccn.py
"""
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from biz_rules.extractors.nccn.nccn_fetcher import iter_nccn, _parse_rendered_list, CATEGORIES, _NAME_TO_NUM


def test_list_page():
    """测试列表页：验证 4 个分类各自的条目数（去重后）。"""
    print("=" * 60)
    print("Step 1: 列表页解析（渲染模式，验证各分类条目数）")
    print("=" * 60)

    from scrapling.fetchers import StealthyFetcher
    fetcher = StealthyFetcher(auto_match=False)

    total = 0
    for cat_name, (_, cat_url) in [(v[0], v) for v in CATEGORIES.values()]:
        cat_num = _NAME_TO_NUM[cat_name]
        page = fetcher.fetch(cat_url)
        items = _parse_rendered_list(page, cat_num, cat_name)
        print(f"  {cat_name}: {len(items)} 条")
        total += len(items)

    print(f"\n  合计: {total} 条（各分类独立去重）")
    print("  ✓ 列表页解析完成\n")


def test_detail_page():
    """测试详情页：抓 2 条，验证 section 结构和 PDF 链接。"""
    print("=" * 60)
    print("Step 2: 详情页抓取（2 条，验证 section + PDF）")
    print("=" * 60)

    api_cfg = {
        "categories": ["Treatment by Cancer Type"],
        "max_per_category": 2,
        "fetch_detail": True,
        "request_delay": 1.5,
    }

    for i, record in enumerate(iter_nccn(api_cfg), 1):
        print(f"\n[{i}] {record['title']}")
        print(f"     版本:    {record['version']}")
        print(f"     主 PDF:  {record['pdf_url']}")
        print(f"     UUID:    {record['uuid']}")
        print(f"     Sections ({len(record['sections'])}):")
        for sec in record["sections"]:
            print(f"       [{sec['section_name']}] {len(sec['pdfs'])} 个 PDF")
            for pdf in sec["pdfs"]:
                ver = f" v{pdf['version']}" if pdf["version"] else ""
                print(f"         - {pdf['label']}{ver}")
                print(f"           {pdf['url']}")
        print(f"\n     Markdown 预览（前 300 字符）：")
        print("     " + record["markdown"][:300].replace("\n", "\n     "))

    print("\n  ✓ 详情页抓取完成")


if __name__ == "__main__":
    test_list_page()
    test_detail_page()
