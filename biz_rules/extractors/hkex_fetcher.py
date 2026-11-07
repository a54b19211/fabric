from typing import Dict, Iterator
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from loguru import logger

from config.sites.hkex_pharma_list_auto import PHARMA_COMPANIES  # 改用带 stock_id 的版本


def fetch_hkex_announcements(api_config: Dict) -> Iterator[Dict]:
    """从港交所披露易拉取上市公司公告"""

    base_url = api_config.get("base_url", "https://www1.hkexnews.hk/search/titlesearch.xhtml")
    companies = api_config.get("companies", PHARMA_COMPANIES)

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
            "to": api_config.get("to_date", "20260410"),
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
                cols = row.find_all('td')
                if len(cols) < 4:
                    continue

                # 提取时间
                time_cell = cols[0]
                release_time = time_cell.get_text(strip=True).replace("發放時間: ", "")

                # 提取股票代码（从页面提取，备用）
                stock_code_cell = cols[1]
                extracted_code = stock_code_cell.get_text(strip=True).replace("股份代號: ", "")

                # 提取股票名称
                stock_name_cell = cols[2]
                extracted_name = stock_name_cell.get_text(strip=True).replace("股份簡稱: ", "")

                # 提取文件信息
                doc_cell = cols[3]
                headline = doc_cell.find('div', class_='headline')
                headline_text = headline.get_text(strip=True) if headline else ""

                doc_link = doc_cell.find('div', class_='doc-link')
                pdf_url = ""
                if doc_link:
                    pdf_link = doc_link.find('a')
                    if pdf_link and pdf_link.get('href'):
                        pdf_url = urljoin("https://www1.hkexnews.hk", pdf_link['href'])

                # 构造公告 - 添加 stock_code 字段
                announcement = {
                    "release_time": release_time,
                    "stock_code": stock_code,  # 从配置获取的股票代码
                    "stock_code_extracted": extracted_code,  # 从页面提取的股票代码（备用）
                    "stock_name": extracted_name,
                    "stock_id": stock_id,  # 添加这一行（从配置获取）
                    "headline": headline_text,
                    "pdf_url": pdf_url,
                    "attachment_links": [{"url": pdf_url}] if pdf_url else [],
                }

                yield announcement

        except Exception as e:
            logger.error(f"[HKEX] {stock_name} 采集失败: {e}")
