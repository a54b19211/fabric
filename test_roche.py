from scraper.roche_spider import RocheSpider

def main():
    spider = RocheSpider(headless=True)  # 若想看到浏览器窗口，改为 headless=False
    try:
        releases = spider.parse_release_list()
        if releases:
            print(f"\n✅ 成功获取 {len(releases)} 条新闻，预览前三条：")
            for r in releases[:3]:
                print(f"   - {r['date']} : {r['title']}")
            spider.save_to_csv(releases, 'roche_media_releases.csv')
        else:
            print("\n❌ 未获取到新闻数据。")
            print("请检查项目文件夹中的 page_screenshot.png 截图，")
            print("确认页面是否正常加载。若页面显示异常，可能是反爬机制，请尝试将 headless=False 后重试。")
    finally:
        spider.close()

if __name__ == "__main__":
    main()