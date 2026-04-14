import time
import csv
from typing import List, Dict
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from scraper.config import TARGET_URL, BASE_URL
from scraper.utils import setup_logger

logger = setup_logger()

class RocheSpider:
    """罗氏媒体发布页面爬虫（使用 Playwright 处理动态内容）"""

    def __init__(self, headless: bool = True):
        self.playwright = sync_playwright().start()
        # 启动浏览器，添加反检测参数
        self.browser = self.playwright.chromium.launch(
            headless=headless,
            args=[
                '--disable-blink-features=AutomationControlled',
                '--no-sandbox',
                '--disable-dev-shm-usage',
            ]
        )
        self.page = self.browser.new_page()
        # 隐藏自动化特征
        self.page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
            Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
        """)
        self.page.set_viewport_size({"width": 1280, "height": 800})

    def parse_release_list(self) -> List[Dict]:
        """访问页面并解析新闻列表"""
        logger.info(f"正在访问: {TARGET_URL}")
        # 使用 'domcontentloaded' 代替 'networkidle' 以避免超时
        self.page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=30000)
        
                # 等待关键元素出现（新闻列表容器）
        try:
            # 等待包含新闻链接的元素出现
            self.page.wait_for_selector('a[href*="/media/releases/"]', timeout=10000)
            logger.info("检测到新闻链接，页面已加载")
        except:
            logger.warning("未检测到新闻链接，尝试继续...")
        
        # 滚动到页面顶部，确保新闻列表可见
        self.page.evaluate("window.scrollTo(0, 0)")
        self.page.wait_for_timeout(1000)
        
        # 截取完整页面（full_page=True）以便诊断
        self.page.screenshot(path="page_full_screenshot.png", full_page=True)
        logger.info("已保存完整页面截图: page_full_screenshot.png")

        # 获取渲染后的 HTML
        html = self.page.content()
        soup = BeautifulSoup(html, 'lxml')

        # ========== 自动诊断：输出可能的 class 名称 ==========
        all_elements = soup.find_all(True)
        class_set = set()
        for el in all_elements:
            if el.has_attr('class'):
                for cls in el['class']:
                    if any(k in cls.lower() for k in ['release', 'news', 'media', 'item', 'teaser', 'card']):
                        class_set.add(cls)

        if class_set:
            logger.info("页面中包含关键词的 class 名称（供参考）：")
            for cls in sorted(class_set):
                logger.info(f"  - {cls}")

        # ========== 直接通过新闻详情链接特征提取 ==========
        link_elements = soup.select('a[href*="/media/releases/"]')
        logger.info(f"找到 {len(link_elements)} 个包含 '/media/releases/' 的链接")

        releases = []
        seen_urls = set()  # 去重
        
        for link in link_elements:
            href = link.get('href')
            if not href or href in seen_urls:
                continue
            seen_urls.add(href)
            
            full_url = urljoin(BASE_URL, href)
            
            # 标题：优先取链接内标题标签文本，其次取链接自身文本
            title_elem = link.find('h2') or link.find('h3') or link.find('h4')
            title = title_elem.get_text(strip=True) if title_elem else link.get_text(strip=True)
            
            # 日期：向上查找父级中的 <time> 标签
            parent = link.find_parent('div') or link.find_parent('li') or link.parent
            date_elem = parent.find('time') if parent else None
            date = date_elem.get_text(strip=True) if date_elem else ""
            
            if title and full_url:
                releases.append({
                    'title': title,
                    'link': full_url,
                    'date': date
                })
        
        # 如果没找到，再用备用选择器尝试
        if not releases:
            logger.warning("未通过链接特征找到新闻，尝试备用选择器...")
            items = soup.select('article, div[class*="release"], li[class*="release"]')
            for item in items:
                title_elem = item.find('h2') or item.find('h3')
                link_elem = item.find('a', href=True)
                date_elem = item.find('time')
                if title_elem and link_elem:
                    releases.append({
                        'title': title_elem.get_text(strip=True),
                        'link': urljoin(BASE_URL, link_elem['href']),
                        'date': date_elem.get_text(strip=True) if date_elem else ""
                    })
        
        logger.info(f"共解析到 {len(releases)} 条新闻")
        return releases
    def save_to_csv(self, data: List[Dict], filename: str = 'roche_releases.csv'):
        """保存为 CSV 文件"""
        if not data:
            logger.warning("无数据可保存")
            return
        keys = data[0].keys()
        with open(filename, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.DictWriter(f, fieldnames=keys)
            writer.writeheader()
            writer.writerows(data)
        logger.info(f"数据已保存至 {filename}")

    def close(self):
        """关闭浏览器和 Playwright"""
        self.browser.close()
        self.playwright.stop()