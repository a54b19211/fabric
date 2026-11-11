from datetime import date
from typing import Dict, Iterator
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from loguru import logger

from config.sites.hkex_pharma_list_auto import PHARMA_COMPANIES  # 改用带 stock_id 的版本


def _parse_row(row, company: Dict, skip_headlines) -> Dict | None:
    """结果表的一行 → 一条公告。不是公告的返回 None：列数不对，或文件类型栏在 skip_headlines 里（如“展示文件”，只是提示语）。"""
    cols = row.find_all('td')
    if len(cols) < 4:
        return None

    release_time = cols[0].get_text(strip=True).replace("發放時間: ", "")
    extracted_code = cols[1].get_text(strip=True).replace("股份代號: ", "")  # 从页面提取的股票代码（备用）
    extracted_name = cols[2].get_text(strip=True).replace("股份簡稱: ", "")

    # 文件信息
    doc_cell = cols[3]
    headline = doc_cell.find('div', class_='headline')
    headline_text = headline.get_text(strip=True) if headline else ""
    if headline_text in skip_headlines:
        return None

    doc_link = doc_cell.find('div', class_='doc-link')
    pdf_url = ""
    if doc_link:
        pdf_link = doc_link.find('a')
        if pdf_link and pdf_link.get('href'):
            pdf_url = urljoin("https://www1.hkexnews.hk", pdf_link['href'])

    return {
        "release_time": release_time,
        "stock_code": company.get("code"),  # 从配置获取的股票代码
        "stock_code_extracted": extracted_code,
        "stock_name": extracted_name,
        "stock_id": company.get("stock_id"),  # 从配置获取
        "headline": headline_text,
        "pdf_url": pdf_url,
        "attachment_links": [{"url": pdf_url}] if pdf_url else [],
    }


def fetch_hkex_announcements(api_config: Dict) -> Iterator[Dict]:
    """从港交所披露易拉取上市公司公告"""

    base_url = api_config.get("base_url", "https://www1.hkexnews.hk/search/titlesearch.xhtml")
    companies = api_config.get("companies", PHARMA_COMPANIES)
    skip_headlines = api_config.get("skip_headlines", ())

    logger.info(f"[HKEX] 公司数量: {len(companies)}")

    for idx, company in enumerate(companies):
        stock_id = company.get("stock_id")
        stock_code = company.get("code")  # 获取股票代码
        stock_name = company.get("name")  # 获取股票名称
        logger.info(f"[DEBUG] 第{idx + 1}家: {stock_name} - stock_code={stock_code} - stock_id={stock_id}")

        if not stock_id:
            logger.warning(f"跳过 {stock_name}：缺少 stock_id")
            continue

        # 构建 POST 参数
        params = {
            "lang": api_config.get("lang", "ZH"),
            "market": api_config.get("market", "SEHK"),
            "searchType": api_config.get("searchType", 0),
            "documentType": "",
            "t1code": "",
            "t2Gcode": "",
            "t2code": "",
            "stockId": stock_id,
            "from": api_config.get("from_date", "20240101"),
            "to": api_config.get("to_date") or date.today().strftime("%Y%m%d"),  # 不填 = 今天；接口不接受空的 to
            "category": 0,
            "title": ""
        }

        logger.info(f"[HKEX] 开始拉取 {stock_name}({stock_id}) 的公告")

        try:
            response = requests.post(
                base_url,
                data=params,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                },
                timeout=30
            )
            response.raise_for_status()

            soup = BeautifulSoup(response.text, 'html.parser')
            table = soup.find('table', class_='table')

            if not table:
                logger.warning(f"[HKEX] {stock_name} 未找到公告表格")
                continue

            rows = table.find('tbody').find_all('tr')
            logger.info(f"[HKEX] {stock_name} 找到 {len(rows)} 条公告")

            for row in rows:
                announcement = _parse_row(row, company, skip_headlines)
                if announcement:
                    yield announcement

        except Exception as e:
            logger.error(f"[HKEX] {stock_name} 采集失败: {e}")
