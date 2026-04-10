# Scrapling 概览

## 目录
1. [选择你的学习路径](#选择你的学习路径)
2. [查找元素](#查找元素)
3. [访问元素数据](#访问元素数据)
4. [导航操作](#导航操作)
5. [获取网站内容](#获取网站内容)
   - [HTTP 请求](#http-请求)
   - [动态加载](#动态加载)
   - [动态反保护加载](#动态反保护加载)

---

## 选择你的学习路径

不确定从哪里开始？根据你的需求选择对应的路径：

| 我想做什么 | 从这里开始 |
|---|---|
| **解析已有的 HTML** | [查询元素](https://scrapling.readthedocs.io/en/latest/parsing/selection.html) — CSS、XPath 和基于文本的选择 |
| **快速抓取页面**并制作原型 | 选择一个 [获取器](https://scrapling.readthedocs.io/en/latest/fetching/choosing.html) 立即测试，或启动 [交互式 Shell](https://scrapling.readthedocs.io/en/latest/cli/interactive-shell.html) |
| **构建可扩展的爬虫** | [Spiders](https://scrapling.readthedocs.io/en/latest/spiders/getting-started.html) — 支持并发、多会话、可暂停/恢复的爬取 |
| **无需编写代码即可抓取** | [CLI 提取命令](https://scrapling.readthedocs.io/en/latest/cli/extract-commands.html) 或将 [MCP 服务器](https://scrapling.readthedocs.io/en/latest/ai/mcp-server.html) 连接到你喜欢的 AI 工具 |
| **从其他库迁移** | [从 BeautifulSoup 迁移](https://scrapling.readthedocs.io/en/latest/tutorials/migrating_from_beautifulsoup.html) 或 [Scrapy 对比](https://scrapling.readthedocs.io/en/latest/spiders/architecture.html#comparison-with-scrapy) |

---

## 示例 HTML 文档

以下是由 ChatGPT 生成的 HTML 文档，将作为本页面的示例：

```html
<html>
  <head>
    <title>Complex Web Page</title>
    <style>
      .hidden { display: none; }
    </style>
  </head>
  <body>
    <header>
      <nav>
        <ul>
          <li> <a href="#home">Home</a> </li>
          <li> <a href="#about">About</a> </li>
          <li> <a href="#contact">Contact</a> </li>
        </ul>
      </nav>
    </header>
    <main>
      <section id="products" schema='{"jsonable": "data"}'>
        <h2>Products</h2>
        <div class="product-list">
          <article class="product" data-id="1">
            <h3>Product 1</h3>
            <p class="description">This is product 1</p>
            <span class="price">$10.99</span>
            <div class="hidden stock">In stock: 5</div>
          </article>

          <article class="product" data-id="2">
            <h3>Product 2</h3>
            <p class="description">This is product 2</p>
            <span class="price">$20.99</span>
            <div class="hidden stock">In stock: 3</div>
          </article>

          <article class="product" data-id="3">
            <h3>Product 3</h3>
            <p class="description">This is product 3</p>
            <span class="price">$15.99</span>
            <div class="hidden stock">Out of stock</div>
          </article>
        </div>
      </section>

      <section id="reviews">
        <h2>Customer Reviews</h2>
        <div class="review-list">
          <div class="review" data-rating="5">
            <p class="review-text">Great product!</p>
            <span class="reviewer">John Doe</span>
          </div>
          <div class="review" data-rating="4">
            <p class="review-text">Good value for money.</p>
            <span class="reviewer">Jane Smith</span>
          </div>
        </div>
      </section>
    </main>
    <script id="page-data" type="application/json">
      {
        "lastUpdated": "2024-09-22T10:30:00Z",
        "totalProducts": 3
      }
    </script>
  </body>
</html>
```

### 加载 HTML

```python
from scrapling.parser import Selector
page = Selector(html_doc)
# <data='<html><head><title>Complex Web Page</tit...'>
```

### 获取所有文本内容

```python
page.get_all_text(ignore_tags=('script', 'style'))
# 'Complex Web Page\nHome\nAbout\nContact\nProducts\nProduct 1\nThis is product 1\n$10.99\nIn stock: 5\nProduct 2\nThis is product 2\n$20.99\nIn stock: 3\nProduct 3\nThis is product 3\n$15.99\nOut of stock\nCustomer Reviews\nGreat product!\nJohn Doe\nGood value for money.\nJane Smith'
```

---

## 查找元素

如果你想找到页面上的某个元素，你一定能找到！唯一的限制是你的创造力！

### 查找第一个 HTML section 元素

```python
section_element = page.find('section')
# <data='<section id="products" schema='{"jsonabl...' parent='<main><section id="products" schema='{"j...'>
```

### 查找所有 section 元素

```python
section_elements = page.find_all('section')
# [<data='<section id="products" schema='{"jsonabl...' parent='<main><section id="products" schema='{"j...'>, 
#  <data='<section id="reviews"><h2>Customer Revie...' parent='<main><section id="products" schema='{"j...'>]
```

### 查找 id 属性值为 products 的所有 section 元素

```python
section_elements = page.find_all('section', {'id':"products"})
# 等同于
section_elements = page.find_all('section', id="products")
# [<data='<section id="products" schema='{"jsonabl...' parent='<main><section id="products" schema='{"j...'>]
```

### 查找 id 属性值包含 product 的所有 section 元素

```python
section_elements = page.find_all('section', {'id*':"product"})
```

### 查找文本内容匹配正则表达式 Product \d 的所有 h3 元素

```python
page.find_all('h3', re.compile(r'Product \d'))
# [<data='<h3>Product 1</h3>' parent='<article class="product" data-id="1"><h3...'>, 
#  <data='<h3>Product 2</h3>' parent='<article class="product" data-id="2"><h3...'>, 
#  <data='<h3>Product 3</h3>' parent='<article class="product" data-id="3"><h3...'>]
```

### 查找文本内容仅匹配正则表达式 Product 的所有 h3 和 h2 元素

```python
page.find_all(['h3', 'h2'], re.compile(r'Product'))
# [<data='<h3>Product 1</h3>' parent='<article class="product" data-id="1"><h3...'>, 
#  <data='<h3>Product 2</h3>' parent='<article class="product" data-id="2"><h3...'>, 
#  <data='<h3>Product 3</h3>' parent='<article class="product" data-id="3"><h3...'>, 
#  <data='<h2>Products</h2>' parent='<section id="products" schema='{"jsonabl...'>]
```

### 查找文本内容精确匹配 Products 的所有元素（不考虑空白字符）

```python
page.find_by_text('Products', first_match=False)
# [<data='<h2>Products</h2>' parent='<section id="products" schema='{"jsonabl...'>]
```

### 查找文本内容匹配正则表达式 Product \d 的所有元素

```python
page.find_by_regex(r'Product \d', first_match=False)
# [<data='<h3>Product 1</h3>' parent='<article class="product" data-id="1"><h3...'>, 
#  <data='<h3>Product 2</h3>' parent='<article class="product" data-id="2"><h3...'>, 
#  <data='<h3>Product 3</h3>' parent='<article class="product" data-id="3"><h3...'>]
```

### 查找与目标元素相似的所有元素

```python
target_element = page.find_by_regex(r'Product \d', first_match=True)
# <data='<h3>Product 1</h3>' parent='<article class="product" data-id="1"><h3...'>
target_element.find_similar()
# [<data='<h3>Product 2</h3>' parent='<article class="product" data-id="2"><h3...'>, 
#  <data='<h3>Product 3</h3>' parent='<article class="product" data-id="3"><h3...'>]
```

### 查找匹配 CSS 选择器的第一个元素

```python
page.css('.product-list [data-id="1"]')[0]
# <data='<article class="product" data-id="1"><h3...' parent='<div class="product-list"> <article clas...'>
```

### 查找匹配 CSS 选择器的所有元素

```python
page.css('.product-list article')
# [<data='<article class="product" data-id="1"><h3...' parent='<div class="product-list"> <article clas...'>, 
#  <data='<article class="product" data-id="2"><h3...' parent='<div class="product-list"> <article clas...'>, 
#  <data='<article class="product" data-id="3"><h3...' parent='<div class="product-list"> <article clas...'>]
```

### 查找匹配 XPath 选择器的第一个元素

```python
page.xpath("//*[@id='products']/div/article")[0]
# <data='<article class="product" data-id="1"><h3...' parent='<div class="product-list"> <article clas...'>
```

### 查找匹配 XPath 选择器的所有元素

```python
page.xpath("//*[@id='products']/div/article")
# [<data='<article class="product" data-id="1"><h3...' parent='<div class="product-list"> <article clas...'>, 
#  <data='<article class="product" data-id="2"><h3...' parent='<div class="product-list"> <article clas...'>, 
#  <data='<article class="product" data-id="3"><h3...' parent='<div class="product-list"> <article clas...'>]
```

---

## 访问元素数据

访问元素数据非常简单：

```python
>>> section_element.tag
'section'

>>> print(section_element.attrib)
{'id': 'products', 'schema': '{"jsonable": "data"}'}

>>> section_element.attrib['schema'].json()  # 如果属性值可以转换为 json，则使用 .json() 转换
{'jsonable': 'data'}

>>> section_element.text  # 直接的文本内容
''

>>> section_element.get_all_text()  # 递归获取所有文本内容
'Products\nProduct 1\nThis is product 1\n$10.99\nIn stock: 5\nProduct 2\nThis is product 2\n$20.99\nIn stock: 3\nProduct 3\nThis is product 3\n$15.99\nOut of stock'

>>> section_element.html_content  # 元素的 HTML 内容
'<section id="products" schema=\'{"jsonable": "data"}\'><h2>Products</h2>\n <div class="product-list">\n <article class="product" data-id="1"><h3>Product 1</h3>\n <p class="description">This is product 1</p>\n <span class="price">$10.99</span>\n <div class="hidden stock">In stock: 5</div>\n </article><article class="product" data-id="2"><h3>Product 2</h3>\n <p class="description">This is product 2</p>\n <span class="price">$20.99</span>\n <div class="hidden stock">In stock: 3</div>\n </article><article class="product" data-id="3"><h3>Product 3</h3>\n <p class="description">This is product 3</p>\n <span class="price">$15.99</span>\n <div class="hidden stock">Out of stock</div>\n </article></div>\n </section>'

>>> print(section_element.prettify())  # 格式化版本
'''
<section id="products" schema='{"jsonable": "data"}'><h2>Products</h2>
 <div class="product-list">
 <article class="product" data-id="1"><h3>Product 1</h3>
 <p class="description">This is product 1</p>
 <span class="price">$10.99</span>
 <div class="hidden stock">In stock: 5</div>
 </article><article class="product" data-id="2"><h3>Product 2</h3>
 <p class="description">This is product 2</p>
 <span class="price">$20.99</span>
 <div class="hidden stock">In stock: 3</div>
 </article><article class="product" data-id="3"><h3>Product 3</h3>
 <p class="description">This is product 3</p>
 <span class="price">$15.99</span>
 <div class="hidden stock">Out of stock</div>
 </article>
 </div>
</section>
'''

>>> section_element.path  # 元素在 DOM 树中的所有祖先
[<data='<main><section id="products" schema='{"j...' parent='<body> <header><nav><ul><li> <a href="#h...'>,
 <data='<body> <header><nav><ul><li> <a href="#h...' parent='<html><head><title>Complex Web Page</tit...'>,
 <data='<html><head><title>Complex Web Page</tit...'>]

>>> section_element.generate_css_selector
'#products'

>>> section_element.generate_full_css_selector
'body > main > #products > #products'

>>> section_element.generate_xpath_selector
"//*[@id='products']"

>>> section_element.generate_full_xpath_selector
"//body/main/*[@id='products']"
```

---

## 导航操作

使用上面找到的元素进行导航操作：

```python
>>> section_element.parent
<data='<main><section id="products" schema='{"j...' parent='<body> <header><nav><ul><li> <a href="#h...'>
>>> section_element.parent.tag
'main'
>>> section_element.parent.parent.tag
'body'

>>> section_element.children
[<data='<h2>Products</h2>' parent='<section id="products" schema='{"jsonabl...'>,
 <data='<div class="product-list"> <article clas...' parent='<section id="products" schema='{"jsonabl...'>]

>>> section_element.siblings
[<data='<section id="reviews"><h2>Customer Revie...' parent='<main><section id="products" schema='{"j...'>]

>>> section_element.next  # 获取下一个元素，同样逻辑适用于 `section_element.previous`
<data='<section id="reviews"><h2>Customer Revie...' parent='<main><section id="products" schema='{"j...'>]

>>> section_element.children.css('h2::text').getall()
['Products']

>>> page.css('[data-id="1"]')[0].has_class('product')
True
```

### 遍历祖先元素

如果需要访问元素的所有祖先，可以像下面这样遍历：

```python
for ancestor in section_element.iterancestors():
    # 对祖先元素执行操作...
```

### 查找满足条件的祖先元素

可以搜索元素的某个特定祖先，只需传递一个接受 `Selector` 对象作为参数的函数，如果满足条件返回 `True`，否则返回 `False`：

```python
>>> section_element.find_ancestor(lambda ancestor: ancestor.css('nav'))
<data='<body> <header><nav><ul><li> <a href="#h...' parent='<html><head><title>Complex Web Page</tit...'>
```

---

## 获取网站内容

Scrapling 不只支持传入原始 HTML，还可以通过 HTTP 请求或浏览器抓取直接获取网站响应。

每个用例都有对应的获取器。

### HTTP 请求

对于简单的 HTTP 请求，有一个 `Fetcher` 类可以导入并使用：

```python
from scrapling.fetchers import Fetcher
page = Fetcher.get('https://scrapling.requestcatcher.com/get', impersonate="chrome")
```

#### 所有 HTTP 方法的使用示例

```python
>>> from scrapling.fetchers import Fetcher
>>> page = Fetcher.get('https://scrapling.requestcatcher.com/get', stealthy_headers=True, follow_redirects=True)
>>> page = Fetcher.post('https://scrapling.requestcatcher.com/post', data={'key': 'value'}, proxy='http://username:password@localhost:8030')
>>> page = Fetcher.put('https://scrapling.requestcatcher.com/put', data={'key': 'value'})
>>> page = Fetcher.delete('https://scrapling.requestcatcher.com/delete')
```

#### 异步请求

对于异步请求，需要修改导入：

```python
>>> from scrapling.fetchers import AsyncFetcher
>>> page = await AsyncFetcher.get('https://scrapling.requestcatcher.com/get', stealthy_headers=True, follow_redirects=True)
>>> page = await AsyncFetcher.post('https://scrapling.requestcatcher.com/post', data={'key': 'value'}, proxy='http://username:password@localhost:8030')
>>> page = await AsyncFetcher.put('https://scrapling.requestcatcher.com/put', data={'key': 'value'})
>>> page = await AsyncFetcher.delete('https://scrapling.requestcatcher.com/delete')
```

#### 重要说明

1. **stealthy_headers 参数**：启用后，会生成真实浏览器头部并使用它们，包括 Google referer 头部。默认启用。

2. **impersonate 参数**：允许伪造特定浏览器版本的 TLS 指纹。

3. **http3 参数**：启用后，获取器将使用 HTTP/3 进行请求，使请求更真实。

这只是该获取器的冰山一角，更多详情请查看[这里](https://scrapling.readthedocs.io/en/latest/fetching/static.html)。

---

### 动态加载

如果你像大多数人一样需要处理动态网站，我们为你准备好了！

`DynamicFetcher` 类（原 `PlayWrightFetcher`）提供了使用基于 Chromium 的浏览器获取和加载网页的多种选项。

```python
>>> from scrapling.fetchers import DynamicFetcher
>>> page = DynamicFetcher.fetch('https://www.google.com/search?q=%22Scrapling%22', disable_resources=True)  # 原生 Playwright 选项
>>> page.css("#search a::attr(href)").get()
'https://github.com/D4Vinci/Scrapling'
>>> # fetch 的异步版本
>>> page = await DynamicFetcher.async_fetch('https://www.google.com/search?q=%22Scrapling%22', disable_resources=True)
>>> page.css("#search a::attr(href)").get()
'https://github.com/D4Vinci/Scrapling'
```

它构建在 [Playwright](https://playwright.dev/python/) 之上，目前提供两种主要的运行选项，可以按需混合使用：

- **原生 Playwright**：除了你选择的修改外，不做任何其他修改。使用 Chromium 浏览器。

- **真实浏览器**：通过传递 `real_chrome` 参数或你的浏览器的 CDP URL 来控制，大多数选项都可以在其上启用。

这只是该获取器的冰山一角。更多详情和完整参数列表请查看[这里](https://scrapling.readthedocs.io/en/latest/fetching/dynamic.html)。

---

### 动态反保护加载

如果你需要处理带有烦人反保护措施的动态网站，我们也为你准备好了！

`StealthyFetcher` 类使用上面解释的 `DynamicFetcher` 的隐身版本。

它的一些功能包括：

1. **轻松自动绕过**所有类型的 Cloudflare Turnstile/Interstitial。

2. **绕过 CDP 运行时泄漏和 WebRTC 泄漏**。

3. **隔离 JS 执行**，移除许多 Playwright 指纹，并停止通过已知的机器人行为进行检测。

4. **生成 Canvas 噪声**以防止通过 Canvas 进行指纹识别。

5. **自动修补已知方法**以检测无头模式运行，并提供击败时区不匹配攻击的选项。

6. **以及其他反保护选项...**

```python
>>> from scrapling.fetchers import StealthyFetcher
>>> page = StealthyFetcher.fetch('https://www.browserscan.net/bot-detection')  # 默认以无头模式运行
>>> page.status == 200
True
>>> page = StealthyFetcher.fetch('https://nopecha.com/demo/cloudflare', solve_cloudflare=True)  # 如果出现 Cloudflare 验证码，自动解决
>>> page.status == 200
True
>>> page = StealthyFetcher.fetch('https://www.browserscan.net/bot-detection', humanize=True, os_randomize=True) # 以及其他参数...
>>> # fetch 的异步版本
>>> page = await StealthyFetcher.async_fetch('https://www.browserscan.net/bot-detection')
>>> page.status == 200
True
```

这只是该获取器的冰山一角。更多详情和完整参数列表请查看[这里](https://scrapling.readthedocs.io/en/latest/fetching/stealthy.html)。

---

## 总结

这就是 Scrapling 的概览。如果你想了解更多，请继续阅读下一章节。

### 核心功能总结

1. **灵活的元素查找**：支持 CSS 选择器、XPath 选择器、文本匹配、正则表达式等多种查找方式
2. **丰富的数据访问**：可以轻松访问元素的标签、属性、文本、HTML 内容等
3. **强大的导航功能**：支持父子、兄弟元素导航，以及祖先元素遍历
4. **多样的网站获取方式**：
   - `Fetcher`：简单 HTTP 请求
   - `DynamicFetcher`：动态网页抓取
   - `StealthyFetcher`：反保护动态网页抓取
5. **同步与异步支持**：所有获取器都支持同步和异步操作
6. **反检测能力**：StealthyFetcher 提供强大的反检测和反爬虫绕过功能

无论是静态网页还是动态网页，无论是否有反爬虫保护，Scrapling 都能提供合适的解决方案。

---

# 性能基准测试 (Performance Benchmarks)

## 概述

Scrapling 不仅功能强大，而且速度极快。以下基准测试将 Scrapling 的解析器与其他流行库的最新版本进行了对比。

### 文本提取速度测试（5000 个嵌套元素）

| # | 库 | 时间 (毫秒) | 相比 Scrapling |
|---|---|---|---|
| 1 | Scrapling | 2.02 | 1.0x |
| 2 | Parsel/Scrapy | 2.04 | 1.01x |
| 3 | Raw Lxml | 2.54 | 1.257x |
| 4 | PyQuery | 24.17 | ~12x |
| 5 | Selectolax | 82.63 | ~41x |
| 6 | MechanicalSoup | 1549.71 | ~767.1x |
| 7 | BS4 with Lxml | 1584.31 | ~784.3x |
| 8 | BS4 with html5lib | 3391.91 | ~1679.1x |

### 元素相似性和文本搜索性能

Scrapling 的自适应元素查找功能显著优于其他库：

| 库 | 时间 (毫秒) | 相比 Scrapling |
|---|---|---|
| Scrapling | 2.39 | 1.0x |
| AutoScraper | 12.45 | 5.209x |

**说明**：所有基准测试结果均基于 100+ 次运行的平均值。如需了解测试方法，请查看 [benchmarks.py](https://github.com/D4Vinci/Scrapling/blob/main/benchmarks.py)

## 性能分析

### 文本提取速度

从测试结果可以看出：

1. **Scrapling 与 Parsel/Scrapy 表现相近**：两者在 5000 个嵌套元素的文本提取中几乎相同（2.02ms vs 2.04ms），这表明 Scrapling 在基础解析性能上已达到顶级水平。

2. **Raw Lxml 稍慢**：作为底层的 lxml 库，速度约为 Scrapling 的 1.26 倍。

3. **PyQuery 明显落后**：PyQuery 的速度是 Scrapling 的约 12 倍（24.17ms vs 2.02ms）。

4. **Selectolax 更慢**：速度约为 Scrapling 的 41 倍。

5. **基于 BeautifulSoup 的库最慢**：MechanicalSoup、BS4 with Lxml 和 BS4 with html5lib 的速度分别是 Scrapling 的约 767 倍、784 倍和 1679 倍。

### 元素相似性和文本搜索

在元素相似性和文本搜索方面：

- **Scrapling 明显快于 AutoScraper**：Scrapling 耗时 2.39ms，而 AutoScraper 需要 12.45ms，约为 Scrapling 的 5.2 倍。

这说明 Scrapling 的自适应元素查找功能在保持高性能的同时，提供了强大的智能匹配能力。

## 结论

Scrapling 在性能方面表现出色：

1. **解析速度快**：与 Scrapy/Parsel 持平，远超大多数竞争对手。

2. **自适应查找高效**：在提供智能元素定位的同时，保持了极高的执行速度。

3. **适合大规模爬取**：在处理大量嵌套元素和复杂页面结构时仍能保持快速响应。

对于需要高性能 Web 爬取的项目，Scrapling 是一个优秀的选择。

---

# 查询元素 (Querying elements)

## 概述

Scrapling 目前仅支持解析 HTML 页面，不支持 XML feeds。做出这个决定是因为自适应功能无法与 XML 一起工作，但这可能会很快改变，所以请保持关注 :)

在 Scrapling 中，有五种主要方法来查找元素：

1. **CSS3 选择器**

2. **XPath 选择器**

3. **基于过滤器/条件的查找元素**

4. **查找内容包含特定文本的元素**

5. **查找内容匹配特定正则表达式的元素**

当然，还有其他间接的方法可以查找元素，但在这里我们将详细讨论主要方法。我们还将介绍 Scrapling 最显著的功能之一：能够查找与你的元素相似的元素；你可以直接从[这里](#finding-similar-elements)跳转到该部分。

如果你是 Web 爬取的新手，几乎没有编写选择器的经验，并且想要快速开始，我建议你直接从[这里](#filters-based-searching)学习 `find`/`find_all` 方法。

## CSS/XPath 选择器

### 什么是 CSS 选择器？

[CSS](https://en.wikipedia.org/wiki/CSS)是一种用于为 HTML 文档应用样式的语言。它定义了选择器，以将这些样式与特定的 HTML 元素关联。

Scrapling 实现了如 [W3C 规范](http://www.w3.org/TR/2011/REC-css3-selectors-20110929/)中所述的 CSS3 选择器。CSS 选择器支持来自 `cssselect`，所以最好阅读[cssselect 支持的选择器](https://cssselect.readthedocs.io/en/latest/#supported-selectors)和伪函数/元素。

此外，Scrapling 还实现了一些非标准的伪元素：

- 要选择文本节点，使用 `::text`。

- 要选择属性值，使用 `::attr(name)`，其中 name 是你想要获取其值的属性名称

简而言之，如果你来自 Scrapy/Parsel，你会发现这里的选择器逻辑相同，以便于使用。无需实现大多数人习惯的不同逻辑 :)

要使用 CSS 选择器选择元素，使用 `css` 方法，它返回 `Selectors`。使用 `[0]` 获取第一个元素，或使用 `.get()` / `.getall()` 从文本/属性伪选择器中提取文本值。

### 什么是 XPath 选择器？

[XPath](https://en.wikipedia.org/wiki/XPath)是一种用于选择 XML 文档中节点的语言，也可以用于 HTML。这个[速查表](https://devhints.io/xpath)是学习关于[XPath](https://en.wikipedia.org/wiki/XPath)的好资源。Scrapling 通过 [lxml](https://lxml.de/)直接添加 XPath 选择器。

简而言之，这与 CSS 选择器的情况相同；如果你来自 Scrapy/Parsel，你会发现这里的选择器逻辑相同。然而，Scrapling 没有像 Scrapy/Parsel 那样实现 XPath 扩展函数 `has-class`。相反，它提供了 `has_class` 方法，可以在返回的元素上用于相同的目的。

要使用 XPath 选择器选择元素，你有 `xpath` 方法。同样，该方法遵循上述 CSS 选择器方法的相同逻辑。

注意：`css` 和 `xpath` 的每个方法都有额外的参数，但我们在这里没有解释，因为它们都关于自适应功能。该功能将在后面的单独页面中详细说明。

### 选择器示例

让我们看看使用 CSS 和 XPath 选择器的一些共享示例。

选择所有类为 `product` 的元素。

```
products = page.css('.product')
products = page.xpath('//*[@class="product"]')
```

**注意**：

如果还有另一个类，XPath 选择器将不准确；**始终最好依赖 CSS 按类选择**

选择类为 `product` 的第一个元素。

```
product = page.css('.product')[0]
product = page.xpath('//*[@class="product"]')[0]
```

获取第一个类为 `h1` 的元素的文本

```
title = page.css('h1::text').get()
title = page.xpath('//h1//text()').get()
```

这与执行以下操作相同

```
title = page.css('h1')[0].text
title = page.xpath('//h1')[0].text
```

获取第一个类为 `a` 的元素的 `href` 属性

```
link = page.css('a::attr(href)').get()
link = page.xpath('//a/@href').get()
```

选择类为 `h1` 且类为 `product` 的元素下的第一个元素的文本，其中包含 `Phone`。

```
title = page.css('.product h1:contains("Phone")::text').get()
title = page.xpath('//*[@class="product"]//h1[contains(text(),"Phone")]/text()').get()
```

如果它们返回结果，你可以按需要嵌套和链接选择器

```
page.css('.product')[0].css('h1:contains("Phone")::text').get()
page.xpath('//*[@class="product"]')[0].xpath('//h1[contains(text(),"Phone")]/text()').get()
page.xpath('//*[@class="product"]')[0].css('h1:contains("Phone")::text').get()
```

另一个示例

所有在其 `href` 属性中有 'image' 的链接

```
links = page.css('a[href*="image"]')
links = page.xpath('//a[contains(@href, "image")]')
for index, link in enumerate(links):
    link_value = link.attrib['href']  # 比 link.css('::attr(href)').get() 更简洁
    link_text = link.text
    print(f'Link number {index} points to this url {link_value} with text content as "{link_text}"')
```

## 基于文本内容的选择

Scrapling 提供了基于元素的直接文本内容选择元素的能力，你有两种方法来做到这一点：

1. 通过 `find_by_text` 方法查找其直接文本内容包含给定文本的元素，并提供许多选项。

2. 通过 `find_by_regex` 方法查找其直接文本内容匹配给定正则表达式模式的元素，并提供许多选项。

如果你对正则表达式（regex）足够熟悉，你可以用 `find_by_regex` 做到 `find_by_text` 能做的任何事情，但我们提供了更多选项以使其更容易被所有用户访问。

使用 `find_by_text`，你传递文本作为第一个参数；使用 `find_by_regex`，正则表达式模式是第一个参数。这两种方法共享以下参数：

- **first_match**：如果为 `True`（默认），该方法将返回它找到的第一个结果。

- **case_sensitive**：如果为 `True`，将考虑字母的大小写。

- **clean_match**：如果为 `True`，所有空白和连续空格将在匹配前被替换为单个空格。

默认情况下，Scrapling 搜索你传递给 `find_by_text` 的文本/模式的精确匹配，因此所需元素的文本内容必须仅是你输入的文本，但这就是为什么它还有一个额外的参数，即：

- **partial**：如果启用，`find_by_text` 将返回包含输入文本的元素。因此它不再是精确匹配

**注意**：

方法 `find_by_regex` 可以接受正则字符串和编译的正则表达式模式作为其第一个参数，正如你在接下来的示例中看到的那样。

### 查找相似元素

Scrapling 提出的最显著的新功能之一是能够告诉 Scrapling 查找与你手头元素相似的元素。此功能的灵感来自 AutoScraper 库，但在 Scrapling 中，它可以在通过任何方法找到的元素上使用。它的大多数用可能会在通过文本内容查找元素之后发生，类似于 AutoScraper 的工作方式，因此在这里解释很方便。

那么，它是如何工作的呢？

想象一个场景，你找到了一个产品，例如通过其标题，并且你想从同一个表格/容器中提取其他列出的产品。有了你所拥有的元素，你可以在其上调用方法 `.find_similar()`，Scrapling 将会：

1. 查找与该元素具有相同 DOM 树深度的所有页面元素。

2. 检查所有找到的元素，并丢弃那些不具有相同标签名、父标签名和祖父标签名的元素。

3. 现在我们确定（就像 99% 确定）这些元素是我们想要的元素，但作为最后的检查，Scrapling 将使用模糊匹配来丢弃其属性看起来不像我们元素属性的元素。有一个百分比来控制这一步，我建议你除非默认设置没有获得你想要的元素，否则不要修改它。

这说很多，我知道，但我必须深入探讨。我将在下一节给出使用此方法的示例，但首先，这些是可以传递给此方法的参数：

- **similarity_threshold**：这是我们在第 3 步中讨论的用于比较元素属性的百分比。默认值为 0.2。简单来说，两个元素的标签属性应该至少有 20% 的相似性。如果你想关闭此检查（基本上是第 3 步），你可以将此属性设置为 0，但我建议你先阅读其他参数的作用。

- **ignore_attributes**：在最后一步匹配属性时将被忽略的属性名称。默认值为 `('href', 'src',)`，因为 URL 可能在元素之间变化很大，使它们不可靠。

- **match_text**：如果为 `True`，元素的文本内容将在匹配时被考虑（第 3 步）。在典型情况下使用此参数不建议，但这取决于你。

现在，让我们查看下面的示例。

### 示例

让我们看看一些使用原始文本和正则表达式查找元素的共享示例。

我将使用 `Fetcher` 类来进行这些示例，但它将在后面详细说明。

```
from scrapling.fetchers import Fetcher
page = Fetcher.get('https://books.toscrape.com/index.html')
```

查找文本完全匹配此文本的第一个元素

```
>>> page.find_by_text('Tipping the Velvet')
<data='<a href="catalogue/tipping-the-velvet_99...' parent='<h3><a href="catalogue/tipping-the-velve...'>
```

将其与 `page.urljoin` 结合使用，以从相对 `href` 返回完整的 URL。

```
>>> page.find_by_text('Tipping the Velvet').attrib['href']
'catalogue/tipping-the-velvet_999/index.html'
>>> page.urljoin(page.find_by_text('Tipping the Velvet').attrib['href'])
'https://books.toscrape.com/catalogue/tipping-the-velvet_999/index.html'
```

如果有更多匹配，获取所有匹配（注意它返回一个列表）

```
>>> page.find_by_text('Tipping the Velvet', first_match=False)
[<data='<a href="catalogue/tipping-the-velvet_99...' parent='<h3><a href="catalogue/tipping-the-velve...'>]
```

获取包含单词 'the' 的所有元素（部分匹配）

```
>>> results = page.find_by_text('the', partial=True, first_match=False)
>>> [i.text for i in results]
['A Light in the ...',
 'Tipping the Velvet',
 'The Requiem Red',
 'The Dirty Little Secrets ...',
 'The Coming Woman: A ...',
 'The Boys in the ...',
 'The Black Maria',
 'Mesaerion: The Best Science ...',
 "It's Only the Himalayas"]
```

搜索是不区分大小写的，所以这些结果包括 `The`，而不仅仅是小写的 `the`；让我们将搜索限制为仅包含 `the` 的元素。

```
>>> results = page.find_by_text('the', partial=True, first_match=False, case_sensitive=True)
>>> [i.text for i in results]
['A Light in the ...',
 'Tipping the Velvet',
 'The Boys in the ...',
 "It's Only the Himalayas"]
```

获取文本内容匹配我的价格正则表达式的第一个元素

```
>>> page.find_by_regex(r'£[\d\.]+')
<data='<p class="price_color">£51.77</p>' parent='<div class="product_price"> <p class="pr...'>
>>> page.find_by_regex(r'£[\d\.]+').text
'£51.77'
```

如果你也传递编译后的正则表达式，它是相同的；Scrapling 将检测输入类型并据此行动：

```
>>> import re
>>> regex = re.compile(r'£[\d\.]+')
>>> page.find_by_regex(regex)
<data='<p class="price_color">£51.77</p>' parent='<div class="product_price"> <p class="pr...'>
>>> page.find_by_regex(regex).text
'£51.77'
```

获取匹配正则表达式的所有元素

```
>>> page.find_by_regex(r'£[\d\.]+', first_match=False)
[<data='<p class="price_color">£51.77</p>' parent='<div class="product_price"> <p class="pr...'>,
 <data='<p class="price_color">£53.74</p>' parent='<div class="product_price"> <p class="pr...'>,
 <data='<p class="price_color">£50.10</p>' parent='<div class="product_price"> <p class="pr...'>,
 <data='<p class="price_color">£47.82</p>' parent='<div class="product_price"> <p class="pr...'>,
 ...]
```

依此类推...

查找当前位置和属性与当前元素相似的所有元素。对于我们的情况，在匹配时忽略 'title' 属性

```
>>> element = page.find_by_text('Tipping the Velvet')
>>> element.find_similar(ignore_attributes=['title'])
[<data='<a href="catalogue/a-light-in-the-attic_...' parent='<h3><a href="catalogue/a-light-in-the-at...'>,
 <data='<a href="catalogue/soumission_998/index....' parent='<h3><a href="catalogue/soumission_998/in...'>,
 <data='<a href="catalogue/sharp-objects_997/ind...' parent='<h3><a href="catalogue/sharp-objects_997...'>,
...]
```

注意元素的数量是 19，而不是 20，因为当前元素不包含在结果中。

```
>>> len(element.find_similar(ignore_attributes=['title']))
19
```

从所有相似元素获取 `href` 属性

```
>>> [
    element.attrib['href']
    for element in element.find_similar(ignore_attributes=['title'])
]
['catalogue/a-light-in-the-attic_1000/index.html',
 'catalogue/soumission_998/index.html',
 'catalogue/sharp-objects_997/index.html',
 ...]
```

为了增加一点复杂性，假设我们想由于某种原因使用该元素作为起点来获取所有书籍数据

```
>>> for product in element.parent.parent.find_similar():
        print({
            "name": product.css('h3 a::text').get(),
            "price": product.css('.price_color')[0].re_first(r'[\d\.]+'),
            "stock": product.css('.availability::text').getall()[-1].clean()
        })
{'name': 'A Light in the ...', 'price': '51.77', 'stock': 'In stock'}
{'name': 'Soumission', 'price': '50.10', 'stock': 'In stock'}
{'name': 'Sharp Objects', 'price': '47.82', 'stock': 'In stock'}
...
```

### 高级示例

查看更多使用 `find_similar` 方法的高级或真实世界示例。

#### 电子商务产品提取

```python
def extract_product_grid(page):
    # 查找第一个产品卡片
    first_product = page.find_by_text('Add to Cart').find_ancestor(
        lambda e: e.has_class('product-card')
    )

    # 查找相似的产品卡片
    products = first_product.find_similar()

    return [
        {
            'name': p.css('h3::text').get(),
            'price': p.css('.price::text').re_first(r'\d+\.\d{2}'),
            'stock': 'In stock' in p.text,
            'rating': p.css('.rating')[0].attrib.get('data-rating')
        }
        for p in products
    ]
```

#### 表格行提取

```python
def extract_table_data(page):
    # 查找第一个数据行
    first_row = page.css('table tbody tr')[0]

    # 查找相似的行
    rows = first_row.find_similar()

    return [
        {
            'column1': row.css('td:nth-child(1)::text').get(),
            'column2': row.css('td:nth-child(2)::text').get(),
            'column3': row.css('td:nth-child(3)::text').get()
        }
        for row in rows
    ]
```

#### 表单字段提取

```python
def extract_form_fields(page):
    # 查找第一个表单字段容器
    first_field = page.css('input')[0].find_ancestor(
        lambda e: e.has_class('form-field')
    )

    # 查找相似的字段容器
    fields = first_field.find_similar()

    return [
        {
            'label': f.css('label::text').get(),
            'type': f.css('input')[0].attrib.get('type'),
            'required': 'required' in f.css('input')[0].attrib
        }
        for f in fields
    ]
```

#### 从网站提取评论

```python
def extract_reviews(page):
    # 查找第一个评论
    first_review = page.find_by_text('Great product!')
    review_container = first_review.find_ancestor(
        lambda e: e.has_class('review')
    )

    # 查找相似的评论
    all_reviews = review_container.find_similar()

    return [
        {
            'text': r.css('.review-text::text').get(),
            'rating': r.attrib.get('data-rating'),
            'author': r.css('.reviewer::text').get()
        }
        for r in all_reviews
    ]
```

## 基于过滤器的搜索

这种搜索方法可以说是 Scrapling 中查找元素的最佳方式，因为它功能强大，并且对于 Web 爬取的新手来说，学习编写选择器比学习更容易。

受 BeautifulSoup 的 `find_all` 函数启发，你可以使用 `find_all` 和 `find` 方法查找元素。这两种方法都可以接受多个过滤器，并返回页面上应用所有这些过滤器的所有元素。

更具体地说：

- 任何传递的字符串都被视为标签名。

- 任何传递的可迭代对象（如 List/Tuple/Set）都被视为标签名的可迭代对象。

- 任何字典都被视为 HTML 元素的映射、属性名和属性值。

- 任何传递的正则表达式模式都用于通过内容过滤元素，就像 `find_by_regex` 方法一样。

- 任何传递的函数都用于过滤元素。

- 任何传递的关键字参数都被视为具有其值的 HTML 元素属性。

它收集所有传递的参数和关键字，并且每个过滤器将其结果以瀑布式的过滤系统传递给下一个过滤器。

它按以下顺序过滤当前页面/元素中的所有元素：

1. 收集所有具有传递的标签名的元素。

2. 收集所有匹配所有传递的属性的元素；如果使用了前一个过滤器，则过滤先前收集的元素。

3. 收集所有匹配所有传递的正则表达式模式的元素；如果使用了前一个（或多个）过滤器，则过滤先前收集的元素。

4. 收集所有满足所有传递的函数的元素；如果使用了前一个（或多个）过滤器，则过滤先前收集的元素。

**注意**：

1. 正如你可能理解的，过滤过程总是从上述过滤顺序中找到的第一个过滤器开始。因此，如果没有传递标签名但传递了属性，则该过程从该步骤（第 2 步）开始，依此类推。

2. 你传递参数的顺序并不重要。唯一考虑的是上述解释的顺序。

查看示例以清除任何混淆 :)

### 示例

```
>>> from scrapling.fetchers import Fetcher
>>> page = Fetcher.get('https://quotes.toscrape.com/')
```

查找所有标签名为 `div` 的元素。

```
>>> page.find_all('div')
[<data='<div class="container"> <div class="row...' parent='<body> <div class="container"> <div clas...'>,
 <data='<div class="row header-box"> <div class=...' parent='<div class="container"> <div class="row...'>,
...]
```

查找所有类等于 `quote` 的 div 元素。

```
>>> page.find_all('div', class_='quote')
[<data='<div class="quote" itemscope itemtype="h...' parent='<div class="col-md-8"> <div class="quote...'>,
 <data='<div class="quote" itemscope itemtype="h...' parent='<div class="col-md-8"> <div class="quote...'>,
...]
```

与上面相同。

```
>>> page.find_all('div', {'class': 'quote'})
[<data='<div class="quote" itemscope itemtype="h...' parent='<div class="col-md-8"> <div class="quote...'>,
 <data='<div class="quote" itemscope itemtype="h...' parent='<div class="col-md-8"> <div class="quote...'>,
...]
```

查找所有类等于 `quote` 的元素。

```
>>> page.find_all({'class': 'quote'})
[<data='<div class="quote" itemscope itemtype="h...' parent='<div class="col-md-8"> <div class="quote...'>,
 <data='<div class="quote" itemscope itemtype="h...' parent='<div class="col-md-8"> <div class="quote...'>,
...]
```

查找所有类等于 `quote` 且包含元素 `.text` 的 div 元素，其中 `.text` 包含单词 'world'。

```
>>> page.find_all('div', {'class': 'quote'}, lambda e: "world" in e.css('.text::text').get())
[<data='<div class="quote" itemscope itemtype="h...' parent='<div class="col-md-8"> <div class="quote...'>]
```

查找所有具有子元素的元素。

```
>>> page.find_all(lambda element: len(element.children) > 0)
[<data='<html lang="en"><head><meta charset="UTF...'>,
 <data='<head><meta charset="UTF-8"><title>Quote...' parent='<html lang="en"><head><meta charset="UTF...'>,
 <data='<body> <div class="container"> <div clas...' parent='<html lang="en"><head><meta charset="UTF...'>,
...]
```

查找其内容中包含单词 'world' 的所有元素。

```
>>> page.find_all(lambda element: "world" in element.text)
[<data='<span class="text" itemprop="text">"The...' parent='<div class="quote" itemscope itemtype="h...'>,
 <data='<a class="tag" href="/tag/world/page/1/"...' parent='<div class="tags"> Tags: <meta class="ke...'>]
```

查找匹配给定正则表达式的所有 span 元素

```
>>> page.find_all('span', re.compile(r'world'))
[<data='<span class="text" itemprop="text">"The...' parent='<div class="quote" itemscope itemtype="h...'>]
```

查找所有具有类 'quote' 的 div 和 span 元素（没有这样的 span 元素，所以只返回 div）

```
>>> page.find_all(['div', 'span'], {'class': 'quote'})
[<data='<div class="quote" itemscope itemtype="h...' parent='<div class="col-md-8"> <div class="quote...'>,
 <data='<div class="quote" itemscope itemtype="h...' parent='<div class="col-md-8"> <div class="quote...'>,
...]
```

混合使用

```
>>> page.find_all({'itemtype':"http://schema.org/CreativeWork"}, 'div').css('.author::text').getall()
['Albert Einstein',
 'J.K. Rowling',
...]
```

额外专业提示：查找所有其 `href` 属性值以单词 'Einstein' 结尾的元素。

```
>>> page.find_all({'href$': 'Einstein'})
[<data='<a href="/author/Albert-Einstein">(about...' parent='<span>by <small class="author" itemprop=...'>,
 <data='<a href="/author/Albert-Einstein">(about...' parent='<span>by <small class="author" itemprop=...'>,
 <data='<a href="/author/Albert-Einstein">(about...' parent='<span>by <small class="author" itemprop=...'>]
```

另一个专业提示：查找所有其 `href` 属性值中有 '/author/' 的元素

```
>>> page.find_all({'href*': '/author/'})
[<data='<a href="/author/Albert-Einstein">(about...' parent='<span>by <small class="author" itemprop=...'>,
 <data='<a href="/author/J-K-Rowling">(about)</a...' parent='<span>by <small class="author" itemprop=...'>,
 <data='<a href="/author/Albert-Einstein">(about...' parent='<span>by <small class="author" itemprop=...'>,
...]
```

依此类推...

## 生成选择器

你总是可以为任何元素生成 CSS/XPath 选择器，以便在这里或任何其他地方重用，最显著的是，这并不重要你使用什么方法找到了该元素！

为 `url_element` 元素生成一个简短的 CSS 选择器（如果可能，创建一个简短的；否则，它是一个完整的选择器）

```
>>> url_element = page.find({'href*': '/author/'})
>>> url_element.generate_css_selector
'body > div > div:nth-of-type(2) > div > div > span:nth-of-type(2) > a'
```

为 `url_element` 元素从页面开始生成一个完整的 CSS 选择器......

（后续内容更多，包括 XPath 选择器生成等，由于篇幅限制这里省略部分内容）

## 与正则表达式一起使用选择器

Scrapling 的选择器也可以与正则表达式一起使用，但与标准 CSS/XPath 选择器相比，这通常通过 `find_by_regex` 或 `find_by_text` 方法处理，因为它们为此目的进行了优化。

如果你需要将正则表达式与标准选择器一起使用，你可以：

```python
# 使用 CSS 选择器获取所有元素，然后过滤
for element in page.css('.product'):
    if element.css('.title::text').re_first(r'^Product \d+'):
        # 处理匹配的元素
        pass
```

但通常，使用 `find_all` 或 `find_by_regex` 方法更高效，因为它们在底层进行了优化。

## 总结

Scrapling 提供了多种强大的方法来查找和选择元素：

1. **CSS/XPath 选择器**：适合精确的结构化选择
2. **基于文本的查找**：`find_by_text` 和 `find_by_regex` 用于内容驱动的选择
3. **基于过滤器的查找**：`find` 和 `find_all` 提供灵活的多条件过滤
4. **相似元素查找**：`find_similar` 用于智能的重复元素识别

根据你的具体需求选择合适的方法，你可以组合使用这些技术来实现强大的 Web 爬取解决方案。

---

# 解析器主要类 (Parsing Main Classes)

## 概述

本页介绍 Scrapling 解析器的主要类及其使用方法。

## Selector 类

`Selector` 是 Scrapling 解析器的核心类，用于解析 HTML/XML 内容并提供元素选择功能。

### 参数说明

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `html_content` | `str` | *必填* | HTML 内容 |
| `url` | `str` | `None` | 可选的 URL（用于相对链接解析） |
| `adaptive` | `bool` | `False` | 启用自适应抓取功能 |
| `encoding` | `str` | `'UTF-8'` | 内容编码 |
| `keep_comments` | `bool` | `False` | 保留注释 |
| `keep_cdata` | `bool` | `False` | 保留 CDATA 部分 |

### 属性

#### 基本属性

- **`tag`**: 元素的标签名

- **`attrib`**: 元素属性字典，支持字典式访问

```python
element['class']  # 获取 class 属性
element.attrib.get('href', '')  # 安全获取属性
```

- **`text`**: 元素的直接文本内容（不包括子元素）

- **`html_content`**: 元素的 HTML 内容

- **`html`**: `html_content` 的别名

#### 文本提取

- **`get_all_text(separator='\n', strip=False, ignore_tags=('script', 'style'))`**: 递归获取所有文本内容

```python
# 获取元素内的所有文本，包括子元素
full_text = element.get_all_text(separator=' ', strip=True)

# 获取干净的文本，忽略 script 和 style 标签
clean_text = element.get_all_text()
```

- **`get_text(separator='', strip=False)`**: 获取文本内容（已废弃，使用 `get_all_text`）

#### DOM 遍历

- **`parent`**: 父元素

- **`children`**: 直接子元素列表

- **`siblings`**: 兄弟元素列表

- **`iterancestors()`**: 迭代所有祖先元素

```python
# 查找具有特定类的祖先元素
for ancestor in element.iterancestors():
    if 'container' in ancestor.get('class', []):
        return ancestor
```

- **`find_ancestor(func)`**: 查找满足条件的第一个祖先元素

```python
# 查找具有 'card' 类的祖先元素
card = element.find_ancestor(lambda e: 'card' in e.get('class', []))
```

- **`has_class(class_name)`**: 检查元素是否包含指定的类

```python
if element.has_class('product'):
    print("这是一个产品元素")
```

- **`has_classes(class_list)`**: 检查元素是否包含所有指定的类

```python
if element.has_classes(['product', 'featured']):
    print("这是一个特色产品")
```

### 方法

#### 选择器方法

- **`css(selector)`**: 使用 CSS 选择器查找元素

- **`xpath(selector)`**: 使用 XPath 选择器查找元素

```python
titles = page.css('h1.title')
links = page.xpath('//a[@href]')
```

#### 文本搜索方法

- **`find_by_text(text, partial=False, case_sensitive=False, clean_match=True, first_match=True)`**: 根据文本内容查找元素

- **`find_by_regex(pattern, first_match=True, case_sensitive=False, clean_match=True)`**: 根据正则表达式查找元素

```python
# 查找包含特定文本的元素
element = page.find_by_text('Click here', partial=True)

# 使用正则表达式查找价格
price_element = page.find_by_regex(r'\$\d+\.\d{2}')
```

#### 相似元素查找

- **`find_similar(similarity_threshold=0.2, ignore_attributes=('href', 'src'), match_text=False)`**: 查找与当前元素相似的元素

```python
# 查找相似的产品卡片
first_product = page.css('.product')[0]
all_products = first_product.find_similar()
```

#### 过滤方法

- **`find(*filters, **kwargs)`**: 查找满足条件的元素（返回第一个）

- **`find_all(*filters, **kwargs)`**: 查找所有满足条件的元素

```python
# 查找具有特定类和属性的元素
element = page.find('div', class_='container')

# 查找所有具有特定属性的元素
elements = page.find_all({'data-id': True})
```

#### 格式化

- **`prettify()`**: 格式化输出 HTML

```python
print(element.prettify())
```

#### 链接处理

- **`urljoin(url)`**: 将相对 URL 转换为绝对 URL

```python
absolute_url = page.urljoin('/about')
# 结果: https://example.com/about
```

## Selectors 类

`Selectors` 是 `Selector` 对象的列表容器，继承自 Python 的 `list`。

### 数据提取方法

- **`get(default=None)`**: 获取第一个选择器的结果

```python
title = page.css('h1::text').get('No title')
```

- **`getall()`**: 获取所有选择器的结果

```python
all_titles = page.css('h2::text').getall()
```

### 链式操作

`Selectors` 对象支持链式调用选择器方法：

```python
# 获取所有产品中的标题
titles = page.css('.product').css('h3::text')

# 等价于
titles = page.css('.product h3::text')
```

### 列表操作

由于继承自 `list`，支持所有列表操作：

```python
elements = page.css('.product')
first = elements[0]  # 第一个元素
last = elements[-1]  # 最后一个元素
count = len(elements)  # 元素数量

# 遍历
for element in elements:
    print(element.css('.title::text').get())
```

## TextHandler 类

`TextHandler` 是文本内容的包装类，提供额外的文本处理功能。

### 使用方法

通常通过 `Selector` 的 `text` 属性间接使用：

```python
element = page.css('p.description')[0]
text = element.text  # 返回 TextHandler 对象
```

### 方法

- **`clean()`**: 清理文本（去除多余空格和换行）

```python
clean_text = element.text.clean()
```

- **`json()`**: 尝试将文本解析为 JSON

```python
# 如果元素包含 JSON 数据
data = element.text.json()
```

## TextHandlers 类

`TextHandlers` 是 `TextHandler` 对象的列表容器。

### 数据提取

- **`get(default=None)`**: 获取第一个文本

- **`getall()`**: 获取所有文本

```python
all_texts = page.css('p::text').getall()
```

## AttributesHandler 类

`AttributesHandler` 是元素属性的包装类，提供便捷的属性访问方法。

### 属性访问

```python
element = page.css('a')[0]
attributes = element.attrib  # 返回 AttributesHandler

# 字典式访问
href = attributes['href']
class_name = attributes.get('class', '')
```

### 方法

- **`get(key, default=None)`**: 安全获取属性值

- **`keys()`**: 获取所有属性名

- **`values()`**: 获取所有属性值

- **`items()`**: 获取所有属性键值对

```python
for name, value in element.attrib.items():
    print(f"{name}: {value}")
```

## 使用示例

### 基础选择

```python
from scrapling import Selector

html = """
<html>
    <body>
        <div class="container">
            <h1>Welcome</h1>
            <p class="description">This is a test page.</p>
            <ul class="items">
                <li class="item">Item 1</li>
                <li class="item">Item 2</li>
            </ul>
        </div>
    </body>
</html>
"""

page = Selector(html)

# 选择元素
title = page.css('h1::text').get()
print(title)  # 'Welcome'

description = page.css('.description::text').get()
print(description)  # 'This is a test page.'

items = page.css('.item::text').getall()
print(items)  # ['Item 1', 'Item 2']
```

### 遍历和搜索

```python
# 查找特定元素
container = page.css('.container')[0]

# 访问子元素
h1 = container.css('h1::text').get()

# 访问兄弟元素
siblings = container.siblings

# 查找祖先
ancestor = container.find_ancestor(lambda e: e.tag == 'body')
```

### 文本搜索

```python
# 根据文本查找元素
element = page.find_by_text('Welcome')
print(element.text)  # 'Welcome'

# 部分匹配
element = page.find_by_text('test', partial=True)
print(element.text)  # 'This is a test page.'

# 使用正则表达式
import re
element = page.find_by_regex(r'Item \d+')
print(element.text)  # 'Item 1'（第一个匹配）
```

### 相似元素查找

```python
# 查找相似元素
first_item = page.css('.item')[0]
all_items = first_item.find_similar()

for item in all_items:
    print(item.text)
```

### 链接处理

```python
from scrapling import Selector

html = '<a href="/about">About Us</a>'
page = Selector(html, url='https://example.com')

link = page.css('a')[0]
absolute_url = page.urljoin(link.attrib['href'])
print(absolute_url)  # 'https://example.com/about'
```

### JSON 解析

```python
html = '''
<div id="data" data-json='{"name": "John", "age": 30}'>
    User data
</div>
'''

page = Selector(html)
data_div = page.css('#data')[0]

# 获取属性并解析 JSON
import json
data = json.loads(data_div.attrib['data-json'])
print(data)  # {'name': 'John', 'age': 30}
```

### 格式化输出

```python
element = page.css('.container')[0]
print(element.prettify())
```

输出：
```html
<div class="container">
  <h1>Welcome</h1>
  <p class="description">This is a test page.</p>
  <ul class="items">
    <li class="item">Item 1</li>
    <li class="item">Item 2</li>
  </ul>
</div>
```

## 性能考虑

1. **链式选择器**: 尽量使用链式选择器而非多次调用：

```python
# 好
titles = page.css('.product').css('.title::text')

# 避免（性能较差）
products = page.css('.product')
titles = []
for product in products:
    titles.append(product.css('.title::text').get())
```

2. **使用 `get()` 而非列表索引**:

```python
# 好
title = page.css('h1::text').get()

# 避免（如果不存在会出错）
title = page.css('h1::text')[0]
```

3. **批量处理**: 对于大量元素，使用 `getall()` 一次性获取：

```python
# 好
all_texts = page.css('.item::text').getall()

# 避免（多次查询）
texts = []
for item in page.css('.item'):
    texts.append(item.text)
```

## 总结

Scrapling 的主要类提供了强大而灵活的 HTML 解析能力：

- **Selector**: 核心解析引擎，提供完整的 DOM 操作和选择功能
- **Selectors**: 批量元素操作，支持链式调用
- **TextHandler/TextHandlers**: 文本内容处理
- **AttributesHandler**: 属性管理和访问

通过合理使用这些类和方法，可以高效地从网页中提取所需的数据。

---

# 自适应抓取 (Adaptive Scraping)

## 概述

自适应抓取功能是 Scrapling 最显著的功能之一，它允许爬虫在网站结构发生变化后仍能定位到目标元素。这个功能不需要 AI 支持，而是通过保存和匹配元素的唯一属性来实现。

这个功能特别适合那些网站结构经常变化的场景，或者当你想要一个更健壮的爬取解决方案时。

## 工作原理

自适应抓取通过以下步骤工作：

### 1. 保存阶段（学习）

当首次访问页面时，系统会保存目标元素的唯一属性特征：

- **标签名**：元素的 HTML 标签（如 `div`、`span` 等）
- **文本内容**：元素的直接文本内容
- **属性**：元素的所有 HTML 属性
- **父元素信息**：直接父元素的特征
- **路径信息**：元素在 DOM 树中的路径
- **兄弟元素**：相邻元素的信息

这些信息被保存到存储系统中（默认使用 SQLite 数据库）。

### 2. 查找阶段（正常抓取）

当后续抓取页面时：

1. 首先尝试使用原始选择器查找元素
2. 如果找不到元素，则从存储中检索保存的特征
3. 在当前页面中查找与保存特征最相似的元素
4. 返回匹配度最高的元素

### 相似度计算

相似度基于以下因素：

- DOM 树深度的匹配
- 标签名的匹配
- 父标签名的匹配
- 祖父标签名的匹配
- 属性的模糊匹配

## 使用方法

### 基本用法

#### 方法1：通过 Selector 启用

```python
from scrapling import Selector

html = """
<html>
    <body>
        <div class="product-card">
            <h2 class="product-title">Sample Product</h2>
            <span class="price">$99.99</span>
        </div>
    </body>
</html>
"""

# 启用自适应抓取
page = Selector(html, adaptive=True, url='https://example.com/products')

# 查找并保存元素
title = page.css('.product-title')
# 元素属性自动保存
```

#### 方法2：通过 Fetcher 启用

```python
from scrapling.fetchers import Fetcher

# 启用自适应抓取
page = Fetcher.get(
    'https://example.com/products',
    adaptive=True
)

# 使用选择器，元素属性自动保存
products = page.css('.product-card')
```

### 手动保存和恢复元素

#### 保存元素

```python
from scrapling import Selector

page = Selector(html, adaptive=True, url='https://example.com')

# 查找元素
product_title = page.css('.product-title')[0]

# 手动保存元素，设置标识符
page.save(product_title, identifier='product_title')

# 也可以保存多个元素
for i, product in enumerate(page.css('.product-card')):
    page.save(product, identifier=f'product_{i}')
```

#### 恢复元素

```python
# 后续抓取时
page = Selector(new_html, adaptive=True, url='https://example.com')

# 通过标识符检索元素
element = page.retrieve('product_title')

# 如果原始选择器失效，自动尝试相似元素匹配
if element:
    print(f"找到元素: {element.text}")
else:
    print("未找到匹配元素")
```

### 使用 CSS/XPath 选择器

```python
from scrapling import Selector

page = Selector(html, adaptive=True, url='https://example.com')

# 使用 CSS 选择器，启用自动保存
title = page.css('.product-title::text', auto_save=True)

# 使用 XPath 选择器
price = page.xpath('//span[@class="price"]', auto_save=True)
```

## 高级用法

### 自定义存储

默认情况下，自适应抓取使用 SQLite 数据库。你可以使用自定义存储：

```python
from scrapling import Selector

# 使用自定义存储参数
page = Selector(
    html,
    adaptive=True,
    storage='custom_storage',
    storage_args={
        'db_path': './my_custom_db.sqlite',
        'table_name': 'adaptive_data'
    }
)
```

### 设置相似度阈值

```python
# 调整相似度阈值（默认 0.2）
page = Selector(
    html,
    adaptive=True,
    similarity_threshold=0.3  # 更严格的匹配
)
```

### 忽略特定属性

```python
# 在匹配时忽略某些属性
element = page.find_similar(
    ignore_attributes=['href', 'src', 'data-id']
)
```

## 使用场景

### 场景1：电商网站

电商网站经常更改产品卡片的类名和结构。

```python
from scrapling.fetchers import Fetcher

# 首次抓取，保存产品特征
page = Fetcher.get('https://shop.example.com/products', adaptive=True)
first_product = page.css('.product')[0]

# 保存产品标题、价格等元素的标识符
page.save(page.css('.product-title')[0], 'product_title')
page.save(page.css('.price')[0], 'product_price')

# 后续抓取时，即使类名变化也能找到元素
new_page = Fetcher.get('https://shop.example.com/products', adaptive=True)
title = new_page.retrieve('product_title')
price = new_page.retrieve('product_price')
```

### 场景2：新闻网站

新闻网站经常更改文章布局和类名。

```python
from scrapling.fetchers import Fetcher

page = Fetcher.get('https://news.example.com/articles', adaptive=True)

# 保存关键元素的标识符
article_content = page.css('.article-content')[0]
article_title = page.css('.article-title')[0]
article_date = page.css('.publish-date')[0]

page.save(article_content, 'article_content')
page.save(article_title, 'article_title')
page.save(article_date, 'article_date')
```

### 场景3：论坛和社区

论坛经常更新主题和布局。

```python
from scrapling.fetchers import Fetcher

page = Fetcher.get('https://forum.example.com/posts', adaptive=True)

# 保存论坛帖子的关键元素
post_title = page.css('.post-title')[0]
post_author = page.css('.author-name')[0]
post_content = page.css('.post-body')[0]

page.save(post_title, 'post_title')
page.save(post_author, 'post_author')
page.save(post_content, 'post_content')
```

## 故障排除

### 问题1：没有匹配结果

**可能原因**：
1. 没有正确保存元素特征
2. 页面结构发生重大变化
3. 使用的标识符不正确

**解决方案**：
```python
# 检查是否保存了数据
element = page.retrieve('my_identifier')

if not element:
    # 尝试不同的选择器重新保存
    element = page.css('.new-selector')[0]
    page.save(element, 'my_identifier')
```

### 问题2：匹配到错误的元素

**可能原因**：
1. 页面有多个相似元素
2. 保存的元素特征不够具体
3. 相似度阈值设置得太低

**解决方案**：
```python
# 使用更具体的选择器
specific_element = page.css('.container .product.featured')[0]
page.save(specific_element, 'specific_product')

# 或提高相似度阈值
page = Selector(html, adaptive=True, similarity_threshold=0.4)
```

### 问题3：性能问题

**可能原因**：
1. 保存了太多元素
2. 数据库查询慢

**解决方案**：
```python
# 只保存关键元素，避免保存所有元素
# 好的做法：
page.save(page.css('.main-title')[0], 'title')

# 避免：
for element in page.css('*'):
    page.save(element, f'element_{index}')
```

## 最佳实践

### 1. 选择有意义的标识符

```python
# 好的标识符
page.save(product_title, 'product_title')
page.save(product_price, 'product_price')

# 避免使用无意义的标识符
page.save(element, 'element_1')
page.save(element, 'element_2')
```

### 2. 保存稳定的元素

优先保存结构相对稳定的元素，如：
- 页面标题
- 主要内容区域
- 关键数据字段

避免保存可能频繁变化的元素，如：
- 广告内容
- 临时通知
- 动态生成的列表

### 3. 定期验证和更新

```python
import time

# 定期检查和更新保存的元素
def verify_adaptive_elements():
    page = Fetcher.get('https://example.com', adaptive=True)

    for identifier in ['product_title', 'product_price', 'product_link']:
        element = page.retrieve(identifier)
        if not element:
            print(f"警告：找不到标识符 '{identifier}'，需要重新学习")
            # 触发重新学习流程
            return False

    return True

# 定期运行验证
if not verify_adaptive_elements():
    # 触发警报或重新学习
    pass
```

### 4. 结合其他选择器方法

```python
# 自适应抓取可以作为后备方案
primary_selector = '.new-product-class'
backup_identifier = 'product_title'

# 首先尝试标准选择器
title = page.css(primary_selector)

# 如果失败，使用自适应抓取
if not title:
    title = page.retrieve(backup_identifier)
```

## 限制和注意事项

1. **不是万能解决方案**：自适应抓取不能解决所有结构变化问题，特别是当页面完全重构时。

2. **需要初始学习**：必须在页面结构相对稳定时首次保存元素特征。

3. **存储依赖**：需要维护存储数据库，确保数据持久化。

4. **计算开销**：相似度计算需要额外的处理时间，可能影响性能。

5. **不适用于动态内容**：对于完全由 JavaScript 动态生成的内容，效果可能有限。

## 与其他库的对比

### 相比 AutoScraper

| 特性 | Scrapling | AutoScraper |
|--------|-----------|-------------|
| 速度 | 快（2.39ms） | 较慢（12.45ms） |
| 集成度 | 与完整解析器集成 | 独立功能 |
| 灵活性 | 可与所有选择器配合使用 | 主要基于相似性 |
| 存储 | 可自定义存储系统 | 固定存储方式 |

### 相比基于 AI 的方案

| 特性 | Scrapling 自适应抓取 | AI 方案 |
|--------|-------------------|---------|
| 成本 | 免费 | 需要付费 API |
| 速度 | 快 | 较慢（需要网络请求） |
| 隐私 | 本地处理 | 数据发送到外部 |
| 准确性 | 基于结构特征 | 基于 NLP 理解 |

## 总结

自适应抓取是 Scrapling 的核心优势之一，它提供了：

1. **健壮性**：应对网站结构变化
2. **高效性**：无需 AI 支持即可实现智能匹配
3. **易用性**：简单的 API，易于集成
4. **灵活性**：可与所有选择器方法配合使用

对于需要长期维护的爬虫项目，自适应抓取是一个强大的工具，可以显著减少因网站结构变化导致的维护工作。

---

# 抓取器基础 (Fetchers Basics)

## 概述

Scrapling 提供三种不同类型的抓取器，每种都适用于不同的场景。理解每种抓取器的特性和使用场景是高效 Web 爬取的关键。

## 抓取器类型对比

### Fetcher（基础抓取器）

**用途**：适用于基本的 HTTP 请求

**特点**：
- ✅ 极快的速度（最快）
- ✅ 低内存占用
- ✅ 支持浏览器指纹伪装
- ✅ 支持 HTTP/3
- ❌ 不支持 JavaScript
- ❌ 无法绕过复杂的反爬保护

**适用场景**：
- 静态网站
- API 端点
- 简单的 HTML 页面
- 不需要 JavaScript 渲染的内容

### DynamicFetcher（动态抓取器）

**用途**：适用于动态网站和浏览器自动化

**特点**：
- ✅ 支持 JavaScript 执行
- ✅ 完整的浏览器 API（基于 Playwright）
- ✅ 支持网络控制
- ✅ 支持页面操作
- ⚠️ 速度中等
- ⚠️ 内存占用较高
- ❌ 反爬保护有限

**适用场景**：
- 单页应用（SPA）
- 需要 JavaScript 才能加载的内容
- 复杂的用户交互
- AJAX 动态加载的数据

### StealthyFetcher（隐身抓取器）

**用途**：适用于具有强反爬保护的网站

**特点**：
- ✅ 自动绕过 Cloudflare Turnstile/Interstitial
- ✅ 防止 WebRTC 泄漏
- ✅ Canvas 指纹噪声
- ✅ 修补 headless 检测
- ✅ 完整的浏览器 API
- ⚠️ 速度中等
- ⚠️ 内存占用较高

**适用场景**：
- 受 Cloudflare 保护
- 具有反机器人检测的网站
- 需要绕过 WAF 的场景
- 严格的浏览器指纹检测

## 详细对比表

| 特性 | Fetcher | DynamicFetcher | StealthyFetcher |
|--------|---------|----------------|-----------------|
| **速度** | 🐇🐇🐇🐇🐇 (5星) | 🐇🐇🐇 (3星) | 🐇🐇🐇 (3星) |
| **隐身性** | ⭐⭐ (2星) | ⭐⭐⭐ (3星) | ⭐⭐⭐⭐⭐⭐ (5星) |
| **反反爬选项** | ⭐⭐ (2星) | ⭐⭐⭐ (3星) | ⭐⭐⭐⭐⭐⭐ (5星) |
| **JavaScript 支持** | ❌ | ✅ | ✅ |
| **内存占用** | ⭐ (1星) | ⭐⭐⭐ (3星) | ⭐⭐⭐ (3星) |
| **适用场景** | 基础 HTTP 请求 | 动态网站、简单自动化 | 带强保护的动态网站 |
| **浏览器** | ❌ | Chromium/Chrome | Chromium/Chrome |
| **浏览器 API** | ❌ | PlayWright | PlayWright |
| **Cloudflare 绕过** | ❌ | ❌ | ✅ |
| **WebRTC 防护** | N/A | ❌ | ✅ |
| **Canvas 噪声** | N/A | ❌ | ✅ |

## 选择指南

### 使用 Fetcher 如果

网站满足以下条件：
- 内容完全通过 HTML 加载
- 不需要 JavaScript 渲染
- 没有复杂的反爬保护
- 需要高性能和低延迟

**示例**：
```python
from scrapling.fetchers import Fetcher

# 快速抓取静态页面
page = Fetcher.get('https://example.com/products')
products = page.css('.product')

# 模拟浏览器指纹
page = Fetcher.get(
    'https://example.com',
    impersonate='chrome',
    stealthy_headers=True
)
```

### 使用 DynamicFetcher 如果

网站满足以下条件：
- 内容通过 JavaScript 动态加载
- 是单页应用（SPA）
- 需要等待网络请求完成
- 需要执行页面操作（滚动、点击等）
- 有一定的反爬保护但不过于严格

**示例**：
```python
from scrapling.fetchers import DynamicFetcher

# 抓取动态页面
page = DynamicFetcher.fetch(
    'https://example.com/dashboard',
    network_idle=True,
    wait_selector='.data-loaded'
)

# 自定义页面操作
def scroll_page(page):
    for _ in range(3):
        page.evaluate('window.scrollBy(0, 1000)')
        page.wait_for_timeout(1000)

page = DynamicFetcher.fetch(
    'https://example.com/infinite-scroll',
    page_action=scroll_page
)
```

### 使用 StealthyFetcher 如果

网站满足以下条件：
- 有 Cloudflare 保护
- 有严格的反机器人检测
- 使用 WebRTC 进行指纹识别
- 使用 Canvas 指纹检测
- DynamicFetcher 无法绕过的保护

**示例**：
```python
from scrapling.fetchers import StealthyFetcher

# 绕过 Cloudflare
page = StealthyFetcher.fetch(
    'https://protected-site.com',
    solve_cloudflare=True,
    block_webrtc=True,
    hide_canvas=True
)

# 完整的隐身配置
page = StealthyFetcher.fetch(
    'https://protected-site.com',
    solve_cloudflare=True,
    block_webrtc=True,
    hide_canvas=True,
    google_search=True,
    real_chrome=True
)
```

## 解析器配置

### 全局配置

```python
from scrapling.fetchers import Fetcher

# 全局配置所有抓取器
Fetcher.configure(
    adaptive=True,        # 启用自适应抓取
    keep_comments=False,    # 不保留注释
    keep_cdata=False      # 不保留 CDATA
)

# 后续所有请求都会使用此配置
page = Fetcher.get('https://example.com')
```

### 单次请求配置

```python
# 仅对特定请求配置
page = Fetcher.get(
    'https://example.com',
    selector_config={
        'adaptive': True,
        'keep_comments': False
    }
)
```

## Response 对象

所有抓取器都返回 `Response` 对象，它继承自 `Selector` 并添加了响应特定的属性。

### 基本属性

```python
page = Fetcher.get('https://example.com')

# HTTP 状态码
print(page.status)  # 200, 404, 500 等

# 响应头
print(page.headers)
# {'Content-Type': 'text/html', 'Content-Length': '12345'}

# 请求头
print(page.request_headers)

# Cookies
print(page.cookies)
# {'session_id': 'abc123'}

# 重定向历史
print(page.history)

# 原始响应体（bytes）
print(page.body)
```

### Selector 功能

由于继承自 `Selector`，Response 对象支持所有解析器方法：

```python
# 选择器方法
title = page.css('h1::text').get()
links = page.xpath('//a/@href').getall()

# 文本搜索
element = page.find_by_text('Click here')

# 相似元素查找
products = page.css('.product')[0].find_similar()
```

## 性能考虑

### Fetcher 性能

```python
# 批量请求（使用 AsyncFetcher）
from scrapling.fetchers import AsyncFetcher
import asyncio

async def fetch_multiple():
    urls = [
        'https://example.com/page1',
        'https://example.com/page2',
        'https://example.com/page3'
    ]
    results = await asyncio.gather(*[
        AsyncFetcher.get(url) for url in urls
    ])
    return results

results = asyncio.run(fetch_multiple())
```

### DynamicFetcher 性能

```python
# 禁用不必要的资源
page = DynamicFetcher.fetch(
    'https://example.com',
    disable_resources=True,  # 不加载图片、字体等
    network_idle=True      # 等待网络空闲
)

# 使用会话复用浏览器
from scrapling.fetchers import DynamicSession

with DynamicSession(max_pages=5) as session:
    pages = [session.fetch(url) for url in urls]
```

### StealthyFetcher 性能

```python
# 只在必要时启用隐身功能
page = StealthyFetcher.fetch(
    'https://example.com',
    solve_cloudflare=True,  # 只启用需要的保护
    # 避免不必要的额外保护以提升性能
)

# 使用会话减少启动开销
from scrapling.fetchers import StealthySession

with StealthySession() as session:
    pages = [session.fetch(url) for url in urls]
```

## 最佳实践

### 1. 从简单开始

```python
# 首先尝试 Fetcher
try:
    page = Fetcher.get(url)
    data = page.css('.content')
except:
    # 如果失败，尝试 DynamicFetcher
    page = DynamicFetcher.fetch(url)
    data = page.css('.content')
```

### 2. 监控和降级

```python
def fetch_with_retry(url, max_retries=3):
    for attempt in range(max_retries):
        try:
            # 尝试 Fetcher
            page = Fetcher.get(url)
            if page.status == 200:
                return page
        except:
            # 降级到 DynamicFetcher
            page = DynamicFetcher.fetch(url)
            if page.status == 200:
                return page

    raise Exception(f"Failed after {max_retries} attempts")
```

### 3. 合理选择抓取器

```python
# 根据网站类型选择
if is_static_site(url):
    return Fetcher.get(url)
elif is_dynamic_site(url):
    return DynamicFetcher.fetch(url)
elif is_protected_site(url):
    return StealthyFetcher.fetch(url)
```

## 总结

选择正确的抓取器是成功 Web 爬取的关键：

1. **Fetcher**：最快的选择，适用于静态内容
2. **DynamicFetcher**：平衡性能和功能，适用于动态内容
3. **StealthyFetcher**：最强大的反爬能力，适用于受保护的网站

通过理解每种抓取器的特性和适用场景，你可以优化爬取性能，同时确保数据获取的可靠性。

---

# HTTP 请求 (HTTP Requests)

## 概述

Fetcher 类基于 `curl_cffi` 库，提供了快速、高效的 HTTP 请求能力。它支持各种高级特性，包括浏览器指纹伪装、HTTP/3、会话管理等。

## 基础用法

### 基本请求

```python
from scrapling.fetchers import Fetcher

# GET 请求
page = Fetcher.get('https://example.com')

# POST 请求
page = Fetcher.post(
    'https://api.example.com/data',
    data={'key': 'value'}
)

# PUT 请求
page = Fetcher.put(
    'https://api.example.com/update',
    data={'id': 123, 'name': 'updated'}
)

# DELETE 请求
page = Fetcher.delete('https://api.example.com/item/123')
```

### 带参数的请求

```python
# URL 参数
page = Fetcher.get(
    'https://example.com/search',
    params={'q': 'python', 'page': 1}
)

# 等价于访问：
# https://example.com/search?q=python&page=1
```

### 带请求头的请求

```python
# 自定义请求头
page = Fetcher.get(
    'https://example.com',
    headers={
        'User-Agent': 'Mozilla/5.0',
        'Accept-Language': 'zh-CN,zh;q=0.9'
    }
)
```

## 核心参数

### stealthy_headers

生成真实浏览器的请求头，默认启用。

```python
# 启用（默认）
page = Fetcher.get('https://example.com', stealthy_headers=True)

# 禁用
page = Fetcher.get('https://example.com', stealthy_headers=False)
```

### impersonate

模拟特定浏览器的 TLS 指纹。

```python
# 模拟 Chrome 110
page = Fetcher.get(
    'https://example.com',
    impersonate='chrome110'
)

# 模拟 Firefox
page = Fetcher.get(
    'https://example.com',
    impersonate='firefox'
)

# 可用的浏览器标识
# chrome110, chrome120, chrome131, chrome131_android
# firefox, edge, safari, opera, etc.
```

### http3

启用 HTTP/3 协议以提升性能。

```python
# 启用 HTTP/3
page = Fetcher.get(
    'https://example.com',
    http3=True
)

# 如果 HTTP/3 不支持，自动降级到 HTTP/2
```

### 代理配置

```python
# 单个代理
page = Fetcher.get(
    'https://example.com',
    proxy='http://proxy.example.com:8080'
)

# 带认证的代理
page = Fetcher.get(
    'https://example.com',
    proxy='http://user:pass@proxy.example.com:8080'
)

# 代理列表（轮换）
page = Fetcher.get(
    'https://example.com',
    proxies=[
        'http://proxy1.example.com:8080',
        'http://proxy2.example.com:8080',
        'http://proxy3.example.com:8080'
    ]
)
```

### 请求重试

```python
# 设置重试次数和延迟
page = Fetcher.get(
    'https://example.com',
    retries=5,           # 最多重试 5 次
    retry_delay=2.0        # 每次重试间隔 2 秒
)
```

### 超时设置

```python
# 设置超时时间
page = Fetcher.get(
    'https://example.com',
    timeout=30.0  # 30 秒超时
)
```

## 会话管理

### FetcherSession

`FetcherSession` 用于复用连接池和 cookies，提升性能。

```python
from scrapling.fetchers import FetcherSession

# 创建会话
with FetcherSession(
    impersonate='chrome',
    http3=True
) as session:
    # 复用连接和 cookies
    page1 = session.get('https://example.com/page1')
    page2 = session.get('https://example.com/page2')
    page3 = session.post(
        'https://example.com/api',
        data={'action': 'login'}
    )
```

### 会话特性

1. **连接复用**：重用 TCP 连接
2. **Cookie 持久化**：自动处理 cookies
3. **DNS 缓存**：减少 DNS 查询
4. **更快的请求**：避免重复握手

### 高级会话配置

```python
from scrapling.fetchers import FetcherSession

# 完整配置的会话
session = FetcherSession(
    impersonate='chrome110',
    http3=True,
    timeout=30,
    retries=3,
    stealthy_headers=True,
    headers={
        'Accept': 'text/html,application/xhtml+xml'
    }
)

# 使用会话
with session:
    page = session.get('https://example.com')
```

## 实用示例

### 1. 产品抓取

```python
from scrapling.fetchers import Fetcher

# 抓取产品列表
page = Fetcher.get('https://shop.example.com/products')

products = []
for product in page.css('.product-card'):
    products.append({
        'name': product.css('.title::text').get(),
        'price': product.css('.price::text').get(),
        'image': product.css('img::attr(src)').get()
    })

print(f"抓取到 {len(products)} 个产品")
```

### 2. 文件下载

```python
from scrapling.fetchers import Fetcher

# 下载图片
response = Fetcher.get('https://example.com/image.png')

with open('downloaded_image.png', 'wb') as f:
    f.write(response.body)

# 下载 PDF
response = Fetcher.get('https://example.com/document.pdf')

with open('document.pdf', 'wb') as f:
    f.write(response.body)
```

### 3. 分页处理

```python
from scrapling.fetchers import Fetcher

def scrape_all_pages():
    all_items = []
    page = 1

    while True:
        # 抓取当前页
        response = Fetcher.get(
            'https://example.com/items',
            params={'page': page}
        )

        items = response.css('.item::text').getall()
        all_items.extend(items)

        # 检查是否有下一页
        next_page = response.css('.next-page::attr(href)').get()
        if not next_page:
            break

        page += 1

    return all_items

items = scrape_all_pages()
```

### 4. 表单提交

```python
from scrapling.fetchers import Fetcher

# GET 表单
response = Fetcher.get(
    'https://example.com/search',
    params={
        'query': 'laptop',
        'category': 'electronics',
        'price_range': '500-1000'
    }
)

# POST 表单
response = Fetcher.post(
    'https://example.com/contact',
    data={
        'name': 'John Doe',
        'email': 'john@example.com',
        'message': 'Hello!'
    }
)

# JSON 数据提交
import json
response = Fetcher.post(
    'https://api.example.com/data',
    data=json.dumps({'key': 'value'}),
    headers={'Content-Type': 'application/json'}
)
```

### 5. API 请求

```python
from scrapling.fetchers import Fetcher
import json

# REST API 调用
response = Fetcher.get(
    'https://api.example.com/users',
    headers={
        'Authorization': 'Bearer token123',
        'Accept': 'application/json'
    }
)

users = json.loads(response.body)
for user in users:
    print(f"用户: {user['name']}")
```

### 6. 处理重定向

```python
from scrapling.fetchers import Fetcher

response = Fetcher.get('https://example.com/redirect')

# 查看重定向历史
print(f"最终 URL: {response.url}")
print(f"重定向次数: {len(response.history)}")

for redirect in response.history:
    print(f"从 {redirect.url} 重定向到 {response.url}")
```

## 错误处理

```python
from scrapling.fetchers import Fetcher
import requests.exceptions

try:
    response = Fetcher.get('https://example.com')

    # 检查状态码
    if response.status == 200:
        print("成功获取页面")
    elif response.status == 404:
        print("页面不存在")
    elif response.status == 403:
        print("访问被拒绝")
    elif response.status == 500:
        print("服务器错误")

except requests.exceptions.Timeout:
    print("请求超时")
except requests.exceptions.ConnectionError:
    print("连接错误")
except Exception as e:
    print(f"未知错误: {e}")
```

## 性能优化

### 1. 使用会话

```python
# 不推荐（每次都创建新连接）
for url in urls:
    Fetcher.get(url)

# 推荐（复用连接）
with FetcherSession() as session:
    for url in urls:
        session.get(url)
```

### 2. 批量请求

```python
from scrapling.fetchers import AsyncFetcher
import asyncio

async def fetch_all():
    tasks = [AsyncFetcher.get(url) for url in urls]
    results = await asyncio.gather(*tasks)
    return results

results = asyncio.run(fetch_all())
```

### 3. 合理的超时和重试

```python
# 根据网络条件调整
response = Fetcher.get(
    'https://example.com',
    timeout=15.0,   # 合理的超时
    retries=3,       # 适中的重试次数
    retry_delay=1.0   # 较短的延迟
)
```

## 高级用法

### 自定义 User-Agent

```python
import random

user_agents = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)',
    'Mozilla/5.0 (X11; Linux x86_64)'
]

page = Fetcher.get(
    'https://example.com',
    headers={
        'User-Agent': random.choice(user_agents)
    }
)
```

### Cookie 管理

```python
from scrapling.fetchers import Fetcher

# 发送自定义 cookies
page = Fetcher.get(
    'https://example.com',
    cookies={
        'session_id': 'abc123',
        'user_token': 'xyz789'
    }
)

# 获取响应 cookies
response_cookies = page.cookies
print(f"Set-Cookie: {response_cookies}")
```

### 请求头注入

```python
# 添加 Referer
page = Fetcher.get(
    'https://example.com/page',
    headers={
        'Referer': 'https://example.com/home'
    }
)

# 添加自定义认证
page = Fetcher.get(
    'https://api.example.com/data',
    headers={
        'X-API-Key': 'your_api_key',
        'X-Client-Version': '1.0'
    }
)
```

## 总结

Fetcher 类提供了强大而灵活的 HTTP 请求功能：

1. **高性能**：基于 `curl_cffi`，速度快
2. **丰富的特性**：支持浏览器指纹、HTTP/3、代理等
3. **会话支持**：`FetcherSession` 提供连接复用
4. **易用性**：简洁的 API，易于集成

对于大多数静态网站和 API 调用，Fetcher 是最佳选择。对于需要 JavaScript 的动态网站，请使用 `DynamicFetcher` 或 `StealthyFetcher`。

---

# 动态网站 (Dynamic Websites)

## 概述

DynamicFetcher 类基于 Playwright，提供了强大的动态网站抓取能力。它支持完整的 JavaScript 执行、浏览器自动化和网络控制。

## 基础用法

### 基本请求

```python
from scrapling.fetchers import DynamicFetcher

# 抓取动态网站
page = DynamicFetcher.fetch('https://example.com')
```

### 无头模式

```python
# 无头模式（默认）
page = DynamicFetcher.fetch(
    'https://example.com',
    headless=True
)

# 有头模式（可见浏览器）
page = DynamicFetcher.fetch(
    'https://example.com',
    headless=False
)
```

## 运行模式

DynamicFetcher 支持三种运行模式：

### 1. Vanilla Playwright（默认）

使用内置的 Chromium 浏览器。

```python
page = DynamicFetcher.fetch('https://example.com')
```

### 2. Real Chrome

使用本地安装的真实 Chrome 浏览器。

```python
page = DynamicFetcher.fetch(
    'https://example.com',
    real_chrome=True
)
```

**优点**：
- 与真实用户完全一致
- 支持所有 Chrome 扩展
- 更好的网站兼容性

**缺点**：
- 需要 Chrome 已安装
- 可能会打开浏览器窗口

### 3. CDP 连接

连接到远程 Chrome DevTools Protocol。

```python
page = DynamicFetcher.fetch(
    'https://example.com',
    cdp_url='ws://localhost:9222'
)
```

**用途**：
- 调试和开发
- 连接到现有浏览器实例
- 远程浏览器控制

## 核心参数

### 资源控制

```python
# 禁用不必要的资源
page = DynamicFetcher.fetch(
    'https://example.com',
    disable_resources=True  # 不加载图片、字体等
)

# 阻止特定域名
page = DynamicFetcher.fetch(
    'https://example.com',
    blocked_domains=['ads.com', 'analytics.com']
)

# 只加载必要的资源
page = DynamicFetcher.fetch(
    'https://example.com',
    allowed_resources=['document', 'script', 'stylesheet']
)
```

### 网络控制

```python
# 等待网络空闲
page = DynamicFetcher.fetch(
    'https://example.com',
    network_idle=True  # 等待所有网络请求完成
)

# 等待特定选择器
page = DynamicFetcher.fetch(
    'https://example.com',
    wait_selector='.content-loaded'
)

# 等待特定时间
page = DynamicFetcher.fetch(
    'https://example.com',
    wait_for_timeout=5000  # 等待 5 秒
)

# 组合使用
page = DynamicFetcher.fetch(
    'https://example.com',
    network_idle=True,
    wait_selector='.data-ready'
)
```

### 页面操作

```python
# 自定义页面操作函数
def scroll_and_wait(page):
    # 滚动到页面底部
    page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
    # 等待内容加载
    page.wait_for_selector('.more-content')
    # 再次滚动
    page.evaluate('window.scrollTo(0, document.body.scrollHeight)')

page = DynamicFetcher.fetch(
    'https://example.com',
    page_action=scroll_and_wait
)

# 内置的滚动功能
page = DynamicFetcher.fetch(
    'https://example.com',
    scroll_down=True,          # 启用滚动
    scroll_pause=2.0,          # 每次滚动后暂停 2 秒
    scroll_down_limit=10       # 最多滚动 10 次
)
```

### 浏览器配置

```python
# 设置用户代理
page = DynamicFetcher.fetch(
    'https://example.com',
    user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
)

# 设置视口大小
page = DynamicFetcher.fetch(
    'https://example.com',
    viewport={'width': 1920, 'height': 1080}
)

# 设置地理位置
page = DynamicFetcher.fetch(
    'https://example.com',
    geolocation={'latitude': 37.7749, 'longitude': -122.4194}
)

# 设置时区
page = DynamicFetcher.fetch(
    'https://example.com',
    timezone='America/New_York'
)

# 设置语言
page = DynamicFetcher.fetch(
    'https://example.com',
    locale='zh-CN'
)
```

## 高级功能

### 拦截和处理请求

```python
from scrapling.fetchers import DynamicFetcher

def handle_request(route):
    # 修改请求头
    headers = route.request.headers
    headers['X-Custom-Header'] = 'value'
    route.continue_(headers=headers)

# 获取页面对象并设置拦截
page = DynamicFetcher.fetch(
    'https://example.com',
    setup_page=lambda p: p.route('**/*', handle_request)
)
```

### 拦截响应

```python
def handle_response(route):
    # 获取响应
    response = route.response
    print(f"状态码: {response.status}")

    # 可以修改响应
    if response.status == 200:
        # 处理响应
        pass

page = DynamicFetcher.fetch(
    'https://example.com',
    setup_page=lambda p: p.route('**/*', handle_response)
)
```

### 执行 JavaScript

```python
# 执行简单的 JavaScript
page = DynamicFetcher.fetch('https://example.com')

result = page.evaluate('document.title')
print(f"页面标题: {result}")

# 执行复杂的 JavaScript
result = page.evaluate('''
    () => {
        return {
            title: document.title,
            url: window.location.href,
            scrollY: window.scrollY
        }
    }
''')
print(result)

# 等待 JavaScript 条件
page.wait_for_function('document.readyState === "complete"')
```

### 处理弹窗和对话框

```python
# 处理 alert
page = DynamicFetcher.fetch(
    'https://example.com',
    setup_page=lambda p: p.on('dialog', lambda dialog: dialog.accept())
)

# 处理 confirm
def handle_confirm(dialog):
    print(f"确认消息: {dialog.message}")
    dialog.accept()

page = DynamicFetcher.fetch(
    'https://example.com',
    setup_page=lambda p: p.on('dialog', handle_confirm)
)
```

## 会话管理

### DynamicSession

`DynamicSession` 用于复用浏览器实例和连接池。

```python
from scrapling.fetchers import DynamicSession

# 创建会话
with DynamicSession(
    headless=True,
    max_pages=5  # 最多 5 个并发标签页
) as session:
    # 并发抓取多个页面
    urls = [
        'https://example.com/page1',
        'https://example.com/page2',
        'https://example.com/page3'
    ]

    pages = [session.fetch(url) for url in urls]

    for page in pages:
        print(f"页面标题: {page.css('title::text').get()}")
```

### 会话特性

1. **浏览器复用**：避免重复启动浏览器
2. **Cookie 持久化**：跨页面共享 cookies
3. **连接池**：支持多标签并发
4. **资源优化**：共享加载的资源

### 高级会话配置

```python
from scrapling.fetchers import DynamicSession

session = DynamicSession(
    headless=True,
    real_chrome=False,
    max_pages=10,
    disable_resources=True,
    blocked_domains=['analytics.com'],
    timeout=60
)

with session:
    # 使用会话
    for url in urls:
        session.fetch(url)
```

## 实用示例

### 1. 单页应用（SPA）

```python
from scrapling.fetchers import DynamicFetcher

# 等待路由和组件加载
page = DynamicFetcher.fetch(
    'https://spa.example.com/dashboard',
    network_idle=True,
    wait_selector='.dashboard-loaded',
    wait_for_timeout=5000
)

# 提取动态内容
dashboard_data = {
    'user': page.css('.user-name::text').get(),
    'stats': page.css('.stats-value').getall()
}
```

### 2. 无限滚动

```python
from scrapling.fetchers import DynamicFetcher

# 自动滚动加载更多内容
page = DynamicFetcher.fetch(
    'https://infinite-scroll.example.com',
    scroll_down=True,
    scroll_pause=2.0,
    scroll_down_limit=5
)

# 提取所有项目
items = page.css('.item').getall()
print(f"抓取到 {len(items)} 个项目")
```

### 3. 表单交互

```python
from scrapling.fetchers import DynamicFetcher

# 填写并提交表单
page = DynamicFetcher.fetch(
    'https://example.com/login',
    wait_selector='#login-form'
)

# 获取表单元素
username_input = page.css('#username')[0]
password_input = page.css('#password')[0]
submit_button = page.css('button[type="submit"]')[0]

# 填写表单（需要直接使用 page 对象）
# 注意：这里需要访问底层的 Playwright page 对象
# 建议使用 page_action 参数或 setup_page
```

### 4. 点击和导航

```python
def interact_with_page(page):
    # 点击按钮
    page.click('button.load-more')
    # 等待内容加载
    page.wait_for_selector('.new-content')
    # 提取新内容
    content = page.query_selector('.new-content').text_content()
    print(f"新内容: {content}")

page = DynamicFetcher.fetch(
    'https://example.com',
    page_action=interact_with_page
)
```

### 5. 截图

```python
from scrapling.fetchers import DynamicFetcher

# 抓取并截图
page = DynamicFetcher.fetch('https://example.com')

# 保存截图
page.screenshot(path='screenshot.png')
page.screenshot(path='full-page.png', full_page=True)
```

## 性能优化

### 1. 禁用不必要资源

```python
page = DynamicFetcher.fetch(
    'https://example.com',
    disable_resources=True  # 只加载核心资源
)
```

### 2. 使用会话

```python
# 不推荐（每次都启动浏览器）
for url in urls:
    DynamicFetcher.fetch(url)

# 推荐（复用浏览器）
with DynamicSession() as session:
    for url in urls:
        session.fetch(url)
```

### 3. 合理的超时设置

```python
page = DynamicFetcher.fetch(
    'https://example.com',
    timeout=30,                # 页面加载超时
    network_idle_timeout=10,   # 网络空闲超时
    wait_for_timeout=5       # 等待元素超时
)
```

### 4. 限制并发

```python
with DynamicSession(max_pages=3) as session:  # 限制并发数
    # 并发请求
    tasks = [session.fetch(url) for url in urls]
    results = await asyncio.gather(*tasks)
```

## 错误处理

```python
from scrapling.fetchers import DynamicFetcher
from playwright.async_api import TimeoutError

try:
    page = DynamicFetcher.fetch(
        'https://example.com',
        timeout=30,
        wait_selector='.content'
    )

    # 检查页面状态
    if page.status:
        print("页面加载成功")
    else:
        print("页面加载失败")

except TimeoutError:
    print("页面加载超时")
except Exception as e:
    print(f"错误: {e}")
```

## 最佳实践

### 1. 等待策略

```python
# 组合多种等待方式
page = DynamicFetcher.fetch(
    'https://example.com',
    network_idle=True,      # 等待网络空闲
    wait_selector='.data',    # 等待元素出现
    wait_for_timeout=3000     # 最大等待时间
)
```

### 2. 资源过滤

```python
# 只加载必要的资源
page = DynamicFetcher.fetch(
    'https://example.com',
    allowed_resources=['document', 'script'],
    blocked_domains=['ads.com', 'tracker.com']
)
```

### 3. 错误重试

```python
def fetch_with_retry(url, max_retries=3):
    for attempt in range(max_retries):
        try:
            page = DynamicFetcher.fetch(url)
            return page
        except:
            if attempt == max_retries - 1:
                raise
            continue
    return None
```

## 与 StealthyFetcher 的对比

| 特性 | DynamicFetcher | StealthyFetcher |
|--------|---------------|-----------------|
| 基础功能 | 完整 | 完整 |
| Cloudflare 绕过 | ❌ | ✅ |
| WebRTC 防护 | ❌ | ✅ |
| Canvas 噪声 | ❌ | ✅ |
| 性能 | 中等 | 稍慢 |
| 适用场景 | 一般动态网站 | 受保护的动态网站 |

## 总结

DynamicFetcher 提供了强大的动态网站抓取能力：

1. **完整浏览器功能**：基于 Playwright，支持所有浏览器 API
2. **灵活的配置**：支持多种运行模式和高级设置
3. **会话管理**：`DynamicSession` 提供浏览器复用
4. **页面操作**：支持滚动、点击、JavaScript 执行等

对于需要 JavaScript 渲染的网站，DynamicFetcher 是理想的选择。如果网站有强反爬保护，请使用 `StealthyFetcher`。

---

# 强保护动态网站 (Dynamic Websites with Hard Protections)

## 概述

StealthyFetcher 类在 DynamicFetcher 的基础上增加了强大的反爬绕过能力，专门用于处理具有严格反爬保护的网站。它能够自动绕过 Cloudflare Turnstile/Interstitial 挑战，并防止多种指纹识别技术。

## 功能概述

StealthyFetcher 继承了 DynamicFetcher 的所有功能，并添加了以下高级特性：

1. **自动 Cloudflare 绕过**
2. **WebRTC 泄漏防护**
3. **Canvas 指纹噪声**
4. **Headless 检测修补**
5. **完整的浏览器 API**

## 基本用法

### 基本请求

```python
from scrapling.fetchers import StealthyFetcher

# 抓取受保护的网站
page = StealthyFetcher.fetch('https://protected-site.com')
```

### 启用 Cloudflare 绕过

```python
# 自动解决 Cloudflare 挑战
page = StealthyFetcher.fetch(
    'https://cloudflare-protected.com',
    solve_cloudflare=True
)
```

## 核心特性

### 1. 自动解决 Cloudflare 挑战

自动检测并绕过以下 Cloudflare 保护：

- **Turnstile**：新的验证码系统
- **Interstitial**：中间页面挑战
- **JS Challenge**：JavaScript 挑战

```python
page = StealthyFetcher.fetch(
    'https://cloudflare-protected.com',
    solve_cloudflare=True,  # 启用自动解决
    wait_selector='.main-content'  # 等待挑战通过
)
```

### 2. WebRTC 泄漏防护

防止通过 WebRTC 泄漏本地 IP 地址。

```python
# 阻止 WebRTC
page = StealthyFetcher.fetch(
    'https://protected-site.com',
    block_webrtc=True
)
```

**重要性**：WebRTC 可以泄露真实的 IP 地址，即使用户使用了代理。启用此功能可以防止这种泄漏。

### 3. Canvas 指纹噪声

为 Canvas 元素添加随机噪声，防止基于 Canvas 的指纹识别。

```python
# 隐藏 Canvas 指纹
page = StealthyFetcher.fetch(
    'https://protected-site.com',
    hide_canvas=True
)
```

**工作原理**：在绘制到 Canvas 时注入微小的随机变化，使每次渲染都略有不同。

### 4. WebGL 支持

控制 WebGL 的使用。

```python
# 禁用 WebGL
page = StealthyFetcher.fetch(
    'https://protected-site.com',
    allow_webgl=False
)

# 启用 WebGL（默认）
page = StealthyFetcher.fetch(
    'https://protected-site.com',
    allow_webgl=True
)
```

### 5. Headless 检测修补

修补常见的 headless 检测方法，使浏览器看起来像真实的用户浏览器。

```python
# 自动修补（默认启用）
page = StealthyFetcher.fetch(
    'https://protected-site.com'
)
```

## 完整参数列表

### Cloudflare 和隐身选项

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `solve_cloudflare` | `bool` | `False` | 自动解决 Cloudflare 挑战 |
| `block_webrtc` | `bool` | `False` | 阻止 WebRTC 泄漏 |
| `hide_canvas` | `bool` | `False` | 添加 Canvas 噪声 |
| `allow_webgl` | `bool` | `True` | 允许 WebGL |
| `google_search` | `bool` | `False` | 模拟 Google 搜索行为 |

### 浏览器配置

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `real_chrome` | `bool` | `False` | 使用真实 Chrome 浏览器 |
| `headless` | `bool` | `True` | 无头模式 |
| `user_agent` | `str` | 自动 | 自定义 User-Agent |
| `viewport` | `dict` | 自动 | 视口大小 |

### 性能参数

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `disable_resources` | `bool` | `False` | 禁用不必要资源 |
| `network_idle` | `bool` | `False` | 等待网络空闲 |
| `timeout` | `int` | `30` | 超时时间（秒） |

## 高级用法

### 1. 完整隐身配置

```python
from scrapling.fetchers import StealthyFetcher

# 最大化隐身能力
page = StealthyFetcher.fetch(
    'https://strictly-protected.com',
    solve_cloudflare=True,
    block_webrtc=True,
    hide_canvas=True,
    google_search=True,
    real_chrome=True,
    network_idle=True,
    wait_selector='.content'
)
```

### 2. 使用真实 Chrome

```python
# 使用本地 Chrome 浏览器
page = StealthyFetcher.fetch(
    'https://protected-site.com',
    real_chrome=True,
    solve_cloudflare=True
)
```

**优点**：
- 与真实用户完全一致
- 支持所有 Chrome 扩展
- 更好的网站兼容性

**缺点**：
- 需要 Chrome 已安装
- 可能会打开浏览器窗口

### 3. 自定义页面操作

```python
def stealthy_interaction(page):
    # 等待 Cloudflare 挑战通过
    page.wait_for_selector('.not-protected', timeout=10000)

    # 滚动页面以触发懒加载
    for _ in range(3):
        page.evaluate('window.scrollBy(0, 500)')
        page.wait_for_timeout(500)

    # 提取内容
    content = page.query_selector('.main-content').text_content()
    print(f"内容长度: {len(content)}")

page = StealthyFetcher.fetch(
    'https://protected-site.com',
    solve_cloudflare=True,
    page_action=stealthy_interaction
)
```

### 4. 拦截和修改请求

```python
def modify_headers(route):
    # 添加自定义头
    headers = route.request.headers
    headers['X-Custom-Header'] = 'value'
    route.continue_(headers=headers)

page = StealthyFetcher.fetch(
    'https://protected-site.com',
    solve_cloudflare=True,
    setup_page=lambda p: p.route('**/*', modify_headers)
)
```

### 5. 处理多次挑战

```python
def handle_challenges(page):
    # 可能需要多次等待
    try:
        # 等待第一次挑战
        page.wait_for_selector('.challenge-form', timeout=5000)
        print("检测到挑战，等待解决...")
        page.wait_for_selector('.success-message', timeout=15000)
        print("挑战已解决")
    except:
        # 没有挑战，直接继续
        pass

page = StealthyFetcher.fetch(
    'https://protected-site.com',
    solve_cloudflare=True,
    page_action=handle_challenges
)
```

## 会话管理

### StealthySession

`StealthySession` 用于复用隐身浏览器实例。

```python
from scrapling.fetchers import StealthySession

# 创建隐身会话
with StealthySession(
    solve_cloudflare=True,
    block_webrtc=True,
    max_pages=5
) as session:
    # 并发抓取多个页面
    urls = [
        'https://protected-site.com/page1',
        'https://protected-site.com/page2',
        'https://protected-site.com/page3'
    ]

    pages = [session.fetch(url) for url in urls]

    for page in pages:
        print(f"页面: {page.css('title::text').get()}")
```

### 会话优势

1. **Cookie 共享**：跨页面保持登录状态
2. **浏览器复用**：避免重复启动开销
3. **挑战缓存**：避免重复解决 Cloudflare 挑战
4. **资源优化**：共享加载的资源

## 实用示例

### 1. 绕过 Cloudflare Turnstile

```python
from scrapling.fetchers import StealthyFetcher

# 解决 Turnstile 验证
page = StealthyFetcher.fetch(
    'https://nopecha.com/demo/cloudflare',
    solve_cloudflare=True,
    real_chrome=True,
    block_webrtc=True
)

# 提取验证通过后的内容
content = page.css('.result-content::text').get()
print(f"内容: {content}")
```

### 2. 受保护的电商网站

```python
from scrapling.fetchers import StealthyFetcher

# 抓取受保护的产品页面
page = StealthyFetcher.fetch(
    'https://protected-shop.com/products',
    solve_cloudflare=True,
    hide_canvas=True,
    network_idle=True,
    wait_selector='.product-list'
)

# 提取产品信息
products = []
for product in page.css('.product-card'):
    products.append({
        'name': product.css('.title::text').get(),
        'price': product.css('.price::text').get(),
        'link': product.css('a::attr(href)').get()
    })

print(f"抓取到 {len(products)} 个产品")
```

### 3. 新闻网站绕过

```python
# 新闻网站通常有严格保护
page = StealthyFetcher.fetch(
    'https://protected-news.com/articles',
    solve_cloudflare=True,
    google_search=True,  # 模拟搜索行为
    real_chrome=True
)

articles = page.css('.article')
for article in articles:
    title = article.css('.headline::text').get()
    print(f"文章: {title}")
```

### 4. 社交媒体抓取

```python
# 社交媒体有最强的保护
page = StealthyFetcher.fetch(
    'https://protected-social.com/feed',
    solve_cloudflare=True,
    block_webrtc=True,
    hide_canvas=True,
    allow_webgl=False
)

posts = page.css('.post')
for post in posts:
    content = post.css('.post-content::text').get()
    print(f"帖子: {content[:50]}...")
```

### 5. API 端点保护

```python
# 受保护的 API 端点
page = StealthyFetcher.fetch(
    'https://api.example.com/data',
    solve_cloudflare=True,
    method='POST',
    headers={
        'Content-Type': 'application/json',
        'Authorization': 'Bearer token'
    },
    data={'query': 'example'}
)

response_data = page.json()
print(f"API 响应: {response_data}")
```

## 性能考虑

### 1. 按需启用功能

```python
# 只启用必要的隐身功能
page = StealthyFetcher.fetch(
    'https://protected-site.com',
    solve_cloudflare=True,  # 必需
    # 其他功能按需启用
)
```

### 2. 使用会话复用

```python
# 避免重复的 Cloudflare 挑战
with StealthySession(solve_cloudflare=True) as session:
    for url in urls:
        session.fetch(url)  # 后续请求不会重复挑战
```

### 3. 合理的超时设置

```python
page = StealthyFetcher.fetch(
    'https://protected-site.com',
    solve_cloudflare=True,
    timeout=60,  # 给予足够时间解决挑战
    network_idle=True
)
```

## 错误处理

### 处理 Cloudflare 失败

```python
from scrapling.fetchers import StealthyFetcher

try:
    page = StealthyFetcher.fetch(
        'https://protected-site.com',
        solve_cloudflare=True,
        timeout=30
    )

    # 检查是否真正解决
    if 'cf-challenge' in page.url.lower():
        print("Cloudflare 挑战未解决")
        # 可以尝试其他方法
    else:
        print("成功绕过保护")

except Exception as e:
    print(f"错误: {e}")
    # 可以尝试使用其他浏览器模式
```

### 降级策略

```python
def fetch_protected(url):
    # 首先尝试 StealthyFetcher
    try:
        page = StealthyFetcher.fetch(
            url,
            solve_cloudflare=True,
            timeout=30
        )
        return page
    except:
        # 降级到 DynamicFetcher
        try:
            page = DynamicFetcher.fetch(url, timeout=30)
            return page
        except:
            # 最后降级到 Fetcher
            return Fetcher.get(url)
```

## 最佳实践

### 1. 识别保护类型

```python
# 测试网站保护强度
test_page = Fetcher.get('https://example.com')

if 'cloudflare' in test_page.text.lower():
    print("检测到 Cloudflare，使用 StealthyFetcher")
    page = StealthyFetcher.fetch(url, solve_cloudflare=True)
else:
    print("无强保护，使用标准抓取器")
    page = Fetcher.get(url)
```

### 2. 渐进式启用

```python
# 逐步测试保护绕过
configs = [
    {'solve_cloudflare': True},
    {'solve_cloudflare': True, 'block_webrtc': True},
    {'solve_cloudflare': True, 'block_webrtc': True, 'hide_canvas': True}
]

for config in configs:
    try:
        page = StealthyFetcher.fetch(url, **config)
        print(f"成功使用配置: {config}")
        break
    except:
        continue
```

### 3. 监控和日志

```python
import logging

logging.basicConfig(level=logging.INFO)

page = StealthyFetcher.fetch(
    'https://protected-site.com',
    solve_cloudflare=True,
    # Playwright 会输出详细的调试信息
)
```

## 与其他抓取器的对比

| 特性 | Fetcher | DynamicFetcher | StealthyFetcher |
|--------|---------|---------------|-----------------|
| 基础抓取 | ✅ | ✅ | ✅ |
| JavaScript | ❌ | ✅ | ✅ |
| 浏览器自动化 | ❌ | ✅ | ✅ |
| Cloudflare 绕过 | ❌ | ❌ | ✅ |
| WebRTC 防护 | ❌ | ❌ | ✅ |
| Canvas 噪声 | ❌ | ❌ | ✅ |
| 性能 | 最快 | 中等 | 稍慢 |
| 内存占用 | 最低 | 中等 | 较高 |

## 总结

StealthyFetcher 是 Scrapling 最强大的抓取器，专门设计用于绕过严格的反爬保护：

1. **自动绕过 Cloudflare**：解决 Turnstile 和 Interstitial 挑战
2. **多层面防护**：WebRTC、Canvas、WebGL 等
3. **完整浏览器功能**：继承 DynamicFetcher 的所有能力
4. **会话支持**：`StealthySession` 提供高效的复用

对于受 Cloudflare 或其他反爬系统严格保护的网站，StealthyFetcher 是最佳选择。

---

# Spiders 架构 (Spiders Architecture)

## 概述

Scrapling 的 Spider 系统提供了一个强大而灵活的爬虫框架，支持异步处理、多会话管理、并发控制和断点续爬等功能。

## 数据流程

Spider 系统的数据流向如下：

```
┌─────────────┐
│   Spider   │
└──────┬──────┘
       │
       │ 1. 生成初始请求
       ▼
┌──────────────────────┐
│   Scheduler        │
│ (请求调度与去重） │
└──────┬──────────────┘
       │
       │ 2. 加入优先级队列
       ▼
┌──────────────────────┐
│ Crawler Engine    │
│ (并发控制）       │
└──────┬──────────────┘
       │
       │ 3. 调度请求
       ▼
┌──────────────────────┐
│ Session Manager  │
│ (会话管理）       │
└──────┬──────────────┘
       │
       │ 4. 执行请求
       ▼
┌──────────────────────┐
│  Session          │
│ (Fetcher/Dynamic/  │
│  Stealthy）      │
└──────┬──────────────┘
       │
       │ 5. 返回 Response
       ▼
┌──────────────────────┐
│   Callback        │
│ (响应处理）       │
└──────┬──────────────┘
       │
       │ 6. 生成新请求或数据
       ├──────────┬──────────┤
       ▼          ▼
┌─────────┐  ┌─────────┐
│ Requests │  │ Items   │
│(队列）   │  │(数据）   │
└─────────┘  └─────────┘
     │            │
     └────────────┘
          │
          ▼
┌──────────────────────┐
│   Output         │
│ (结果输出）       │
└──────────────────────┘
```

## 核心组件

### 1. Spider（爬虫）

Spider 是用户定义的类，包含爬取逻辑。

**主要职责**：
- 定义初始请求（`start_urls` 或 `start_requests()`）
- 实现回调函数（如 `parse()`）处理响应
- 配置爬虫行为（并发、延迟、会话等）

**示例**：
```python
from scrapling.spiders import Spider, Response

class MySpider(Spider):
    name = "my_spider"
    start_urls = ["https://example.com"]

    async def parse(self, response: Response):
        # 提取数据
        yield {'title': response.css('h1::text').get()}

        # 生成新请求
        yield response.follow('/next', callback=self.parse)
```

### 2. Crawler Engine（爬虫引擎）

负责控制爬虫的整体执行。

**功能**：
- 并发控制
- 请求调度
- 错误处理
- 状态管理

### 3. Scheduler（调度器）

管理请求队列和去重。

**功能**：
- 优先级队列
- 请求去重
- 请求过滤

**去重机制**：
基于 URL、HTTP 方法、请求体和会话 ID 生成指纹，自动过滤重复请求。

### 4. Session Manager（会话管理器）

管理不同类型的会话。

**支持的会话类型**：
- **FetcherSession**：快速 HTTP 会话
- **AsyncDynamicSession**：动态浏览器会话
- **AsyncStealthySession**：隐身浏览器会话

**功能**：
- 会话生命周期管理
- 请求路由到指定会话
- 会话复用和优化

### 5. Checkpoint System（检查点系统）

支持暂停和恢复功能。

**功能**：
- 定期保存爬取状态
- 支持 Ctrl+C 暂停
- 断点续爬

**保存内容**：
- 待处理请求队列
- 已处理请求指纹集合
- 当前爬取状态

### 6. Output（输出）

收集和返回爬取结果。

**返回内容**：
- `CrawlResult` 对象
- 包含所有爬取的数据项
- 包含详细的统计信息

## 与 Scrapy 的对比

| 概念 | Scrapy | Scrapling |
|--------|---------|-----------|
| 并发控制 | `CONCURRENT_REQUESTS` | `concurrent_requests` 属性 |
| 每域名并发 | `CONCURRENT_REQUESTS_PER_DOMAIN` | `per_domain_concurrent_requests` 属性 |
| 下载延迟 | `DOWNLOAD_DELAY` | `download_delay` 属性 |
| 暂停/恢复 | `JOBDIR` | `crawldir` 参数 |
| 多会话 | 需要中间件 | 内置多会话支持 |
| 阻塞检测 | 自定义中间件 | 内置 `is_blocked()` 钩子 |
| 请求去重 | 基于 URL | 基于 URL + 方法 + body + 会话 ID |
| 异步支持 | 部分支持 | 完全异步 |
| 会话类型 | 单一会话 | 多种会话类型 |
| 代理轮换 | 中间件 | 内置 `ProxyRotator` |
| 检查点 | 基于目录 | 基于 `crawldir` 参数 |

## Spider 类属性

### 基本属性

```python
class MySpider(Spider):
    # 爬虫名称（必填）
    name = "my_spider"

    # 起始 URL 列表
    start_urls = [
        "https://example.com/page1",
        "https://example.com/page2"
    ]

    # 允许的域名（用于过滤）
    allowed_domains = ["example.com"]

    # 全局并发请求数
    concurrent_requests = 5

    # 每域名并发请求数
    per_domain_concurrent_requests = 2

    # 下载延迟（秒）
    download_delay = 1.0

    # 爬取目录（用于暂停/恢复）
    crawldir = "./crawl_data"

    # 最大阻塞重试次数
    max_blocked_retries = 3
```

### 指纹配置

```python
class MySpider(Spider):
    # 包含额外参数在指纹中
    fp_include_kwargs = False

    # 保留 URL 片段
    fp_keep_fragments = False

    # 包含请求头在指纹中
    fp_include_headers = False
```

## 生命周期

### 1. 初始化

```python
spider = MySpider()
```

### 2. 启动

```python
result = spider.start()
# 或
spider.start(use_uvloop=True)
```

### 3. 运行中

```
start_requests() -> 调度 -> 抓取 -> parse() -> 生成新请求
    ↑                                              ↓
    └──────────────────────────────────────────────────────┘
```

### 4. 完成/暂停

```python
# 正常完成
result = spider.start()
if result.completed:
    print("爬取完成")
elif result.paused:
    print("爬取已暂停")
```

## 数据收集

### Yield Items

```python
async def parse(self, response: Response):
    # 生成数据项
    yield {
        'title': response.css('h1::text').get(),
        'url': response.url,
        'timestamp': time.time()
    }
```

### Yield Requests

```python
async def parse(self, response: Response):
    # 生成跟进请求
    yield response.follow('/next', callback=self.parse)

    # 生成多个请求
    for link in response.css('a::attr(href)').getall():
        yield response.follow(link)
```

### 混合 Yield

```python
async def parse(self, response: Response):
    # 同时生成数据和请求
    yield {'data': 'extracted'}
    yield response.follow('/next')
```

## 错误处理

### 请求失败

```python
async def on_error(self, request, error: Exception):
    self.logger.error(f"请求失败: {request.url} - {error}")
```

### 阻塞处理

```python
async def is_blocked(self, response: Response) -> bool:
    # 自定义阻塞检测
    if response.status in [403, 429, 503]:
        return True
    if 'access denied' in response.text.lower():
        return True
    return False

async def retry_blocked_request(self, request: Request, response: Response) -> Request:
    # 修改被阻塞的请求
    request.sid = "stealth"
    return request
```

## 性能优化

### 并发控制

```python
class OptimizedSpider(Spider):
    # 全局并发
    concurrent_requests = 10

    # 每域名并发
    per_domain_concurrent_requests = 3

    # 下载延迟
    download_delay = 0.5
```

### 使用 uvloop

```python
# 使用更快的事件循环
result = spider.start(use_uvloop=True)
```

### 流式处理

```python
import asyncio

async def main():
    spider = MySpider()

    # 流式获取结果
    async for item in spider.stream():
        # 实时处理每个项目
        process_item(item)
        # 访问实时统计
        print(f"已抓取: {spider.stats.items_scraped}")

asyncio.run(main())
```

## 监控和统计

### 访问统计

```python
result = spider.start()

# 基本统计
print(f"完成: {result.completed}")
print(f"暂停: {result.paused}")

# 详细统计
stats = result.stats
print(f"请求数: {stats.requests_count}")
print(f"成功数: {stats.successful_requests_count}")
print(f"失败数: {stats.failed_requests_count}")
print(f"阻塞数: {stats.blocked_requests_count}")
print(f"离站数: {stats.offsite_requests_count}")
print(f"抓取项: {stats.items_scraped}")
print(f"丢弃项: {stats.items_dropped}")
print(f"响应字节: {stats.response_bytes}")
print(f"耗时: {stats.elapsed_seconds:.1f}秒")
print(f"速度: {stats.requests_per_second:.1f} 请求/秒")
```

### 详细统计

```python
stats = result.stats

# 状态码分布
print(stats.response_status_count)
# {'status_200': 150, 'status_404': 3, 'status_403': 1}

# 每域名字节
print(stats.domains_response_bytes)
# {'example.com': 1234567, 'api.example.com': 45678}

# 每会话请求数
print(stats.sessions_requests_count)
# {'http': 120, 'stealth': 34}

# 使用的代理
print(stats.proxies)
# ['http://proxy1:8080', 'http://proxy2:8080']

# 日志级别计数
print(stats.log_levels_counter)
# {'debug': 200, 'info': 50, 'warning': 3, 'error': 1}

# 时间信息
print(stats.start_time)
print(stats.end_time)
print(stats.download_delay)

# 并发设置
print(stats.concurrent_requests)
print(stats.concurrent_requests_per_domain)

# 自定义统计
print(stats.custom_stats)
```

## 最佳实践

### 1. 合理设置并发

```python
# 对于友好爬取
class PoliteSpider(Spider):
    concurrent_requests = 5
    per_domain_concurrent_requests = 2
    download_delay = 1.0

# 对于快速爬取
class FastSpider(Spider):
    concurrent_requests = 50
    per_domain_concurrent_requests = 10
    download_delay = 0.1
```

### 2. 使用检查点

```python
# 启用暂停/恢复
spider = MySpider(crawldir="./crawl_state")

# Ctrl+C 暂停
result = spider.start()

# 恢复爬取
result = spider.start()
```

### 3. 错误处理

```python
class RobustSpider(Spider):
    async def on_error(self, request, error):
        # 记录错误
        self.logger.error(f"错误: {request.url} - {error}")

        # 可以决定重试或放弃
        if isinstance(error, TimeoutError):
            self.logger.warning("超时，将重试")
        else:
            self.logger.error("严重错误，放弃请求")
```

### 4. 监控和日志

```python
import logging

class MonitoredSpider(Spider):
    logging_level = logging.INFO
    log_file = "spider.log"

    async def on_start(self, resuming: bool = False):
        if resuming:
            self.logger.info("恢复爬取")
        else:
            self.logger.info("开始新爬取")

    async def on_close(self):
        self.logger.info("爬虫关闭")
        self.logger.info(f"统计: {self.stats.to_dict()}")
```

## 总结

Scrapling 的 Spider 架构提供了强大而灵活的爬虫框架：

1. **模块化设计**：清晰的组件分工
2. **异步支持**：完全异步的并发处理
3. **多会话支持**：内置多种会话类型管理
4. **断点续爬**：检查点系统支持暂停和恢复
5. **丰富的统计**：详细的爬取信息和性能指标

相比于 Scrapy，Scrapling 提供了更现代的异步架构、更灵活的会话管理和内置的反爬绕过能力，是构建大规模爬虫项目的优秀选择。


# Spider 入门指南 (Getting Started)

## 概述

本指南将帮助您快速上手 Scrapling 的 Spider 系统，学习如何创建第一个爬虫、运行爬虫、跟进链接、导出数据和过滤域名。

## 第一个 Spider

### 基础 Spider 示例

让我们从一个简单的爬虫开始，抓取名人名言网站：

```python
from scrapling.spiders import Spider, Response

class QuotesSpider(Spider):
    """抓取名人名言的简单爬虫"""

    name = "quotes"
    start_urls = ["https://quotes.toscrape.com"]

    async def parse(self, response: Response):
        """解析响应并提取数据"""
        # 查找所有名言
        for quote in response.css("div.quote"):
            # 提取数据并 yield
            yield {
                "text": quote.css("span.text::text").get(""),
                "author": quote.css("small.author::text").get(""),
                "tags": quote.css("div.tags a::text").getall()
            }

        # 跟进下一页链接
        next_page = response.css("li.next a::attr(href)").get()
        if next_page:
            yield response.follow(next_page, callback=self.parse)
```

### 运行 Spider

```python
# 创建爬虫实例并运行
spider = QuotesSpider()
result = spider.start()

# 查看结果
print(f"完成: {result.completed}")
print(f"抓取项数: {result.stats.items_scraped}")
```

## Spider 基本结构

### 基本属性

```python
class MySpider(Spider):
    # 爬虫名称（必填，唯一标识）
    name = "my_spider"

    # 起始 URL 列表
    start_urls = ["https://example.com/page1"]

    # 允许的域名（可选，用于过滤外部链接）
    allowed_domains = ["example.com"]
```

### 使用 start_requests()

除了 `start_urls`，你也可以使用 `start_requests()` 方法生成初始请求：

```python
class MySpider(Spider):
    name = "my_spider"

    def start_requests(self):
        """生成初始请求"""
        urls = [
            "https://example.com/page1",
            "https://example.com/page2"
        ]

        for url in urls:
            # 生成请求并指定回调函数
            yield Request(url, callback=self.parse)

    async def parse(self, response: Response):
        # 处理响应
        yield {"title": response.css("h1::text").get()}
```

## 数据提取

### 提取文本

```python
async def parse(self, response: Response):
    # 提取单个文本
    title = response.css("h1::text").get()
    yield {"title": title}

    # 提取多个文本
    paragraphs = response.css("p::text").getall()
    yield {"paragraphs": paragraphs}
```

### 提取属性

```python
async def parse(self, response: Response):
    # 提取链接
    for link in response.css("a"):
        yield {
            "text": link.css("::text").get(),
            "href": link.css("::attr(href)").get()
        }

    # 提取图片
    for img in response.css("img"):
        yield {
            "src": img.css("::attr(src)").get(),
            "alt": img.css("::attr(alt)").get()
        }
```

### 提取复杂结构

```python
async def parse(self, response: Response):
    products = []

    for product in response.css("div.product"):
        product_data = {
            "name": product.css("h2::text").get(),
            "price": product.css(".price::text").get(),
            "description": product.css(".desc::text").get(),
            "features": product.css(".features li::text").getall(),
            "available": "in-stock" in product.get()
        }

        products.append(product_data)

    yield {"products": products}
```

## 链接跟进

### 使用 CSS 选择器

```python
async def parse(self, response: Response):
    # 查找所有链接并跟进
    for link in response.css("a"):
        href = link.css("::attr(href)").get()
        if href:
            yield response.follow(href, callback=self.parse_detail)

    async def parse_detail(self, response: Response):
        yield {"url": response.url, "title": response.css("h1::text").get()}
```

### 使用 XPath

```python
async def parse(self, response: Response):
    # 使用 XPath 跟进链接
    for href in response.xpath("//a/@href").getall():
        yield response.follow(href, callback=self.parse)
```

### 相对 URL 自动处理

`response.follow()` 会自动处理相对 URL：

```python
# 这些都会被正确处理
response.follow("/page2")           # 相对路径
response.follow("//other.com/page")  # 协议相对
response.follow("http://full.url")   # 完整 URL
```

## 导出数据

### 保存为 JSON

```python
spider = QuotesSpider()
result = spider.start()

# 保存为 JSON 文件
result.items.to_json("quotes.json")
print("已保存到 quotes.json")
```

### 保存为 JSONL

```python
# 保存为 JSONL（每行一个 JSON）
result.items.to_jsonl("quotes.jsonl")
print("已保存到 quotes.jsonl")
```

### 访问所有项目

```python
# 获取所有项目列表
all_items = result.items.all()

# 遍历项目
for item in all_items:
    print(f"作者: {item['author']}")
    print(f"名言: {item['text']}")
    print("-" * 50)
```

### 获取第一个项目

```python
# 获取第一个项目
first_item = result.items.get()

if first_item:
    print(f"第一个项目: {first_item}")
```

## 域名过滤

### allowed_domains

使用 `allowed_domains` 过滤外部链接：

```python
class FilteredSpider(Spider):
    name = "filtered"
    start_urls = ["https://example.com"]

    # 只允许 example.com 及其子域名
    allowed_domains = ["example.com"]

    async def parse(self, response: Response):
        # 只跟进 example.com 域名下的链接
        for link in response.css("a"):
            yield response.follow(link, callback=self.parse)
```

### 外部链接处理

```python
class ExternalLinksSpider(Spider):
    name = "external"
    start_urls = ["https://example.com"]

    allowed_domains = ["example.com"]

    async def parse(self, response: Response):
        # 跟进内部链接
        for link in response.css("a.internal"):
            yield response.follow(link, callback=self.parse)

        # 收集但不跟进外部链接
        for link in response.css("a.external"):
            yield {
                "internal": False,
                "external_url": link.css("::attr(href)").get()
            }
```

### 允许多个域名

```python
class MultiDomainSpider(Spider):
    name = "multi_domain"
    start_urls = ["https://example.com"]

    # 允许多个域名
    allowed_domains = ["example.com", "sub.example.com", "partner.com"]

    async def parse(self, response: Response):
        # 只跟进允许的域名
        for link in response.css("a"):
            yield response.follow(link, callback=self.parse)
```

## 完整示例

### 完整的名言爬虫

```python
from scrapling.spiders import Spider, Response

class CompleteQuotesSpider(Spider):
    """完整的名人名言爬虫"""

    name = "complete_quotes"
    start_urls = ["https://quotes.toscrape.com"]
    allowed_domains = ["quotes.toscrape.com"]

    async def parse(self, response: Response):
        """解析每一页"""
        # 提取所有名言
        for quote in response.css("div.quote"):
            yield {
                "text": quote.css("span.text::text").get(""),
                "author": quote.css("small.author::text").get(""),
                "tags": quote.css("div.tags a::text").getall(),
                "url": response.url
            }

        # 跟进下一页
        next_page = response.css("li.next a::attr(href)").get()
        if next_page:
            self.logger.info(f"跟进下一页: {next_page}")
            yield response.follow(next_page, callback=self.parse)
        else:
            self.logger.info("没有更多页面")

# 运行爬虫
if __name__ == "__main__":
    spider = CompleteQuotesSpider()
    result = spider.start()

    # 打印统计信息
    print(f"\n{'='*50}")
    print(f"爬虫统计")
    print(f"{'='*50}")
    print(f"完成: {result.completed}")
    print(f"请求数: {result.stats.requests_count}")
    print(f"成功数: {result.stats.successful_requests_count}")
    print(f"失败数: {result.stats.failed_requests_count}")
    print(f"抓取项: {result.stats.items_scraped}")
    print(f"耗时: {result.stats.elapsed_seconds:.2f}秒")
    print(f"速度: {result.stats.requests_per_second:.2f} 请求/秒")
    print(f"{'='*50}\n")

    # 保存结果
    result.items.to_json("complete_quotes.json")
    print("已保存到 complete_quotes.json")

    # 显示前5个项目
    print("\n前5条名言:")
    for i, item in enumerate(result.items.all()[:5], 1):
        print(f"\n{i}. {item['text']}")
        print(f"   作者: {item['author']}")
        print(f"   标签: {', '.join(item['tags'])}")
```

### 商品爬虫示例

```python
from scrapling.spiders import Spider, Response

class ProductSpider(Spider):
    """商品爬虫示例"""

    name = "products"
    start_urls = ["https://books.toscrape.com"]
    allowed_domains = ["books.toscrape.com"]

    async def parse(self, response: Response):
        """解析列表页"""
        # 遍历所有商品
        for book in response.css("article.product_pod"):
            yield {
                "title": book.css("h3 a::attr(title)").get(),
                "price": book.css(".price_color::text").get(),
                "stock": book.css(".availability::text").get().strip(),
                "rating": book.css("p.star-rating::attr(class)").get().replace("star-rating ", ""),
                "product_url": response.urljoin(book.css("h3 a::attr(href)").get())
            }

        # 跟进下一页
        next_page = response.css("li.next a::attr(href)").get()
        if next_page:
            yield response.follow(next_page, callback=self.parse)
```

## CrawlResult 对象

### 基本属性

```python
result = spider.start()

# 检查状态
print(f"完成: {result.completed}")
print(f"暂停: {result.paused}")

# 访问项目
items = result.items.all()  # 所有项目
first_item = result.items.get()  # 第一个项目

# 访问统计
stats = result.stats
```

### 项目集合

`CrawlResult.items` 是一个 `ItemList` 对象，提供多种访问方式：

```python
# 获取所有项目
all_items = result.items.all()

# 获取第一个项目
first = result.items.get()

# 转换为列表
items_list = list(result.items)

# 检查是否为空
if result.items:
    print("有数据")

# 计数
count = len(result.items)
print(f"共 {count} 个项目")
```

### 统计信息

```python
stats = result.stats

# 基本统计
print(f"请求数: {stats.requests_count}")
print(f"成功数: {stats.successful_requests_count}")
print(f"失败数: {stats.failed_requests_count}")
print(f"阻塞数: {stats.blocked_requests_count}")
print(f"离站数: {stats.offsite_requests_count}")

# 数据统计
print(f"抓取项: {stats.items_scraped}")
print(f"丢弃项: {stats.items_dropped}")

# 性能统计
print(f"响应字节: {stats.response_bytes:,}")
print(f"耗时: {stats.elapsed_seconds:.1f}秒")
print(f"速度: {stats.requests_per_second:.1f} 请求/秒")
```

## 下一步学习

掌握了基础知识后，您可以继续学习以下高级主题：

### 1. 请求与响应

了解如何创建自定义请求、使用请求参数、处理响应等：
- [Requests & Responses](#请求与响应-requests--responses)

### 2. 会话管理

学习如何配置多种会话类型、代理轮换、Cookie 管理等：
- [Sessions](#会话管理-sessions)

### 3. 代理管理

了解如何配置代理池、代理轮换和阻塞处理：
- [Proxy management & Blocking](#代理管理与阻塞处理-proxy-management--blocking)

### 4. 高级特性

探索并发控制、暂停/恢复、生命周期钩子等功能：
- [Advanced features](#高级特性-advanced-features)

## 实用技巧

### 1. 使用日志

```python
import logging

class LoggedSpider(Spider):
    name = "logged"
    start_urls = ["https://example.com"]

    logging_level = logging.INFO

    async def parse(self, response: Response):
        self.logger.info(f"解析页面: {response.url}")
        self.logger.debug(f"页面标题: {response.css('title::text').get()}")
        self.logger.warning(f"警告示例")
        self.logger.error(f"错误示例")

        yield {"url": response.url}
```

### 2. 错误处理

```python
class RobustSpider(Spider):
    name = "robust"
    start_urls = ["https://example.com"]

    async def on_error(self, request, error: Exception):
        self.logger.error(f"请求失败: {request.url}")
        self.logger.error(f"错误类型: {type(error).__name__}")
        self.logger.error(f"错误信息: {error}")
```

### 3. 并发控制

```python
class TunedSpider(Spider):
    name = "tuned"
    start_urls = ["https://example.com"]

    # 全局并发数
    concurrent_requests = 10

    # 每域名并发数
    per_domain_concurrent_requests = 3

    # 下载延迟
    download_delay = 0.5

    async def parse(self, response: Response):
        yield {"url": response.url}
```

### 4. 暂停/恢复

```python
class ResumableSpider(Spider):
    name = "resumable"
    start_urls = ["https://example.com"]

    # 启用检查点
    crawldir = "./crawl_state"

    async def parse(self, response: Response):
        yield {"url": response.url}

# 运行爬虫（支持 Ctrl+C 暂停）
spider = ResumableSpider()
result = spider.start()

# 恢复运行
result = spider.start()
```

## 总结

本指南介绍了 Scrapling Spider 的基础知识：

1. **创建 Spider**：定义 `Spider` 类和 `parse()` 方法
2. **提取数据**：使用 CSS 选择器和 XPath 提取文本和属性
3. **跟进链接**：使用 `response.follow()` 自动处理相对 URL
4. **导出数据**：使用 `CrawlResult` 保存和访问结果
5. **域名过滤**：使用 `allowed_domains` 控制抓取范围
6. **统计信息**：通过 `CrawlResult.stats` 查看爬取详情

下一步，您可以深入学习请求管理、会话配置、代理轮换等高级功能。

---

# 请求与响应 (Requests & Responses)

## 概述

在 Scrapling Spider 系统中，Request 和 Response 是两个核心对象。Request 代表要发送的 HTTP 请求，Response 代表从服务器返回的响应。理解这两个对象是构建高效爬虫的关键。

## Request 对象

### 基本概念

Request 对象封装了 HTTP 请求的所有信息，包括 URL、HTTP 方法、请求头、请求体等。

### 创建 Request

```python
from scrapling.spiders import Request

# 最基本的请求
request = Request("https://example.com")

# 带回调函数的请求
request = Request("https://example.com", callback=self.parse)

# 带方法、请求头和数据的请求
request = Request(
    url="https://example.com/api",
    method="POST",
    headers={"Content-Type": "application/json"},
    data={"key": "value"},
    callback=self.parse_api
)
```

### Request 属性

```python
request = Request("https://example.com", callback=self.parse)

# 基本属性
print(request.url)        # "https://example.com"
print(request.method)     # "GET"
print(request.headers)    # 请求头字典
print(request.cookies)    # Cookies

# 优先级（默认为 0，数字越大优先级越高）
print(request.priority)   # 0

# 会话 ID（指定使用哪个会话）
print(request.sid)        # "default"

# 元数据（自定义数据，可以在回调中访问）
print(request.meta)       # {}

# 回调函数
print(request.callback)   # <function parse>
```

### Request 参数详解

#### url（必填）

请求的目标 URL，支持完整 URL 和相对 URL。

```python
# 完整 URL
Request("https://example.com/page")

# 相对 URL（需要 base_url）
Request("/page", base_url="https://example.com")
```

#### callback

响应处理回调函数，接收 Response 对象作为参数。

```python
def parse(self, response: Response):
    yield {"title": response.css("h1::text").get()}

def parse_detail(self, response: Response):
    yield {"detail": response.css("div.detail::text").get()}

# 使用不同的回调
Request("https://example.com", callback=self.parse)
Request("https://example.com/detail", callback=self.parse_detail)
```

#### method

HTTP 请求方法，默认为 "GET"。

```python
# GET 请求
Request("https://example.com", method="GET")

# POST 请求
Request("https://example.com/api", method="POST", data={"key": "value"})

# PUT 请求
Request("https://example.com/update", method="PUT", data={"id": 1})

# DELETE 请求
Request("https://example.com/delete/1", method="DELETE")
```

#### headers

自定义请求头。

```python
Request(
    "https://example.com",
    headers={
        "User-Agent": "Mozilla/5.0",
        "Accept": "text/html",
        "Authorization": "Bearer token123"
    }
)
```

#### data

请求体数据，用于 POST、PUT 等方法。

```python
# 表单数据
Request(
    "https://example.com/submit",
    method="POST",
    data={"username": "user", "password": "pass"}
)

# JSON 数据
import json
Request(
    "https://example.com/api",
    method="POST",
    data=json.dumps({"key": "value"}),
    headers={"Content-Type": "application/json"}
)

# 文件上传
Request(
    "https://example.com/upload",
    method="POST",
    data={"file": open("data.txt", "rb")},
    files={"upload": open("data.txt", "rb")}
)
```

#### params

URL 查询参数（自动添加到 URL）。

```python
Request(
    "https://example.com/search",
    params={"q": "python", "page": 1, "sort": "relevance"}
)

# 相当于：https://example.com/search?q=python&page=1&sort=relevance
```

#### cookies

请求 cookies。

```python
Request(
    "https://example.com",
    cookies={"session": "abc123", "user": "john"}
)
```

#### proxy

代理设置。

```python
Request(
    "https://example.com",
    proxy="http://proxy.example.com:8080"
)
```

#### priority

请求优先级，默认为 0。数字越大，优先级越高。

```python
# 高优先级请求
Request("https://example.com/important", priority=100, callback=self.parse)

# 中等优先级
Request("https://example.com/normal", priority=50, callback=self.parse)

# 低优先级
Request("https://example.com/low", priority=0, callback=self.parse)
```

#### sid（Session ID）

指定使用哪个会话执行此请求。

```python
class MySpider(Spider):
    sessions = {
        "default": {"type": "fetcher"},
        "browser": {"type": "dynamic"},
        "stealth": {"type": "stealthy"}
    }

    def start_requests(self):
        # 使用默认会话
        yield Request("https://example.com", callback=self.parse)

        # 使用浏览器会话
        yield Request("https://example.com/dynamic",
                    callback=self.parse_dynamic,
                    sid="browser")

        # 使用隐身会话
        yield Request("https://example.com/protected",
                    callback=self.parse_protected,
                    sid="stealth")
```

#### meta

元数据字典，可以在请求中传递自定义数据，回调函数中可以通过 `response.meta` 访问。

```python
def start_requests(self):
    # 在请求中附加元数据
    yield Request(
        "https://example.com/page1",
        callback=self.parse,
        meta={
            "page_number": 1,
            "category": "technology",
            "depth": 0
        }
    )

    async def parse(self, response: Response):
        # 访问元数据
        page_num = response.meta.get("page_number")
        category = response.meta.get("category")
        depth = response.meta.get("depth")

        yield {
            "url": response.url,
            "page": page_num,
            "category": category,
            "depth": depth
        }

        # 继续传递元数据
        next_page = response.css("a.next::attr(href)").get()
        if next_page:
            yield response.follow(
                next_page,
                callback=self.parse,
                meta={**response.meta, "depth": depth + 1}
            )
```

#### dont_filter

是否跳过去重检查，默认为 False。

```python
# 不检查重复（允许相同 URL 多次请求）
Request("https://example.com", dont_filter=True, callback=self.parse)
```

#### errback

错误处理回调函数，当请求失败时调用。

```python
async def parse(self, response: Response):
    yield {"url": response.url}

async def handle_error(self, failure: Request, error: Exception):
    self.logger.error(f"请求失败: {failure.url}")
    self.logger.error(f"错误: {error}")

    # 可以重试或记录
    yield Request(failure.url, callback=self.parse)

def start_requests(self):
    yield Request(
        "https://example.com",
        callback=self.parse,
        errback=self.handle_error
    )
```

## Response 对象

### 基本概念

Response 对象代表服务器的响应，继承自 `Selector` 类，因此可以使用所有选择器方法。同时提供了访问响应元数据的能力。

### Response 属性

```python
async def parse(self, response: Response):
    # 基本属性
    print(response.url)        # 当前 URL（可能经过重定向）
    print(response.status)     # HTTP 状态码
    print(response.headers)    # 响应头
    print(response.cookies)    # Cookies
    print(response.body)       # 原始响应体（bytes）
    print(response.text)       # 解码后的文本

    # 请求信息
    print(response.request)    # 关联的 Request 对象
    print(response.request.url)  # 原始请求 URL
    print(response.request.method)  # HTTP 方法

    # 元数据
    print(response.meta)       # 从 Request 传递的元数据

    # Selector 继承的属性
    print(response.css("h1::text").get())  # CSS 选择器
    print(response.xpath("//h1/text()").get())  # XPath 选择器
```

### Response 方法

#### 选择器方法（继承自 Selector）

```python
async def parse(self, response: Response):
    # CSS 选择器
    title = response.css("h1::text").get()
    links = response.css("a::attr(href)").getall()

    # XPath 选择器
    headings = response.xpath("//h2/text()").getall()

    # 文本选择
    paragraphs = response.css("p::text").getall()

    # 属性选择
    images = response.css("img::attr(src)").getall()

    # 相似元素查找
    main_element = response.css(".main").get()
    similar = main_element.find_similar()
```

#### follow()

跟进链接，自动处理相对 URL。

```python
async def parse(self, response: Response):
    # 跟进单个链接
    next_page = response.css("a.next::attr(href)").get()
    if next_page:
        yield response.follow(next_page, callback=self.parse)

    # 跟进多个链接
    for link in response.css("a"):
        href = link.css("::attr(href)").get()
        if href:
            yield response.follow(href, callback=self.parse_detail)

    # 传递元数据
    for product_link in response.css("a.product"):
        yield response.follow(
            product_link,
            callback=self.parse_product,
            meta={"category": response.css("h1::text").get()}
        )
```

#### urljoin()

拼接相对 URL。

```python
async def parse(self, response: Response):
    # 拼接相对 URL
    absolute_url = response.urljoin("/relative/path")

    # 拼接完整 URL
    full_url = response.urljoin("https://other.com/page")

    print(f"基础 URL: {response.url}")
    print(f"拼接结果: {absolute_url}")
```

#### json()

解析 JSON 响应。

```python
async def parse_api(self, response: Response):
    # 解析 JSON 响应
    data = response.json()

    # 访问数据
    yield {
        "status": data.get("status"),
        "results": data.get("results", [])
    }
```

## 请求去重

### 去重机制

Scrapling 基于以下信息生成请求指纹：

1. URL
2. HTTP 方法（GET、POST 等）
3. 请求体（body）
4. 会话 ID（sid）

这些信息组合生成唯一指纹，重复的请求会被自动过滤。

### 控制去重行为

```python
# 允许重复请求
Request("https://example.com", dont_filter=True, callback=self.parse)

# 默认行为（过滤重复）
Request("https://example.com", dont_filter=False, callback=self.parse)
```

### 去重配置

在 Spider 类中配置指纹生成选项：

```python
class MySpider(Spider):
    name = "my_spider"

    # 包含 URL 参数在指纹中
    fp_include_kwargs = False

    # 保留 URL 片段在指纹中
    fp_keep_fragments = False

    # 包含请求头在指纹中
    fp_include_headers = False
```

## 实用示例

### 1. 基本爬取

```python
from scrapling.spiders import Spider, Response, Request

class BasicSpider(Spider):
    name = "basic"
    start_urls = ["https://example.com"]

    def start_requests(self):
        # 使用 start_requests 可以更灵活地创建请求
        for url in self.start_urls:
            yield Request(url, callback=self.parse)

    async def parse(self, response: Response):
        # 提取数据
        yield {
            "url": response.url,
            "title": response.css("title::text").get(),
            "status": response.status
        }
```

### 2. 多种请求方法

```python
class MethodsSpider(Spider):
    name = "methods"

    def start_requests(self):
        # GET 请求
        yield Request("https://api.example.com/data",
                    callback=self.parse_get)

        # POST 请求
        yield Request("https://api.example.com/create",
                    method="POST",
                    data={"name": "item", "value": 100},
                    callback=self.parse_post)

        # PUT 请求
        yield Request("https://api.example.com/update/1",
                    method="PUT",
                    data={"value": 200},
                    callback=self.parse_put)

        # DELETE 请求
        yield Request("https://api.example.com/delete/1",
                    method="DELETE",
                    callback=self.parse_delete)

    async def parse_get(self, response: Response):
        yield {"method": "GET", "data": response.json()}

    async def parse_post(self, response: Response):
        yield {"method": "POST", "status": response.status}

    async def parse_put(self, response: Response):
        yield {"method": "PUT", "status": response.status}

    async def parse_delete(self, response: Response):
        yield {"method": "DELETE", "status": response.status}
```

### 3. 元数据传递

```python
class MetadataSpider(Spider):
    name = "metadata"

    def start_requests(self):
        categories = ["technology", "science", "business"]

        for category in categories:
            yield Request(
                f"https://example.com/{category}",
                callback=self.parse,
                meta={
                    "category": category,
                    "depth": 0,
                    "start_time": time.time()
                }
            )

    async def parse(self, response: Response):
        # 访问元数据
        category = response.meta["category"]
        depth = response.meta["depth"]
        start_time = response.meta["start_time"]

        yield {
            "category": category,
            "depth": depth,
            "elapsed": time.time() - start_time,
            "url": response.url
        }

        # 继续传递元数据
        for link in response.css("a"):
            yield response.follow(
                link,
                callback=self.parse,
                meta={**response.meta, "depth": depth + 1}
            )
```

### 4. 错误处理

```python
class ErrorHandlingSpider(Spider):
    name = "error_handling"

    def start_requests(self):
        urls = [
            "https://example.com/page1",
            "https://example.com/page2",
            "https://invalid-url.example.com"
        ]

        for url in urls:
            yield Request(
                url,
                callback=self.parse,
                errback=self.handle_error
            )

    async def parse(self, response: Response):
        yield {
            "url": response.url,
            "status": response.status,
            "success": True
        }

    async def handle_error(self, failure: Request, error: Exception):
        self.logger.error(f"请求失败: {failure.url}")
        self.logger.error(f"错误类型: {type(error).__name__}")
        self.logger.error(f"错误信息: {error}")

        yield {
            "url": failure.url,
            "success": False,
            "error": str(error)
        }
```

### 5. API 爬取

```python
class ApiSpider(Spider):
    name = "api"
    start_urls = ["https://api.github.com/users"]

    async def parse(self, response: Response):
        # 解析 JSON 响应
        users = response.json()

        for user in users:
            # 提取用户基本信息
            yield {
                "login": user["login"],
                "id": user["id"],
                "type": user["type"],
                "url": user["html_url"]
            }

            # 跟进用户详情
            yield Request(
                user["url"],
                callback=self.parse_user,
                meta={"login": user["login"]}
            )

    async def parse_user(self, response: Response):
        # 解析用户详情
        user_data = response.json()
        login = response.meta["login"]

        yield {
            "login": login,
            "name": user_data.get("name"),
            "company": user_data.get("company"),
            "location": user_data.get("location"),
            "followers": user_data.get("followers"),
            "following": user_data.get("following")
        }
```

### 6. 分页处理

```python
class PaginationSpider(Spider):
    name = "pagination"
    start_urls = ["https://example.com/items?page=1"]

    async def parse(self, response: Response):
        # 提取当前页数据
        for item in response.css("div.item"):
            yield {
                "title": item.css("h2::text").get(),
                "description": item.css("p::text").get(),
                "page": response.meta.get("page", 1)
            }

        # 提取下一页链接
        next_page = response.css("a.next::attr(href)").get()
        if next_page:
            # 使用 response.follow 自动处理相对 URL
            yield response.follow(
                next_page,
                callback=self.parse,
                meta={"page": response.meta.get("page", 1) + 1}
            )
```

### 7. 深度优先爬取

```python
class DepthFirstSpider(Spider):
    name = "depth_first"
    start_urls = ["https://example.com"]

    def start_requests(self):
        yield Request(
            "https://example.com",
            callback=self.parse,
            meta={"depth": 0}
        )

    async def parse(self, response: Response):
        depth = response.meta.get("depth", 0)

        # 提取数据
        yield {
            "url": response.url,
            "depth": depth,
            "title": response.css("title::text").get()
        }

        # 限制深度
        if depth >= 3:
            return

        # 跟进所有链接（深度优先）
        for link in response.css("a"):
            href = link.css("::attr(href)").get()
            if href:
                yield response.follow(
                    href,
                    callback=self.parse,
                    meta={"depth": depth + 1}
                )
```

### 8. 会话选择

```python
class SessionSpider(Spider):
    name = "session"

    sessions = {
        "default": {"type": "fetcher"},
        "browser": {"type": "dynamic", "config": {"real_chrome": True}},
        "stealth": {"type": "stealthy", "config": {"solve_cloudflare": True}}
    }

    def start_requests(self):
        # 使用不同会话
        yield Request("https://example.com/static",
                    callback=self.parse_static,
                    sid="default")

        yield Request("https://example.com/dynamic",
                    callback=self.parse_dynamic,
                    sid="browser")

        yield Request("https://example.com/protected",
                    callback=self.parse_protected,
                    sid="stealth")

    async def parse_static(self, response: Response):
        yield {"type": "static", "url": response.url}

    async def parse_dynamic(self, response: Response):
        yield {"type": "dynamic", "url": response.url}

    async def parse_protected(self, response: Response):
        yield {"type": "protected", "url": response.url}
```

## 最佳实践

### 1. 使用 start_requests

```python
# 推荐：使用 start_requests 获得更多灵活性
class GoodSpider(Spider):
    def start_requests(self):
        for url in self.start_urls:
            yield Request(url, callback=self.parse)

    async def parse(self, response: Response):
        yield {"url": response.url}
```

### 2. 合理使用元数据

```python
# 传递上下文信息
async def parse_list(self, response: Response):
    category = response.css("h1::text").get()

    for item in response.css(".item"):
        yield response.follow(
            item.css("a"),
            callback=self.parse_detail,
            meta={"category": category}
        )

async def parse_detail(self, response: Response):
    yield {
        "url": response.url,
        "category": response.meta["category"],
        "title": response.css("h1::text").get()
    }
```

### 3. 错误处理

```python
# 处理可能的错误
async def parse(self, response: Response):
    try:
        data = response.json()
        yield {"data": data}
    except Exception as e:
        self.logger.error(f"JSON 解析失败: {e}")
        yield {"error": str(e)}
```

### 4. 深度控制

```python
# 限制爬取深度
async def parse(self, response: Response):
    max_depth = 3
    current_depth = response.meta.get("depth", 0)

    yield {"url": response.url, "depth": current_depth}

    if current_depth < max_depth:
        for link in response.css("a"):
            yield response.follow(
                link,
                callback=self.parse,
                meta={"depth": current_depth + 1}
            )
```

## 总结

Request 和 Response 是 Scrapling Spider 系统的核心对象：

1. **Request 对象**：封装 HTTP 请求的所有信息
   - URL、方法、请求头、请求体
   - 优先级、会话 ID、元数据
   - 回调函数、错误处理

2. **Response 对象**：继承自 Selector，提供响应处理能力
   - 基本属性：URL、状态码、响应头、响应体
   - 选择器方法：CSS、XPath、文本选择
   - 跟进链接：自动处理相对 URL
   - JSON 解析：`response.json()`

3. **请求去重**：基于 URL、方法、body 和 sid 自动去重
4. **元数据传递**：通过 meta 字典在请求间传递数据
5. **错误处理**：通过 errback 处理请求失败

掌握 Request 和 Response 对象是构建高效、健壮爬虫的基础。

---

# 会话管理 (Sessions)

## 概述

Scrapling Spider 系统支持在同一爬虫中配置多种不同类型的会话。每个会话可以独立配置，包括抓取器类型、代理、浏览器设置等。通过指定会话 ID（sid），可以将请求路由到特定的会话。

## 会话类型

Scrapling 支持三种主要会话类型：

| 会话类型 | 对应抓取器 | 特点 |
|---------|-----------|------|
| fetcher | Fetcher/AsyncFetcher | 快速 HTTP 请求，低内存占用 |
| dynamic | DynamicFetcher/DynamicSession | 动态 JavaScript 渲染 |
| stealthy | StealthyFetcher/StealthySession | 隐身抓取，绕过反爬 |

## 基本配置

### 定义会话

在 Spider 类中通过 `sessions` 属性定义会话：

```python
from scrapling.spiders import Spider, Response

class MultiSessionSpider(Spider):
    name = "multi_session"

    # 定义多个会话
    sessions = {
        "default": {
            "type": "fetcher",
            "config": {
                "impersonate": "chrome",
                "http3": True
            }
        },
        "browser": {
            "type": "dynamic",
            "config": {
                "headless": True,
                "network_idle": True
            }
        },
        "stealth": {
            "type": "stealthy",
            "config": {
                "solve_cloudflare": True,
                "block_webrtc": True
            }
        }
    }

    def start_requests(self):
        # 使用不同会话发送请求
        yield Request("https://example.com/static",
                    callback=self.parse_static,
                    sid="default")

        yield Request("https://example.com/dynamic",
                    callback=self.parse_dynamic,
                    sid="browser")

        yield Request("https://example.com/protected",
                    callback=self.parse_protected,
                    sid="stealth")
```

### Fetcher 会话配置

```python
class FetcherSessionSpider(Spider):
    name = "fetcher_session"

    sessions = {
        "default": {
            "type": "fetcher",
            "config": {
                # TLS 指纹伪装
                "impersonate": "chrome110",

                # HTTP/3 支持
                "http3": True,

                # 代理
                "proxy": "http://proxy.example.com:8080",

                # 请求头
                "headers": {
                    "User-Agent": "Mozilla/5.0...",
                    "Accept": "text/html"
                },

                # 超时
                "timeout": 30,

                # 重试
                "retries": 3,

                # 自适应解析
                "adaptive": True
            }
        }
    }
```

### Dynamic 会话配置

```python
class DynamicSessionSpider(Spider):
    name = "dynamic_session"

    sessions = {
        "browser": {
            "type": "dynamic",
            "config": {
                # 浏览器类型
                "browser_type": "chromium",

                # 无头模式
                "headless": True,

                # 真实 Chrome
                "real_chrome": False,

                # CDP 连接
                "cdp_url": None,

                # 禁用资源
                "disable_resources": False,

                # 网络空闲等待
                "network_idle": False,

                # 阻塞域名
                "blocked_domains": [],

                # 超时
                "timeout": 30,

                # 视口
                "viewport": {"width": 1920, "height": 1080},

                # 代理
                "proxy": "http://proxy.example.com:8080"
            }
        }
    }
```

### Stealthy 会话配置

```python
class StealthySessionSpider(Spider):
    name = "stealthy_session"

    sessions = {
        "stealth": {
            "type": "stealthy",
            "config": {
                # Cloudflare 绕过
                "solve_cloudflare": True,

                # WebRTC 阻塞
                "block_webrtc": True,

                # Canvas 隐藏
                "hide_canvas": True,

                # WebGL 支持
                "allow_webgl": True,

                # 真实 Chrome
                "real_chrome": True,

                # Google 搜索模拟
                "google_search": False,

                # 无头模式
                "headless": True,

                # 网络空闲
                "network_idle": True,

                # 超时
                "timeout": 60
            }
        }
    }
```

## 使用会话

### 指定会话 ID

通过 `sid` 参数指定请求使用的会话：

```python
class MySpider(Spider):
    sessions = {
        "default": {"type": "fetcher"},
        "browser": {"type": "dynamic"},
        "stealth": {"type": "stealthy"}
    }

    def start_requests(self):
        # 使用 default 会话
        yield Request("https://example.com/page1",
                    callback=self.parse,
                    sid="default")

        # 使用 browser 会话
        yield Request("https://example.com/dynamic",
                    callback=self.parse_dynamic,
                    sid="browser")

        # 使用 stealth 会话
        yield Request("https://example.com/protected",
                    callback=self.parse_protected,
                    sid="stealth")
```

### 默认会话

如果不指定 `sid`，则使用名为 "default" 的会话：

```python
class MySpider(Spider):
    sessions = {
        "default": {
            "type": "fetcher",
            "config": {"impersonate": "chrome"}
        }
    }

    def start_requests(self):
        # 使用 default 会话
        yield Request("https://example.com",
                    callback=self.parse)

        # 显式指定 default 会话
        yield Request("https://example.com/page2",
                    callback=self.parse,
                    sid="default")
```

## 会话优先级

### 请求级 vs 会话级配置

请求级别的配置可以覆盖会话级别的配置：

```python
class PrioritySpider(Spider):
    sessions = {
        "browser": {
            "type": "dynamic",
            "config": {
                "headless": True,
                "timeout": 30
            }
        }
    }

    def start_requests(self):
        # 使用会话的默认配置
        yield Request("https://example.com/normal",
                    callback=self.parse,
                    sid="browser")

        # 覆盖超时配置
        yield Request("https://example.com/slow",
                    callback=self.parse,
                    sid="browser",
                    timeout=60)
```

## 多会话协同

### 混合使用不同会话

```python
class HybridSpider(Spider):
    name = "hybrid"

    sessions = {
        "static": {
            "type": "fetcher",
            "config": {"impersonate": "chrome"}
        },
        "dynamic": {
            "type": "dynamic",
            "config": {"headless": True}
        }
    }

    def start_requests(self):
        # 使用快速 HTTP 会话抓取列表页
        yield Request("https://example.com/products",
                    callback=self.parse_list,
                    sid="static")

    async def parse_list(self, response: Response):
        # 提取产品链接
        for product_link in response.css("a.product-link"):
            href = product_link.css("::attr(href)").get()
            if href:
                # 使用动态会话抓取详情页
                yield response.follow(
                    href,
                    callback=self.parse_product,
                    sid="dynamic"
                )

    async def parse_product(self, response: Response):
        yield {
            "url": response.url,
            "title": response.css("h1::text").get(),
            "price": response.css(".price::text").get()
        }
```

### 会话间数据传递

```python
class DataShareSpider(Spider):
    name = "data_share"

    sessions = {
        "list": {"type": "fetcher"},
        "detail": {"type": "dynamic"}
    }

    def start_requests(self):
        yield Request("https://example.com/items",
                    callback=self.parse_list,
                    sid="list")

    async def parse_list(self, response: Response):
        # 提取列表数据
        items = []
        for item in response.css("div.item"):
            items.append({
                "id": item.css("::attr(data-id)").get(),
                "title": item.css("h3::text").get()
            })

            # 使用动态会话抓取详情
            link = item.css("a::attr(href)").get()
            yield response.follow(
                link,
                callback=self.parse_detail,
                sid="detail",
                meta={"list_data": item}  # 传递列表数据
            )
```

## 代理配置

### 代理轮换

```python
from scrapling.fetchers import ProxyRotator

class ProxySpider(Spider):
    name = "proxy_spider"

    # 创建代理轮换器
    proxy_rotator = ProxyRotator([
        "http://proxy1.example.com:8080",
        "http://proxy2.example.com:8080",
        "http://proxy3.example.com:8080"
    ])

    sessions = {
        "proxied": {
            "type": "fetcher",
            "config": {
                "proxy_rotator": proxy_rotator
            }
        }
    }

    def start_requests(self):
        # 自动使用代理轮换
        yield Request("https://example.com",
                    callback=self.parse,
                    sid="proxied")
```

### 代理文件

```python
class ProxyFileSpider(Spider):
    name = "proxy_file"

    # 从文件加载代理
    proxy_rotator = ProxyRotator.from_file("proxies.txt")

    sessions = {
        "proxied": {
            "type": "fetcher",
            "config": {
                "proxy_rotator": proxy_rotator
            }
        }
    }
```

### 不同会话使用不同代理

```python
class MultiProxySpider(Spider):
    name = "multi_proxy"

    proxy_rotator1 = ProxyRotator(["http://proxy1:8080"])
    proxy_rotator2 = ProxyRotator(["http://proxy2:8080"])

    sessions = {
        "session1": {
            "type": "fetcher",
            "config": {"proxy_rotator": proxy_rotator1}
        },
        "session2": {
            "type": "fetcher",
            "config": {"proxy_rotator": proxy_rotator2}
        }
    }
```

## Cookie 管理

### 会话间共享 Cookie

```python
class CookieSpider(Spider):
    name = "cookie"

    sessions = {
        "login": {
            "type": "dynamic",
            "config": {"headless": False}
        },
        "data": {
            "type": "fetcher"
        }
    }

    def start_requests(self):
        # 使用浏览器登录
        yield Request("https://example.com/login",
                    callback=self.parse_login,
                    sid="login")

    async def parse_login(self, response: Response):
        # 登录逻辑（这里简化）
        # 提交登录表单
        yield Request(
            "https://example.com/login/action",
            method="POST",
            data={"username": "user", "password": "pass"},
            callback=self.parse_after_login,
            sid="login"
        )

    async def parse_after_login(self, response: Response):
        # 登录成功，使用相同的 sid 继续抓取
        # Cookie 会被自动保存和复用
        yield Request("https://example.com/protected/data",
                    callback=self.parse_data,
                    sid="login")

    async def parse_data(self, response: Response):
        # 访问受保护的数据
        yield {"data": response.css(".content::text").get()}
```

## 会话生命周期

### 会话复用

同一会话的请求会复用相同的连接和浏览器实例：

```python
class SessionReuseSpider(Spider):
    name = "session_reuse"

    sessions = {
        "browser": {
            "type": "dynamic",
            "config": {
                "headless": True,
                "max_pages": 5  # 最多 5 个并发页面
            }
        }
    }

    def start_requests(self):
        urls = [
            "https://example.com/page1",
            "https://example.com/page2",
            "https://example.com/page3"
        ]

        # 这些请求会复用同一个浏览器实例
        for url in urls:
            yield Request(url,
                        callback=self.parse,
                        sid="browser")
```

### 会话清理

会话在爬虫关闭时自动清理：

```python
class CleanupSpider(Spider):
    name = "cleanup"

    sessions = {
        "browser": {"type": "dynamic"}
    }

    async def on_close(self):
        # 爬虫关闭时清理资源
        self.logger.info("爬虫关闭，清理会话资源")
```

## 实用示例

### 1. 电商网站抓取

```python
class EcommerceSpider(Spider):
    name = "ecommerce"

    sessions = {
        # 列表页使用快速 HTTP
        "list": {
            "type": "fetcher",
            "config": {
                "impersonate": "chrome",
                "http3": True
            }
        },
        # 详情页使用动态渲染
        "detail": {
            "type": "dynamic",
            "config": {
                "headless": True,
                "network_idle": True
            }
        },
        # 支付页面使用隐身模式
        "payment": {
            "type": "stealthy",
            "config": {
                "solve_cloudflare": True
            }
        }
    }

    def start_requests(self):
        yield Request("https://shop.example.com/products",
                    callback=self.parse_list,
                    sid="list")

    async def parse_list(self, response: Response):
        for product in response.css("div.product"):
            link = product.css("a::attr(href)").get()
            if link:
                yield response.follow(link,
                                    callback=self.parse_detail,
                                    sid="detail")

    async def parse_detail(self, response: Response):
        yield {
            "title": response.css("h1::text").get(),
            "price": response.css(".price::text").get(),
            "description": response.css(".description::text").get()
        }
```

### 2. 新闻网站抓取

```python
class NewsSpider(Spider):
    name = "news"

    sessions = {
        "home": {
            "type": "fetcher",
            "config": {"impersonate": "chrome"}
        },
        "article": {
            "type": "dynamic",
            "config": {
                "headless": True,
                "wait_selector": ".article-content"
            }
        }
    }

    def start_requests(self):
        yield Request("https://news.example.com",
                    callback=self.parse_home,
                    sid="home")

    async def parse_home(self, response: Response):
        for article_link in response.css("a.article"):
            yield response.follow(
                article_link,
                callback=self.parse_article,
                sid="article"
            )

    async def parse_article(self, response: Response):
        yield {
            "title": response.css("h1::text").get(),
            "content": response.css(".article-content::text").getall(),
            "author": response.css(".author::text").get()
        }
```

### 3. 多域名爬取

```python
class MultiDomainSpider(Spider):
    name = "multi_domain"

    sessions = {
        "domain1": {
            "type": "fetcher",
            "config": {
                "impersonate": "chrome",
                "proxy": "http://proxy1:8080"
            }
        },
        "domain2": {
            "type": "dynamic",
            "config": {
                "headless": True,
                "proxy": "http://proxy2:8080"
            }
        }
    }

    def start_requests(self):
        # 不同域名使用不同会话
        yield Request("https://domain1.example.com",
                    callback=self.parse,
                    sid="domain1")

        yield Request("https://domain2.example.com",
                    callback=self.parse,
                    sid="domain2")

    async def parse(self, response: Response):
        yield {
            "url": response.url,
            "title": response.css("title::text").get()
        }
```

### 4. 登录认证

```python
class AuthenticatedSpider(Spider):
    name = "authenticated"

    sessions = {
        # 登录使用浏览器
        "login": {
            "type": "dynamic",
            "config": {"headless": False}
        },
        # 认证后使用快速 HTTP
        "auth": {
            "type": "fetcher"
        }
    }

    def start_requests(self):
        # 第一步：登录
        yield Request("https://example.com/login",
                    callback=self.parse_login_page,
                    sid="login")

    async def parse_login_page(self, response: Response):
        # 获取表单 token
        token = response.css("input[name='token']::attr(value)").get()

        # 提交登录
        yield Request(
            "https://example.com/login",
            method="POST",
            data={
                "username": "user",
                "password": "pass",
                "token": token
            },
            callback=self.parse_after_login,
            sid="login"
        )

    async def parse_after_login(self, response: Response):
        # 登录成功，切换到 HTTP 会话抓取数据
        # 注意：需要手动传递 Cookie
        cookies = response.request.cookies

        yield Request(
            "https://example.com/dashboard",
            callback=self.parse_dashboard,
            sid="auth",
            cookies=cookies
        )

    async def parse_dashboard(self, response: Response):
        yield {"dashboard": response.css(".data::text").getall()}
```

### 5. 故障降级

```python
class FallbackSpider(Spider):
    name = "fallback"

    sessions = {
        "fast": {"type": "fetcher"},
        "slow": {"type": "dynamic"},
        "stealth": {"type": "stealthy"}
    }

    def start_requests(self):
        # 首先尝试快速会话
        yield Request("https://example.com/data",
                    callback=self.parse,
                    errback=self.handle_error,
                    sid="fast")

    async def parse(self, response: Response):
        yield {"success": True, "data": response.text}

    async def handle_error(self, failure: Request, error: Exception):
        self.logger.warning(f"快速会话失败: {error}")

        # 降级到动态会话
        yield Request(
            failure.url,
            callback=self.parse,
            errback=self.handle_error2,
            sid="slow"
        )

    async def handle_error2(self, failure: Request, error: Exception):
        self.logger.warning(f"动态会话失败: {error}")

        # 最后降级到隐身会话
        yield Request(
            failure.url,
            callback=self.parse,
            sid="stealth"
        )
```

## 最佳实践

### 1. 合理选择会话类型

```python
class OptimalSpider(Spider):
    sessions = {
        # 静态内容：快速 HTTP
        "static": {"type": "fetcher"},

        # 轻量 JS：动态浏览器
        "dynamic": {"type": "dynamic"},

        # 强保护：隐身模式
        "stealth": {"type": "stealthy"}
    }
```

### 2. 命名清晰的会话

```python
# 推荐：使用描述性名称
sessions = {
    "list_page": {"type": "fetcher"},
    "detail_page": {"type": "dynamic"},
    "checkout": {"type": "stealthy"}
}

# 避免：使用无意义的名称
sessions = {
    "session1": {"type": "fetcher"},
    "session2": {"type": "dynamic"}
}
```

### 3. 共享配置

```python
class SharedConfigSpider(Spider):
    # 共享的代理
    proxy_rotator = ProxyRotator(["http://proxy:8080"])

    sessions = {
        "session1": {
            "type": "fetcher",
            "config": {"proxy_rotator": proxy_rotator}
        },
        "session2": {
            "type": "dynamic",
            "config": {"proxy_rotator": proxy_rotator}
        }
    }
```

### 4. 监控会话使用

```python
class MonitoredSpider(Spider):
    async def on_close(self):
        # 查看会话使用统计
        stats = self.stats

        if hasattr(stats, 'sessions_requests_count'):
            for sid, count in stats.sessions_requests_count.items():
                self.logger.info(f"会话 {sid}: {count} 个请求")
```

## 总结

Scrapling 的会话管理提供了强大的多会话支持：

1. **三种会话类型**：fetcher、dynamic、stealthy
2. **灵活配置**：每个会话独立配置参数
3. **请求路由**：通过 sid 指定使用的会话
4. **会话复用**：同一会话共享连接和资源
5. **代理支持**：支持代理轮换和代理池
6. **Cookie 管理**：会话间自动共享 Cookie
7. **故障降级**：支持会话切换和重试

合理使用多会话可以构建高效、灵活、健壮的爬虫系统。

---

# 代理管理与阻塞处理 (Proxy Management & Blocking)

## 概述

Scrapling Spider 系统提供了强大的代理管理功能和内置的阻塞检测机制。代理管理支持代理池、自动轮换、健康检查等功能。阻塞处理能够自动检测网站的反爬行为，并提供灵活的重试策略。

## 代理管理

### ProxyRotator 类

`ProxyRotator` 是代理轮换的核心类，支持从列表或文件加载代理，并自动在请求间轮换。

### 基本使用

#### 从列表加载代理

```python
from scrapling.fetchers import ProxyRotator

# 创建代理轮换器
proxy_rotator = ProxyRotator([
    "http://proxy1.example.com:8080",
    "http://proxy2.example.com:8080",
    "http://proxy3.example.com:8080"
])

# 在 Spider 中使用
class MySpider(Spider):
    name = "my_spider"

    sessions = {
        "default": {
            "type": "fetcher",
            "config": {
                "proxy_rotator": proxy_rotator
            }
        }
    }
```

#### 从文件加载代理

```python
# 从文件加载代理列表
proxy_rotator = ProxyRotator.from_file("proxies.txt")

# proxies.txt 格式：
# http://proxy1.example.com:8080
# http://proxy2.example.com:8080
# http://user:pass@proxy3.example.com:8080
```

#### 认证代理

```python
# 支持用户名和密码认证
proxy_rotator = ProxyRotator([
    "http://user1:pass1@proxy1.example.com:8080",
    "http://user2:pass2@proxy2.example.com:8080"
])
```

### 在 Spider 中配置代理

#### 单一代理

```python
class SingleProxySpider(Spider):
    name = "single_proxy"

    sessions = {
        "default": {
            "type": "fetcher",
            "config": {
                "proxy": "http://proxy.example.com:8080"
            }
        }
    }
```

#### 代理轮换

```python
class RotatingProxySpider(Spider):
    name = "rotating_proxy"

    proxy_rotator = ProxyRotator([
        "http://proxy1.example.com:8080",
        "http://proxy2.example.com:8080",
        "http://proxy3.example.com:8080"
    ])

    sessions = {
        "default": {
            "type": "fetcher",
            "config": {
                "proxy_rotator": proxy_rotator
            }
        }
    }
```

#### 不同会话不同代理

```python
class MultiProxySpider(Spider):
    name = "multi_proxy"

    # 为不同会话配置不同的代理
    proxy_rotator1 = ProxyRotator(["http://proxy1:8080", "http://proxy2:8080"])
    proxy_rotator2 = ProxyRotator(["http://proxy3:8080", "http://proxy4:8080"])

    sessions = {
        "session1": {
            "type": "fetcher",
            "config": {"proxy_rotator": proxy_rotator1}
        },
        "session2": {
            "type": "fetcher",
            "config": {"proxy_rotator": proxy_rotator2}
        }
    }
```

### 代理与健康检查

#### 健康检查

```python
# 启用健康检查
proxy_rotator = ProxyRotator([
    "http://proxy1.example.com:8080",
    "http://proxy2.example.com:8080"
], check_health=True)

# 健康检查参数
proxy_rotator = ProxyRotator(
    proxies,
    check_health=True,
    health_check_url="https://httpbin.org/ip",
    health_check_timeout=10,
    health_check_retries=3
)
```

#### 代理权重

```python
# 为不同代理设置权重（权重越高，使用频率越高）
proxy_rotator = ProxyRotator([
    ("http://fast-proxy.example.com:8080", 5),  # 权重 5
    ("http://normal-proxy.example.com:8080", 3),  # 权重 3
    ("http://slow-proxy.example.com:8080", 1)   # 权重 1
])
```

## 阻塞处理

### 内置阻塞检测

Scrapling 提供了内置的阻塞检测机制，能够自动识别常见的反爬行为：

1. **HTTP 状态码**：403、429、503 等
2. **内容检测**：检测页面中的特定关键词
3. **验证码**：检测验证码页面
4. **JavaScript 挑战**：检测 Cloudflare 等挑战

### 默认阻塞检测

默认情况下，系统会自动检测以下情况：

- HTTP 状态码 403（禁止访问）
- HTTP 状态码 429（请求过多）
- HTTP 状态码 503（服务不可用）
- 页面内容包含 "access denied"、"blocked"、"captcha" 等关键词

### 自定义阻塞检测

重写 `is_blocked()` 方法实现自定义检测逻辑：

```python
class CustomBlockingSpider(Spider):
    name = "custom_blocking"

    max_blocked_retries = 5

    async def is_blocked(self, response: Response) -> bool:
        """自定义阻塞检测"""

        # 检查状态码
        if response.status in [403, 429, 503]:
            return True

        # 检查内容
        text = response.text.lower()

        # 检测常见阻塞关键词
        blocked_keywords = [
            "access denied",
            "blocked",
            "captcha",
            "verify you are human",
            "cloudflare"
        ]

        for keyword in blocked_keywords:
            if keyword in text:
                self.logger.warning(f"检测到阻塞关键词: {keyword}")
                return True

        # 检测页面重定向到验证页面
        if "verify" in response.url.lower() or "captcha" in response.url.lower():
            self.logger.warning("重定向到验证页面")
            return True

        # 自定义检测逻辑
        if len(response.css("div.captcha")) > 0:
            self.logger.warning("检测到验证码元素")
            return True

        return False
```

### 阻塞重试配置

配置最大重试次数：

```python
class RetrySpider(Spider):
    name = "retry"

    # 最大阻塞重试次数
    max_blocked_retries = 3

    async def is_blocked(self, response: Response) -> bool:
        return response.status in [403, 429, 503]
```

### 重试策略

重写 `retry_blocked_request()` 方法实现自定义重试策略：

```python
class RetryStrategySpider(Spider):
    name = "retry_strategy"

    max_blocked_retries = 5

    async def is_blocked(self, response: Response) -> bool:
        return response.status == 429

    async def retry_blocked_request(self, request: Request, response: Response) -> Request:
        """自定义重试策略"""

        # 403：切换到隐身会话
        if response.status == 403:
            self.logger.warning("403 错误，切换到隐身会话")
            request.sid = "stealth"

        # 429：增加延迟并切换代理
        elif response.status == 429:
            self.logger.warning("429 错误，增加延迟")
            request.meta["delay"] = request.meta.get("delay", 0) + 5

        # 503：切换代理
        elif response.status == 503:
            self.logger.warning("503 错误，切换代理")
            # 代理会自动轮换

        return request
```

## 完整示例

### 1. 基本代理使用

```python
from scrapling.spiders import Spider, Response, Request
from scrapling.fetchers import ProxyRotator

class BasicProxySpider(Spider):
    name = "basic_proxy"

    # 配置代理轮换
    proxy_rotator = ProxyRotator([
        "http://proxy1.example.com:8080",
        "http://proxy2.example.com:8080",
        "http://proxy3.example.com:8080"
    ])

    sessions = {
        "default": {
            "type": "fetcher",
            "config": {
                "proxy_rotator": proxy_rotator
            }
        }
    }

    def start_requests(self):
        urls = [
            "https://httpbin.org/ip",
            "https://httpbin.org/ip",
            "https://httpbin.org/ip"
        ]

        for url in urls:
            yield Request(url, callback=self.parse)

    async def parse(self, response: Response):
        # 查看使用的代理 IP
        ip_info = response.json()
        yield {
            "url": response.url,
            "ip": ip_info.get("origin"),
            "used_proxy": response.request.meta.get("proxy", "direct")
        }
```

### 2. 带健康检查的代理

```python
class HealthCheckSpider(Spider):
    name = "health_check"

    # 启用健康检查
    proxy_rotator = ProxyRotator.from_file(
        "proxies.txt",
        check_health=True,
        health_check_url="https://httpbin.org/ip",
        health_check_timeout=10
    )

    sessions = {
        "default": {
            "type": "fetcher",
            "config": {
                "proxy_rotator": proxy_rotator
            }
        }
    }

    async def on_start(self, resuming: bool = False):
        # 爬虫启动时，检查代理健康状态
        if not resuming:
            self.logger.info(f"代理总数: {len(self.proxy_rotator.proxies)}")
            self.logger.info(f"健康代理数: {len(self.proxy_rotator.healthy_proxies)}")
```

### 3. 高级阻塞处理

```python
class AdvancedBlockingSpider(Spider):
    name = "advanced_blocking"

    max_blocked_retries = 10

    # 代理轮换
    proxy_rotator = ProxyRotator([
        "http://proxy1:8080",
        "http://proxy2:8080",
        "http://proxy3:8080"
    ])

    sessions = {
        "default": {
            "type": "fetcher",
            "config": {
                "proxy_rotator": proxy_rotator
            }
        },
        "stealth": {
            "type": "stealthy",
            "config": {
                "solve_cloudflare": True
            }
        }
    }

    async def is_blocked(self, response: Response) -> bool:
        """多层级阻塞检测"""

        # 1. 状态码检测
        if response.status in [403, 429, 503]:
            self.logger.warning(f"阻塞状态码: {response.status}")
            return True

        # 2. 内容关键词检测
        text = response.text.lower()
        blocked_keywords = [
            "access denied",
            "blocked",
            "captcha",
            "verify you are human",
            "rate limit"
        ]

        for keyword in blocked_keywords:
            if keyword in text:
                self.logger.warning(f"阻塞关键词: {keyword}")
                return True

        # 3. 元素检测
        if response.css("div.captcha") or response.css("#captcha-form"):
            self.logger.warning("检测到验证码")
            return True

        # 4. URL 检测
        if "blocked" in response.url or "captcha" in response.url:
            self.logger.warning(f"阻塞 URL: {response.url}")
            return True

        return False

    async def retry_blocked_request(self, request: Request, response: Response) -> Request:
        """智能重试策略"""

        retry_count = request.meta.get("blocked_retry_count", 0)
        request.meta["blocked_retry_count"] = retry_count + 1

        self.logger.info(f"重试 #{retry_count + 1} - {request.url}")

        # 403：切换到隐身会话
        if response.status == 403:
            if request.sid != "stealth":
                self.logger.info("切换到隐身会话")
                request.sid = "stealth"
            else:
                self.logger.warning("已使用隐身会话，继续尝试")

        # 429：增加延迟
        elif response.status == 429:
            delay = min(2 ** retry_count, 60)  # 指数退避，最大 60 秒
            self.logger.info(f"增加延迟: {delay} 秒")
            request.meta["delay"] = delay

        # 其他错误：代理会自动轮换
        else:
            self.logger.info("代理自动轮换")

        return request
```

### 4. 多会话代理配置

```python
class MultiSessionProxySpider(Spider):
    name = "multi_session_proxy"

    # 不同会话使用不同的代理池
    fast_proxies = ProxyRotator([
        "http://fast1:8080",
        "http://fast2:8080"
    ])

    slow_proxies = ProxyRotator([
        "http://slow1:8080",
        "http://slow2:8080"
    ])

    sessions = {
        "fast": {
            "type": "fetcher",
            "config": {
                "proxy_rotator": fast_proxies,
                "timeout": 10
            }
        },
        "slow": {
            "type": "fetcher",
            "config": {
                "proxy_rotator": slow_proxies,
                "timeout": 30
            }
        }
    }

    def start_requests(self):
        # 使用快速代理池
        yield Request("https://example.com/quick",
                    callback=self.parse,
                    sid="fast")

        # 使用慢速代理池
        yield Request("https://example.com/complex",
                    callback=self.parse,
                    sid="slow")
```

### 5. 代理认证

```python
class AuthProxySpider(Spider):
    name = "auth_proxy"

    # 认证代理
    proxy_rotator = ProxyRotator([
        "http://user1:pass1@proxy1.example.com:8080",
        "http://user2:pass2@proxy2.example.com:8080"
    ])

    sessions = {
        "default": {
            "type": "fetcher",
            "config": {
                "proxy_rotator": proxy_rotator
            }
        }
    }
```

### 6. 代理故障转移

```python
class FailoverSpider(Spider):
    name = "failover"

    # 主要代理
    primary_proxies = ProxyRotator([
        "http://primary1:8080",
        "http://primary2:8080"
    ])

    # 备用代理
    backup_proxies = ProxyRotator([
        "http://backup1:8080",
        "http://backup2:8080"
    ])

    sessions = {
        "default": {
            "type": "fetcher",
            "config": {
                "proxy_rotator": primary_proxies
            }
        },
        "backup": {
            "type": "fetcher",
            "config": {
                "proxy_rotator": backup_proxies
            }
        }
    }

    async def retry_blocked_request(self, request: Request, response: Response) -> Request:
        """故障转移策略"""

        retry_count = request.meta.get("blocked_retry_count", 0)
        request.meta["blocked_retry_count"] = retry_count + 1

        # 3 次重试后切换到备用代理
        if retry_count >= 3:
            if request.sid != "backup":
                self.logger.warning("切换到备用代理")
                request.sid = "backup"
            else:
                self.logger.error("备用代理也失败")

        return request
```

### 7. 监控代理使用

```python
class MonitoredProxySpider(Spider):
    name = "monitored_proxy"

    proxy_rotator = ProxyRotator.from_file("proxies.txt")

    sessions = {
        "default": {
            "type": "fetcher",
            "config": {
                "proxy_rotator": proxy_rotator
            }
        }
    }

    async def parse(self, response: Response):
        # 记录使用的代理
        proxy = response.request.meta.get("proxy", "direct")
        self.logger.info(f"URL: {response.url} | 代理: {proxy}")

        yield {"url": response.url}

    async def on_close(self):
        # 爬虫结束时打印代理使用统计
        stats = self.stats

        if hasattr(stats, 'proxies'):
            self.logger.info("=" * 50)
            self.logger.info("代理使用统计")
            self.logger.info("=" * 50)

            for proxy, count in stats.proxies.items():
                self.logger.info(f"{proxy}: {count} 次请求")

            self.logger.info("=" * 50)
```

## 最佳实践

### 1. 代理池管理

```python
# 推荐：使用代理轮换器
proxy_rotator = ProxyRotator(proxies)

# 避免：手动管理代理
# session.config["proxy"] = "random choice"  # 不推荐
```

### 2. 健康检查

```python
# 对于大量代理，启用健康检查
proxy_rotator = ProxyRotator.from_file(
    "proxies.txt",
    check_health=True,
    health_check_url="https://httpbin.org/ip"
)
```

### 3. 分层代理策略

```python
# 不同场景使用不同质量代理
class TieredProxySpider(Spider):
    high_quality = ProxyRotator(["http://premium1:8080"])
    medium_quality = ProxyRotator(["http://standard1:8080"])
    low_quality = ProxyRotator(["http://free1:8080"])
```

### 4. 阻塞检测策略

```python
# 多层级检测提高准确性
async def is_blocked(self, response: Response) -> bool:
    # 状态码
    if response.status in [403, 429, 503]:
        return True

    # 内容
    if "blocked" in response.text.lower():
        return True

    # 元素
    if response.css("div.captcha"):
        return True

    return False
```

### 5. 智能重试

```python
# 根据错误类型采用不同策略
async def retry_blocked_request(self, request: Request, response: Response) -> Request:
    if response.status == 403:
        request.sid = "stealth"  # 切换会话
    elif response.status == 429:
        request.meta["delay"] = 5  # 增加延迟

    return request
```

## 代理统计

### 访问代理使用统计

```python
# 爬虫结束后查看代理统计
result = spider.start()
stats = result.stats

if hasattr(stats, 'proxies'):
    for proxy, count in stats.proxies.items():
        print(f"{proxy}: {count} 次")

# 输出示例：
# http://proxy1:8080: 45 次
# http://proxy2:8080: 32 次
# http://proxy3:8080: 23 次
```

### 阻塞统计

```python
# 查看阻塞相关统计
print(f"阻塞请求数: {stats.blocked_requests_count}")
print(f"离站请求数: {stats.offsite_requests_count}")
print(f"失败请求数: {stats.failed_requests_count}")
```

## 总结

Scrapling 提供了强大的代理管理和阻塞处理功能：

1. **代理管理**：
   - `ProxyRotator` 类支持代理轮换
   - 从列表或文件加载代理
   - 支持代理认证
   - 健康检查和权重配置

2. **阻塞处理**：
   - 内置阻塞检测机制
   - 自定义阻塞检测逻辑
   - 灵活的重试策略
   - 智能故障转移

3. **多会话支持**：
   - 不同会话使用不同代理池
   - 代理与会话独立配置
   - 支持代理认证和切换

4. **统计和监控**：
   - 代理使用统计
   - 阻塞请求统计
   - 详细的日志记录

合理配置代理和阻塞处理，可以显著提高爬虫的稳定性和成功率。

---

# 高级特性 (Advanced Features)

## 概述

Scrapling Spider 系统提供了多种高级特性，包括并发控制、暂停/恢复、流式处理、生命周期钩子和日志配置等。这些功能可以帮助你构建更强大、更灵活的爬虫。

## 并发控制

### 基本概念

并发控制是爬虫性能优化的关键。Scrapling 提供了全局并发和每域名并发两个层面的控制。

### 全局并发控制

设置整个爬虫的最大并发请求数：

```python
from scrapling.spiders import Spider, Response

class ConcurrentSpider(Spider):
    name = "concurrent"

    # 全局最大并发请求数
    concurrent_requests = 10

    async def parse(self, response: Response):
        yield {"url": response.url}
```

### 每域名并发控制

限制对单个域名的并发请求数：

```python
class PerDomainSpider(Spider):
    name = "per_domain"

    # 全局并发
    concurrent_requests = 50

    # 每域名并发
    per_domain_concurrent_requests = 3

    async def parse(self, response: Response):
        yield {"url": response.url}
```

### 下载延迟

设置请求之间的延迟时间：

```python
class DelayedSpider(Spider):
    name = "delayed"

    # 每个请求之间延迟 0.5 秒
    download_delay = 0.5

    async def parse(self, response: Response):
        yield {"url": response.url}
```

### 并发配置示例

```python
class OptimizedSpider(Spider):
    name = "optimized"

    # 全局并发：50 个请求
    concurrent_requests = 50

    # 每域名并发：最多 3 个
    per_domain_concurrent_requests = 3

    # 请求延迟：0.2 秒
    download_delay = 0.2

    def start_requests(self):
        # 这些请求会被并发执行
        urls = [f"https://example.com/page{i}" for i in range(1, 101)]

        for url in urls:
            yield Request(url, callback=self.parse)

    async def parse(self, response: Response):
        yield {"url": response.url}
```

## 暂停与恢复

### 基本概念

检查点系统允许你暂停爬虫并在之后恢复，从上次中断的地方继续抓取，避免重复工作。

### 启用检查点

通过 `crawldir` 参数启用检查点功能：

```python
class ResumableSpider(Spider):
    name = "resumable"

    # 指定爬取状态保存目录
    crawldir = "./crawl_state"

    async def parse(self, response: Response):
        yield {"url": response.url}
```

### 使用检查点

```python
# 创建爬虫实例
spider = ResumableSpider()

# 第一次运行
result = spider.start()

# 可以按 Ctrl+C 暂停，或等待完成

# 恢复运行（从上次中断的地方继续）
result = spider.start()

# 检查是否从检查点恢复
if result.stats.resumed:
    print("从检查点恢复")
else:
    print("新的爬取")
```

### 检查点内容

检查点保存以下信息：

1. 待处理请求队列
2. 已处理请求指纹集合（用于去重）
3. 爬取统计数据
4. 爬虫状态

### 手动保存检查点

```python
class ManualCheckpointSpider(Spider):
    name = "manual_checkpoint"

    crawldir = "./crawl_state"

    async def parse(self, response: Response):
        # 提取数据
        yield {"url": response.url}

        # 每处理 100 个项目手动保存一次检查点
        if self.stats.items_scraped % 100 == 0:
            self.save_checkpoint()
            self.logger.info(f"已保存检查点，处理了 {self.stats.items_scraped} 个项目")
```

## 流式处理

### 基本概念

流式处理允许你实时处理爬取的数据，而不是等待所有数据收集完成后再处理。这对于大规模爬取特别有用。

### 使用 stream() 方法

```python
import asyncio

class StreamingSpider(Spider):
    name = "streaming"

    def start_requests(self):
        urls = [f"https://example.com/page{i}" for i in range(1, 101)]

        for url in urls:
            yield Request(url, callback=self.parse)

    async def parse(self, response: Response):
        yield {
            "url": response.url,
            "title": response.css("title::text").get()
        }

# 流式运行爬虫
async def main():
    spider = StreamingSpider()

    # 实时处理每个项目
    async for item in spider.stream():
        # 实时保存或处理项目
        print(f"处理项目: {item['url']}")

        # 可以实时写入数据库
        # db.save(item)

        # 查看实时统计
        print(f"已抓取: {spider.stats.items_scraped}")

asyncio.run(main())
```

### 流式处理的优点

1. **内存高效**：不需要在内存中保存所有数据
2. **实时输出**：数据立即可用
3. **容错性**：即使爬虫失败，已处理的数据不会丢失
4. **可扩展**：适合大规模数据爬取

### 流式处理示例

```python
class RealTimeSpider(Spider):
    name = "realtime"

    def start_requests(self):
        urls = [f"https://api.example.com/data/{i}" for i in range(1, 1001)]

        for url in urls:
            yield Request(url, callback=self.parse)

    async def parse(self, response: Response):
        try:
            data = response.json()
            yield data
        except:
            self.logger.error(f"JSON 解析失败: {response.url}")
```

```python
# 流式处理并实时保存
import json

async def stream_to_file():
    spider = RealTimeSpider()

    with open("output.jsonl", "w") as f:
        async for item in spider.stream():
            # 每行一个 JSON 对象
            f.write(json.dumps(item) + "\n")

            print(f"已保存: {spider.stats.items_scraped}")

asyncio.run(stream_to_file())
```

## 生命周期钩子

### 可用的生命周期钩子

Scrapling 提供了多个生命周期钩子，允许你在爬虫的不同阶段执行自定义逻辑：

1. `on_start(resuming=False)`：爬虫启动时调用
2. `on_close()`：爬虫关闭时调用
3. `on_scraped_item(item)`：每次抓取一个项目时调用
4. `on_dropped_item(item, reason)`：丢弃一个项目时调用
5. `on_error(request, error)`：请求发生错误时调用
6. `on_retry(request, reason)`：请求重试时调用

### on_start

```python
class OnStartSpider(Spider):
    name = "on_start"

    async def on_start(self, resuming: bool = False):
        """爬虫启动时调用"""

        if resuming:
            self.logger.info("从检查点恢复爬取")
            self.logger.info(f"已完成: {self.stats.items_scraped}")
        else:
            self.logger.info("开始新的爬取")

        # 初始化资源
        self.setup_database()
        self.load_proxies()

    def setup_database(self):
        """设置数据库连接"""
        self.logger.info("初始化数据库连接")

    def load_proxies(self):
        """加载代理列表"""
        self.logger.info("加载代理列表")
```

### on_close

```python
class OnCloseSpider(Spider):
    name = "on_close"

    async def on_close(self):
        """爬虫关闭时调用"""

        self.logger.info("爬虫关闭")

        # 打印统计信息
        self.log_stats()

        # 清理资源
        self.cleanup_resources()

    def log_stats(self):
        """记录统计信息"""
        stats = self.stats
        self.logger.info(f"总请求数: {stats.requests_count}")
        self.logger.info(f"成功数: {stats.successful_requests_count}")
        self.logger.info(f"失败数: {stats.failed_requests_count}")
        self.logger.info(f"抓取项: {stats.items_items_scraped}")

    def cleanup_resources(self):
        """清理资源"""
        self.logger.info("清理资源")
```

### on_scraped_item

```python
class OnScrapedItemSpider(Spider):
    name = "on_scraped_item"

    def start_requests(self):
        for url in self.start_urls:
            yield Request(url, callback=self.parse)

    async def parse(self, response: Response):
        yield {
            "url": response.url,
            "title": response.css("title::text").get()
        }

    async def on_scraped_item(self, item):
        """每次抓取一个项目时调用"""

        # 实时处理项目
        self.process_item(item)

        # 实时保存
        self.save_to_database(item)

    def process_item(self, item):
        """处理项目"""
        # 自定义处理逻辑
        pass

    def save_to_database(self, item):
        """保存到数据库"""
        # 保存逻辑
        pass
```

### on_dropped_item

```python
class OnDroppedItemSpider(Spider):
    name = "on_dropped_item"

    async def on_dropped_item(self, item, reason):
        """丢弃一个项目时调用"""

        self.logger.warning(f"丢弃项目: {item}")
        self.logger.warning(f"原因: {reason}")

        # 可以记录或分析被丢弃的项目
        self.track_dropped_item(item, reason)

    def track_dropped_item(self, item, reason):
        """跟踪被丢弃的项目"""
        # 分析丢弃原因
        pass
```

### on_error

```python
class OnErrorSpider(Spider):
    name = "on_error"

    def start_requests(self):
        for url in self.start_urls:
            yield Request(url, callback=self.parse, errback=self.handle_error)

    async def parse(self, response: Response):
        yield {"url": response.url}

    async def on_error(self, request, error: Exception):
        """请求发生错误时调用"""

        self.logger.error(f"请求错误: {request.url}")
        self.logger.error(f"错误类型: {type(error).__name__}")
        self.logger.error(f"错误信息: {error}")

        # 统计错误
        self.track_error(error)

    def track_error(self, error):
        """跟踪错误"""
        # 错误统计逻辑
        pass
```

### on_retry

```python
class OnRetrySpider(Spider):
    name = "on_retry"

    async def on_retry(self, request, reason):
        """请求重试时调用"""

        self.logger.info(f"重试请求: {request.url}")
        self.logger.info(f"重试原因: {reason}")

        # 记录重试次数
        retry_count = request.meta.get("retry_count", 0) + 1
        request.meta["retry_count"] = retry_count

        self.logger.info(f"重试次数: {retry_count}")

        # 超过最大重试次数则放弃
        if retry_count > 3:
            self.logger.warning("超过最大重试次数，放弃请求")
```

## 日志配置

### 设置日志级别

```python
class LogSpider(Spider):
    name = "log"

    # 设置日志级别
    logging_level = logging.INFO

    async def parse(self, response: Response):
        self.logger.debug("调试信息")  # 不会显示
        self.logger.info("信息")  # 会显示
        self.logger.warning("警告")  # 会显示
        self.logger.error("错误")  # 会显示
```

### 日志级别

- `logging.DEBUG`：调试信息
- `logging.INFO`：一般信息（默认）
- `logging.WARNING`：警告信息
- `logging.ERROR`：错误信息
- `logging.CRITICAL`：严重错误

### 日志文件

```python
class FileLogSpider(Spider):
    name = "file_log"

    # 设置日志级别
    logging_level = logging.INFO

    # 设置日志文件
    log_file = "spider.log"

    async def parse(self, response: Response):
        self.logger.info(f"处理: {response.url}")
```

### 自定义日志格式

```python
import logging

class CustomLogSpider(Spider):
    name = "custom_log"

    logging_level = logging.INFO
    log_file = "spider.log"

    async def on_start(self, resuming: bool = False):
        """自定义日志格式"""

        # 创建自定义格式
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )

        # 应用格式到文件处理器
        for handler in self.logger.handlers:
            if isinstance(handler, logging.FileHandler):
                handler.setFormatter(formatter)

        self.logger.info("自定义日志格式已设置")
```

## 完整示例

### 1. 完整的高性能爬虫

```python
import logging
from scrapling.spiders import Spider, Response, Request

class HighPerformanceSpider(Spider):
    name = "high_performance"

    start_urls = ["https://example.com"]

    # 并发控制
    concurrent_requests = 100
    per_domain_concurrent_requests = 10
    download_delay = 0.1

    # 检查点
    crawldir = "./crawl_state"

    # 日志
    logging_level = logging.INFO
    log_file = "spider.log"

    async def on_start(self, resuming: bool = False):
        """启动时初始化"""
        if resuming:
            self.logger.info("从检查点恢复")
        else:
            self.logger.info("开始新爬取")

        self.logger.info(f"并发数: {self.concurrent_requests}")
        self.logger.info(f"每域名并发: {self.per_domain_concurrent_requests}")

    def start_requests(self):
        """生成初始请求"""
        urls = [f"https://example.com/page{i}" for i in range(1, 1001)]

        for url in urls:
            yield Request(url, callback=self.parse, errback=self.handle_error)

    async def parse(self, response: Response):
        """解析响应"""
        try:
            yield {
                "url": response.url,
                "title": response.css("title::text").get(),
                "status": response.status
            }
        except Exception as e:
            self.logger.error(f"解析错误: {e}")

    async def on_scraped_item(self, item):
        """处理每个项目"""
        self.logger.info(f"已抓取: {self.stats.items_scraped}")

        # 实时保存
        self.save_item(item)

    def save_item(self, item):
        """保存项目（示例）"""
        # 这里可以保存到数据库、文件等
        pass

    async def on_error(self, request, error: Exception):
        """处理错误"""
        self.logger.error(f"请求错误: {request.url} - {error}")

    async def on_close(self):
        """关闭时清理"""
        self.logger.info("=" * 50)
        self.logger.info("爬虫统计")
        self.logger.info("=" * 50)
        self.logger.info(f"请求数: {self.stats.requests_count}")
        self.logger.info(f"成功数: {self.stats.successful_requests_count}")
        self.logger.info(f"失败数: {self.stats.failed_requests_count}")
        self.logger.info(f"抓取项: {self.stats.items_scraped}")
        self.logger.info("=" * 50)
```

### 2. 流式处理爬虫

```python
import json
from scrapling.spiders import Spider, Response, Request

class StreamProcessingSpider(Spider):
    name = "stream_processing"

    def start_requests(self):
        urls = [f"https://api.example.com/data/{i}" for i in range(1, 10001)]

        for url in urls:
            yield Request(url, callback=self.parse)

    async def parse(self, response: Response):
        try:
            data = response.json()
            yield data
        except Exception as e:
            self.logger.error(f"JSON 解析失败: {response.url}")
            yield None

# 流式处理并保存
import asyncio

async def process_stream():
    spider = StreamProcessingSpider()

    with open("output.jsonl", "w") as f:
        async for item in spider.stream():
            if item:
                # 实时保存
                f.write(json.dumps(item, ensure_ascii=False) + "\n")

            # 实时统计
            if spider.stats.items_scraped % 100 == 0:
                print(f"已处理: {spider.stats.items_scraped} 个项目")

    print(f"完成！总共处理 {spider.stats.items_scraped} 个项目")

asyncio.run(process_stream())
```

### 3. 带生命周期钩子的爬虫

```python
import logging
from scrapling.spiders import Spider, Response, Request

class LifecycleSpider(Spider):
    name = "lifecycle"

    start_urls = ["https://example.com"]

    # 日志配置
    logging_level = logging.INFO
    log_file = "lifecycle_spider.log"

    async def on_start(self, resuming: bool = False):
        """爬虫启动"""
        self.logger.info("=" * 50)
        self.logger.info("爬虫启动")
        self.logger.info("=" * 50)

        if resuming:
            self.logger.info("从检查点恢复")
        else:
            self.logger.info("开始新的爬取")

        # 初始化资源
        self.initialize_resources()

    def initialize_resources(self):
        """初始化资源"""
        self.logger.info("初始化资源")

    def start_requests(self):
        """生成请求"""
        urls = [f"https://example.com/page{i}" for i in range(1, 101)]

        for url in urls:
            yield Request(url, callback=self.parse, errback=self.handle_error)

    async def parse(self, response: Response):
        """解析响应"""
        yield {
            "url": response.url,
            "title": response.css("title::text").get()
        }

    async def on_scraped_item(self, item):
        """处理项目"""
        self.logger.info(f"处理项目: {item['url']}")

    async def on_dropped_item(self, item, reason):
        """处理丢弃的项目"""
        self.logger.warning(f"丢弃项目: {item}, 原因: {reason}")

    async def on_error(self, request, error: Exception):
        """处理错误"""
        self.logger.error(f"请求错误: {request.url} - {error}")

    async def on_close(self):
        """爬虫关闭"""
        self.logger.info("=" * 50)
        self.logger.info("爬虫关闭")
        self.logger.info("=" * 50)

        # 打印统计
        self.log_statistics()

        # 清理资源
        self.cleanup_resources()

    def log_statistics(self):
        """记录统计信息"""
        stats = self.stats
        self.logger.info(f"请求数: {stats.requests_count}")
        self.logger.info(f"成功数: {stats.successful_requests_count}")
        self.logger.info(f"失败数: {stats.failed_requests_count}")
        self.logger.info(f"抓取项: {stats.items_scraped}")
        self.logger.info(f"丢弃项: {stats.items_dropped}")

    def cleanup_resources(self):
        """清理资源"""
        self.logger.info("清理资源")
```

## 最佳实践

### 1. 合理设置并发

```python
# 对于友好爬取
class PoliteSpider(Spider):
    concurrent_requests = 10
    per_domain_concurrent_requests = 2
    download_delay = 1.0

# 对于高速爬取
class FastSpider(Spider):
    concurrent_requests = 200
    per_domain_concurrent_requests = 20
    download_delay = 0.05
```

### 2. 始终启用检查点

```python
class RobustSpider(Spider):
    # 始终设置检查点目录
    crawldir = "./crawl_state"
```

### 3. 使用流式处理大规模数据

```python
# 对于大规模数据爬取
async for item in spider.stream():
    # 实时处理，避免内存问题
    process_item(item)
```

### 4. 充分利用生命周期钩子

```python
class WellStructuredSpider(Spider):
    async def on_start(self, resuming=False):
        # 初始化资源
        pass

    async def on_scraped_item(self, item):
        # 实时处理
        pass

    async def on_close(self):
        # 清理资源
        pass
```

### 5. 合理配置日志

```python
class LoggedSpider(Spider):
    logging_level = logging.INFO  # 生产环境使用 INFO
    log_file = "spider.log"  # 保存日志文件

    # 开发环境可以设置 DEBUG
    # logging_level = logging.DEBUG
```

## 总结

Scrapling 的高级特性提供了强大的功能：

1. **并发控制**：全局并发和每域名并发
2. **暂停/恢复**：检查点系统支持断点续爬
3. **流式处理**：实时处理爬取数据
4. **生命周期钩子**：在爬虫不同阶段执行自定义逻辑
5. **日志配置**：灵活的日志级别和文件配置

这些高级特性使得 Scrapling 能够处理各种复杂的爬取场景，从小型项目到大规模数据采集都能轻松应对。

---

# AI MCP 服务器与隐身抓取 (AI MCP Server & Stealth Scraping)

## 概述

Scrapling 的 AI MCP (Model Context Protocol) 服务器提供了一个强大的接口，允许将 Scrapling 与 AI 工具集成，实现无代码抓取。通过 MCP 服务器，AI 助手可以直接控制 Scrapling 的各种抓取器，包括隐身抓取功能。

## MCP 服务器概述

### 基本概念

MCP 服务器是一个 HTTP API 服务，它封装了 Scrapling 的所有抓取功能，允许通过简单的 API 调用执行抓取任务。这使得 AI 助手（如 ChatGPT、Claude 等）能够自然地理解和执行网页抓取请求。

### 主要功能

1. **无代码抓取**：通过自然语言描述抓取需求
2. **AI 友好接口**：专为 AI 设计的简洁 API
3. **隐身抓取**：自动绕过 Cloudflare 等反爬保护
4. **灵活配置**：支持各种抓取器配置选项
5. **实时处理**：即时返回抓取结果

## 启动 MCP 服务器

### 基本启动

```bash
# 启动 MCP 服务器
scrapling mcp-server

# 指定主机和端口
scrapling mcp-server --host 0.0.0.0 --port 8000

# 查看帮助
scrapling mcp-server --help
```

### 启动选项

```bash
# 基本选项
--host HOST          # 服务器主机（默认: 127.0.0.1）
--port PORT          # 服务器端口（默认: 8000）
--workers WORKERS    # 工作进程数（默认: 1）

# 隐身选项
--stealth            # 启用隐身模式
--solve-cloudflare   # 自动解决 Cloudflare 挑战
--block-webrtc       # 阻止 WebRTC 泄漏
--hide-canvas        # 隐藏 Canvas 指纹

# 浏览器选项
--real-chrome        # 使用真实 Chrome 浏览器
--headless           # 无头模式（默认: True）
--network-idle       # 等待网络空闲

# 代理选项
--proxy URL          # 设置代理

# 日志选项
--log-level LEVEL    # 日志级别（DEBUG/INFO/WARNING/ERROR）
--log-file FILE      # 日志文件路径
```

### 完整启动示例

```bash
# 启动带有隐身功能的 MCP 服务器
scrapling mcp-server \
  --host 0.0.0.0 \
  --port 8000 \
  --stealth \
  --solve-cloudflare \
  --block-webrtc \
  --hide-canvas \
  --real-chrome \
  --workers 4 \
  --log-level INFO
```

## API 端点

### /health

健康检查端点。

```bash
curl http://localhost:8000/health
```

**响应：**
```json
{
  "status": "ok",
  "version": "1.0.0"
}
```

### /extract

主要的数据提取端点。

**请求：**
```bash
curl -X POST http://localhost:8000/extract \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://example.com",
    "prompt": "提取所有产品名称和价格"
  }'
```

**请求参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `url` | `string` | 是 | 目标 URL |
| `prompt` | `string` | 是 | 抓取需求描述 |
| `stealth` | `bool` | 否 | 是否启用隐身模式 |
| `solve_cloudflare` | `bool` | 否 | 是否解决 Cloudflare |
| `block_webrtc` | `bool` | 否 | 是否阻止 WebRTC |
| `hide_canvas` | `bool` | 否 | 是否隐藏 Canvas |
| `real_chrome` | `bool` | 否 | 是否使用真实 Chrome |
| `proxy` | `string` | 否 | 代理 URL |
| `timeout` | `int` | 否 | 超时时间（秒） |

**响应：**
```json
{
  "success": true,
  "data": [
    {
      "product": "产品 A",
      "price": "$99.99"
    },
    {
      "product": "产品 B",
      "price": "$149.99"
    }
  ],
  "metadata": {
    "url": "https://example.com",
    "status": 200,
    "elapsed": 2.5
  }
}
```

### /stealth-scrape

专门的隐身抓取端点，自动启用所有隐身功能。

**请求：**
```bash
curl -X POST http://localhost:8000/stealth-scrape \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://cloudflare-protected.com",
    "prompt": "提取页面主要内容"
  }'
```

**响应：**
```json
{
  "success": true,
  "data": {
    "content": "页面主要内容...",
    "title": "页面标题"
  },
  "metadata": {
    "url": "https://cloudflare-protected.com",
    "status": 200,
    "elapsed": 5.2,
    "cloudflare_solved": true
  }
}
```

## 隐身抓取

### 基本隐身抓取

```bash
curl -X POST http://localhost:8000/extract \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://protected-site.com",
    "prompt": "提取所有文章标题和链接",
    "stealth": true
  }'
```

### Cloudflare 绕过

```bash
curl -X POST http://localhost:8000/stealth-scrape \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://cloudflare-protected.com",
    "prompt": "提取页面数据",
    "solve_cloudflare": true
  }'
```

### 完整隐身配置

```bash
curl -X POST http://localhost:8000/extract \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://strictly-protected.com",
    "prompt": "提取所有产品信息",
    "stealth": true,
    "solve_cloudflare": true,
    "block_webrtc": true,
    "hide_canvas": true,
    "real_chrome": true
  }'
```

### 使用代理

```bash
curl -X POST http://localhost:8000/extract \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://example.com",
    "prompt": "提取页面内容",
    "proxy": "http://proxy.example.com:8080"
  }'
```

## AI 集成示例

### ChatGPT 集成

通过 MCP 服务器，ChatGPT 可以直接执行抓取任务：

```
用户：请帮我抓取 https://example.com 上的所有产品名称和价格

ChatGPT：我来帮你抓取这个网站的数据...

[ChatGPT 调用 MCP 服务器 API]

ChatGPT：我已经成功抓取了该网站的数据。以下是产品列表：

1. 产品 A - $99.99
2. 产品 B - $149.99
3. 产品 C - $199.99

共抓取到 3 个产品。
```

### 自然语言抓取

用户可以用自然语言描述抓取需求：

```bash
# 提取新闻标题
curl -X POST http://localhost:8000/extract \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://news.example.com",
    "prompt": "提取所有新闻标题和发布时间"
  }'

# 提取产品信息
curl -X POST http://localhost:8000/extract \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://shop.example.com",
    "prompt": "提取产品名称、价格、描述和图片链接"
  }'

# 提取社交媒体帖子
curl -X POST http://localhost:8000/extract \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://social.example.com",
    "prompt": "提取帖子内容、作者、发布时间和点赞数"
  }'
```

## 高级用法

### 批量抓取

```python
import requests

urls = [
    "https://example.com/page1",
    "https://example.com/page2",
    "https://example.com/page3"
]

api_url = "http://localhost:8000/extract"

for url in urls:
    response = requests.post(
        api_url,
        json={
            "url": url,
            "prompt": "提取页面标题和主要内容",
            "stealth": True
        }
    )

    result = response.json()
    print(f"URL: {url}")
    print(f"数据: {result['data']}")
    print("-" * 50)
```

### 自定义配置

```python
import requests

# 针对不同网站使用不同配置
configs = {
    "normal": {
        "stealth": False
    },
    "protected": {
        "stealth": True,
        "solve_cloudflare": True
    },
    "strict": {
        "stealth": true,
        "solve_cloudflare": true,
        "block_webrtc": true,
        "hide_canvas": true,
        "real_chrome": true
    }
}

url = "https://protected-site.com"
config = configs["protected"]

response = requests.post(
    "http://localhost:8000/extract",
    json={
        "url": url,
        "prompt": "提取页面数据",
        **config
    }
)

result = response.json()
print(result)
```

### 错误处理

```python
import requests

def safe_scrape(url, prompt, retries=3):
    """带重试的抓取函数"""

    for i in range(retries):
        try:
            response = requests.post(
                "http://localhost:8000/extract",
                json={
                    "url": url,
                    "prompt": prompt,
                    "stealth": True
                },
                timeout=30
            )

            result = response.json()

            if result.get("success"):
                return result["data"]
            else:
                print(f"请求失败: {result.get('error')}")

        except requests.exceptions.Timeout:
            print(f"超时，重试 {i+1}/{retries}")
        except Exception as e:
            print(f"错误: {e}")

    return None

# 使用
data = safe_scrape("https://example.com", "提取标题")
if data:
    print("成功抓取:", data)
else:
    print("抓取失败")
```

## 实用场景

### 1. 受 Cloudflare 保护的网站

```bash
curl -X POST http://localhost:8000/stealth-scrape \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://cloudflare-protected-site.com",
    "prompt": "提取所有产品信息",
    "solve_cloudflare": true,
    "real_chrome": true
  }'
```

### 2. 电商网站数据提取

```bash
curl -X POST http://localhost:8000/extract \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://shop.example.com/products",
    "prompt": "提取产品名称、价格、描述、评分和图片链接"
  }'
```

### 3. 新闻网站抓取

```bash
curl -X POST http://localhost:8000/extract \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://news.example.com",
    "prompt": "提取所有新闻文章的标题、链接、作者和发布时间"
  }'
```

### 4. 社交媒体数据

```bash
curl -X POST http://localhost:8000/stealth-scrape \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://social.example.com/feed",
    "prompt": "提取帖子内容、作者、发布时间、点赞数和评论数",
    "stealth": true,
    "block_webrtc": true
  }'
```

### 5. API 响应提取

```bash
curl -X POST http://localhost:8000/extract \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://api.example.com/data",
    "prompt": "提取 API 响应中的关键数据"
  }'
```

## 性能优化

### 并发请求

```python
import asyncio
import aiohttp

async def scrape_multiple(urls):
    """并发抓取多个 URL"""

    async with aiohttp.ClientSession() as session:
        tasks = []

        for url in urls:
            task = session.post(
                "http://localhost:8000/extract",
                json={
                    "url": url,
                    "prompt": "提取页面标题",
                    "stealth": True
                }
            )
            tasks.append(task)

        responses = await asyncio.gather(*tasks)

        for response in responses:
            result = await response.json()
            print(result)

# 使用
urls = [f"https://example.com/page{i}" for i in range(1, 11)]
asyncio.run(scrape_multiple(urls))
```

### 缓存结果

```python
import requests
import json
from hashlib import md5

class CachedScraper:
    """带缓存的抓取器"""

    def __init__(self):
        self.cache_file = "scrape_cache.json"
        self.cache = self.load_cache()

    def load_cache(self):
        """加载缓存"""
        try:
            with open(self.cache_file, "r") as f:
                return json.load(f)
        except:
            return {}

    def save_cache(self):
        """保存缓存"""
        with open(self.cache_file, "w") as f:
            json.dump(self.cache, f)

    def get_cache_key(self, url, prompt):
        """生成缓存键"""
        key = f"{url}:{prompt}"
        return md5(key.encode()).hexdigest()

    def scrape(self, url, prompt, use_cache=True):
        """抓取（带缓存）"""

        cache_key = self.get_cache_key(url, prompt)

        # 检查缓存
        if use_cache and cache_key in self.cache:
            print("从缓存返回")
            return self.cache[cache_key]

        # 实际抓取
        response = requests.post(
            "http://localhost:8000/extract",
            json={
                "url": url,
                "prompt": prompt,
                "stealth": True
            }
        )

        result = response.json()

        if result.get("success"):
            # 保存到缓存
            self.cache[cache_key] = result["data"]
            self.save_cache()

            return result["data"]
        else:
            return None

# 使用
scraper = CachedScraper()
data = scraper.scrape("https://example.com", "提取标题")
print(data)
```

## 监控和日志

### 服务器日志

```bash
# 查看实时日志
scrapling mcp-server --log-file mcp.log --log-level DEBUG
```

### 请求统计

```python
import time
import requests

class MonitoredScraper:
    """带监控的抓取器"""

    def __init__(self):
        self.stats = {
            "total_requests": 0,
            "successful": 0,
            "failed": 0,
            "total_time": 0
        }

    def scrape(self, url, prompt, **kwargs):
        """抓取（带监控）"""

        start_time = time.time()
        self.stats["total_requests"] += 1

        try:
            response = requests.post(
                "http://localhost:8000/extract",
                json={
                    "url": url,
                    "prompt": prompt,
                    **kwargs
                }
            )

            elapsed = time.time() - start_time
            self.stats["total_time"] += elapsed

            result = response.json()

            if result.get("success"):
                self.stats["successful"] += 1
                print(f"成功: {url} - {elapsed:.2f}s")
                return result["data"]
            else:
                self.stats["failed"] += 1
                print(f"失败: {url} - {result.get('error')}")
                return None

        except Exception as e:
            self.stats["failed"] += 1
            print(f"错误: {url} - {e}")
            return None

    def print_stats(self):
        """打印统计信息"""
        print("\n" + "=" * 50)
        print("抓取统计")
        print("=" * 50)
        print(f"总请求数: {self.stats['total_requests']}")
        print(f"成功: {self.stats['successful']}")
        print(f"失败: {self.stats['failed']}")
        if self.stats['total_requests'] > 0:
            print(f"成功率: {self.stats['successful']/self.stats['total_requests']*100:.1f}%")
            print(f"平均耗时: {self.stats['total_time']/self.stats['successful']:.2f}s")
        print("=" * 50 + "\n")

# 使用
scraper = MonitoredScraper()

urls = [f"https://example.com/page{i}" for i in range(1, 11)]

for url in urls:
    scraper.scrape(url, "提取标题")

scraper.print_stats()
```

## 最佳实践

### 1. 选择合适的抓取器

```python
# 对于正常网站
{
  "url": "https://example.com",
  "prompt": "提取数据"
}

# 对于受保护的网站
{
  "url": "https://protected.com",
  "prompt": "提取数据",
  "stealth": True
}

# 对于严格保护的网站
{
  "url": "https://strict.com",
  "prompt": "提取数据",
  "stealth": True,
  "solve_cloudflare": True,
  "block_webrtc": True,
  "hide_canvas": True
}
```

### 2. 编写清晰的提示词

```python
# 好的提示词
{
  "prompt": "提取产品名称、价格（带货币符号）和评分（1-5星）"
}

# 不好的提示词
{
  "prompt": "提取数据"
}
```

### 3. 错误处理和重试

```python
# 实现重试机制
max_retries = 3

for i in range(max_retries):
    result = scrape(url, prompt)

    if result:
        break
    else:
        print(f"重试 {i+1}/{max_retries}")
```

### 4. 监控和日志

```python
# 记录所有请求和响应
log_request(url, prompt)
result = scrape(url, prompt)
log_result(result)
```

### 5. 性能优化

```python
# 使用并发请求提高效率
async def batch_scrape(urls):
    tasks = [scrape(url, prompt) for url in urls]
    results = await asyncio.gather(*tasks)
    return results
```

## 总结

Scrapling AI MCP 服务器提供了一个强大的接口，使得 AI 助手能够轻松地执行网页抓取任务：

1. **无代码抓取**：通过自然语言描述抓取需求
2. **AI 友好**：专为 AI 设计的简洁 API
3. **隐身抓取**：自动绕过各种反爬保护
4. **灵活配置**：支持各种抓取器和配置选项
5. **易于集成**：简单的 REST API，易于与各种 AI 工具集成

通过 MCP 服务器，即使是没有任何编程经验的用户，也能通过 AI 助手轻松地抓取网页数据。这使得数据抓取变得更加普及和易用。

---

# Selector 类 API 参考 (Selector API Reference)

## 概述

`Selector` 类是 Scrapling 的核心解析引擎，提供 HTML 解析和元素选择功能。该类是对 lxml.html.HtmlElement 的扩展，提供了更简洁的接口，同时解决了 lxml 对象不可序列化的问题。

## 导入方式

```python
from scrapling.parser import Selector
```

## 类定义

```python
Selector(
    content=None,
    url="",
    encoding="utf-8",
    huge_tree=True,
    root=None,
    keep_comments=False,
    keep_cdata=False,
    adaptive=False,
    storage=SQLiteStorageSystem,
    storage_args=None
)
```

**基类**：`SelectorsGeneration`

## 构造函数参数

| 参数 | 类型 | 默认值 | 说明 |
|------|------|---------|------|
| `content` | `Optional[str \| bytes]` | `None` | HTML 内容，可以是字符串或字节 |
| `url` | `str` | `''` | 与 HTML 数据一起存储的 URL，用于后续检索 |
| `encoding` | `str` | `'utf-8'` | HTML 解析使用的编码类型 |
| `huge_tree` | `bool` | `True` | 是否启用大树模式，解析大型 HTML 文档时应始终启用 |
| `root` | `Optional[HtmlElement]` | `None` | 内部使用的 etree 对象，优先级最高，仅在了解后果时使用 |
| `keep_comments` | `bool` | `False` | 解析时是否保留 HTML 注释 |
| `keep_cdata` | `bool` | `False` | 解析时是否保留 CDATA 部分 |
| `adaptive` | `bool` | `False` | 全局关闭自适应功能，优先级高于所有自适应相关参数 |
| `storage` | `Any` | `SQLiteStorageSystem` | 用于自适应功能的存储类 |
| `storage_args` | `Optional[Dict]` | `None` | 传递给存储类的参数字典 |

## 类属性

### __slots__

定义类的插槽，用于内存优化。

```python
__slots__ = ()
```

## 实例属性

### url

与 HTML 数据一起存储的 URL。

```python
selector = Selector(html, url="https://example.com")
print(selector.url)  # "https://example.com"
```

### encoding

HTML 解析使用的编码类型。

```python
print(selector.encoding)  # "utf-8"
```

### tag

元素的标签名。

```python
element = page.css("h1").get()
print(element.tag)  # "h1"
```

### text

元素的直接文本内容。

```python
element = page.css("h1").get()
print(element.text)  # 标题文本
```

### attrib

元素的属性字典，支持字典式访问。

```python
element = page.css("a").get()
print(element.attrib)  # {'href': '...', 'class': '...'}
print(element['class'])  # '...'
```

### html_content

元素的 HTML 内容。

```python
element = page.css("div").get()
print(element.html_content)  # "<div>...</div>"
```

### body

元素的 body 内容（bytes 格式）。

```python
print(element.body)
```

### parent

父元素。

```python
element = page.css("span").get()
parent = element.parent
print(parent.tag)  # 父元素的标签
```

### below_elements

当前元素下方的所有元素。

### children

子元素列表。

```python
element = page.css("ul").get()
for child in element.children:
    print(child.tag)
```

### siblings

兄弟元素列表。

```python
element = page.css("li").get()
for sibling in element.siblings:
    print(sibling.text)
```

### path

元素在 DOM 树中的路径。

```python
print(element.path)
```

### next

下一个兄弟元素。

```python
element = page.css("li").get()
next_elem = element.next
```

### previous

上一个兄弟元素。

```python
element = page.css("li").get()
prev_elem = element.previous
```

## 实例方法

### extract()

提取所有匹配元素的文本内容（别名：`getall()`）。

```python
page.css("a::text").extract()
# 或
page.css("a::text").getall()
```

### extract_first()

提取第一个匹配元素的文本内容（别名：`get()`）。

```python
page.css("h1::text").extract_first()
# 或
page.css("h1::text").get()
```

### generate_css_selector()

为元素生成 CSS 选择器。

```python
element = page.css("div.product").get()
selector = element.generate_css_selector()
print(selector)  # ".product" 或更具体的选择器
```

### generate_full_css_selector()

生成完整的 CSS 选择器（从根元素开始）。

```python
selector = element.generate_full_css_selector()
print(selector)  # "html > body > div.container > div.product"
```

### generate_xpath_selector()

为元素生成 XPath 选择器。

```python
selector = element.generate_xpath_selector()
```

### generate_full_xpath_selector()

生成完整的 XPath 选择器。

```python
selector = element.generate_full_xpath_selector()
```

### __getitem__(key)

通过索引或切片访问子元素。

```python
# 索引访问
first = page.css("li")[0]

# 切片访问
first_three = page.css("li")[0:3]
```

### __contains__(element)

检查是否包含特定元素或文本。

```python
element = page.css("div").get()

# 检查文本
if "content" in element:
    print("包含该文本")

# 检查元素
if child_elem in element:
    print("包含该子元素")
```

### __getstate__()

获取对象状态（用于序列化）。

```python
state = element.__getstate__()
```

### get_all_text()

递归获取所有文本内容。

```python
# 获取所有文本，包括子元素的文本
text = element.get_all_text(separator="\n", strip=False)

# 参数：
# - separator: 文本分隔符，默认 "\n"
# - strip: 是否去除空白，默认 False
# - ignore_tags: 忽略的标签，默认 ("script", "style")
```

### urljoin()

将相对 URL 拼接为绝对 URL。

```python
element = page.css("a").get()
absolute_url = element.urljoin(element.attrib["href"])
```

### prettify()

格式化输出 HTML。

```python
print(element.prettify())
```

### has_class(class_name)

检查元素是否包含指定的 CSS 类。

```python
element = page.css("div").get()
if element.has_class("active"):
    print("元素有 active 类")
```

### iterancestors()

迭代所有祖先元素。

```python
for ancestor in element.iterancestors():
    print(ancestor.tag)
```

### find_ancestor(func)

查找满足条件的祖先元素。

```python
# 查找标签为 div 的祖先
div_ancestor = element.find_ancestor(lambda e: e.tag == "div")
```

### get()

获取第一个匹配的结果。

```python
# 获取文本
title = page.css("h1::text").get()

# 获取属性
href = page.css("a::attr(href)").get()
```

### getall()

获取所有匹配的结果。

```python
# 获取所有链接
links = page.css("a::attr(href)").getall()

# 获取所有文本
texts = page.css("p::text").getall()
```

### __str__()

字符串表示。

```python
print(str(element))
```

### __repr__()

对象的字符串表示（用于调试）。

```python
print(repr(element))
```

### relocate()

重新定位元素（用于自适应抓取）。

```python
# 保存元素属性
element.save("my_element")

# 后续重新定位
relocated = page.relocate(page.retrieve("my_element"))
```

### css(selector)

使用 CSS 选择器查找元素。

```python
# 基本选择器
elements = page.css("div.product")

# 伪元素
text = page.css("h1::text").get()
href = page.css("a::attr(href)").get()

# 属性选择器
elements = page.css("[data-id]")

# 组合选择器
elements = page.css("div.container > ul > li")
```

### xpath(expression)

使用 XPath 表达式查找元素。

```python
# 基本表达式
elements = page.xpath("//div[@class='product']")

# 提取文本
text = page.xpath("//h1/text()").get()

# 提取属性
href = page.xpath("//a/@href").get()
```

### find_all(filters, **kwargs)

使用多种过滤条件查找元素。

```python
# 按标签名
elements = page.find_all("div")

# 按属性
elements = page.find_all(class_="product")

# 使用正则表达式
import re
elements = page.find_all(text=re.compile(r"price"))

# 使用函数
elements = page.find_all(lambda e: "price" in e.text.lower())

# 组合条件
elements = page.find_all("div", class_="product", id=re.compile(r"\d+"))
```

### find(filters, **kwargs)

查找第一个匹配的元素（参数与 `find_all` 相同）。

```python
element = page.find("div", class_="product")
```

### save(element, identifier)

保存元素属性（用于自适应抓取）。

```python
element = page.css("div.product").get()
page.save(element, identifier="product")
```

### retrieve(identifier)

检索保存的元素属性。

```python
stored = page.retrieve("product")
```

### json()

解析 JSON 格式的文本。

```python
# 提取并解析 JSON
json_data = page.css("script[type='application/json']::text").get().json()
```

### re(pattern, **kwargs)

使用正则表达式提取文本。

```python
# 提取所有匹配
emails = page.css("div.contact").get().re(r"[\w.]+@[\w.]+")

# 使用标志
matches = page.text.re(r"price:\s*(\d+)", re.IGNORECASE)
```

### re_first(pattern, **kwargs)

使用正则表达式提取第一个匹配。

```python
price = page.css("div.price").get().re_first(r"\d+")
```

### find_similar(similarity_threshold=0.2, ignore_attributes=('href', 'src'))

查找与当前元素相似的元素。

```python
element = page.css("div.product").get()
similar_elements = element.find_similar()

# 调整相似度阈值
similar = element.find_similar(similarity_threshold=0.3)

# 忽略特定属性
similar = element.find_similar(ignore_attributes=('href', 'src'))
```

### find_by_text(text, partial=False, case_sensitive=False, clean_match=True)

按文本内容查找元素。

```python
# 精确匹配
element = page.find_by_text("Submit")

# 部分匹配
element = page.find_by_text("Submit", partial=True)

# 不区分大小写
element = page.find_by_text("submit", case_sensitive=False)

# 清洁匹配（去除空白等）
element = page.find_by_text("Submit", clean_match=True)
```

### find_by_regex(pattern, first_match=True)

按正则表达式匹配文本查找元素。

```python
# 查找第一个匹配
element = page.find_by_regex(r"price:\s*\d+")

# 查找所有匹配
elements = page.find_by_regex(r"price:\s*\d+", first_match=False)
```

## Selectors 类

`Selectors` 是 `Selector` 对象的列表，继承自 Python `List`。

### 属性

### __slots__

```python
__slots__ = ()
```

### 方法

### extract()

提取所有元素的文本内容。

```python
selectors = page.css("div.product")
texts = selectors.extract()
```

### extract_first()

提取第一个元素的文本内容。

```python
text = page.css("div.product").extract_first()
```

### first()

获取第一个元素。

```python
first_element = page.css("div.product").first()
```

### last()

获取最后一个元素。

```python
last_element = page.css("div.product").last()
```

### length

获取元素数量。

```python
count = page.css("div.product").length
```

### __getitem__(index)

通过索引访问元素。

```python
first = page.css("div.product")[0]
last = page.css("div.product")[-1]
```

### xpath(expression)

对列表中的所有元素应用 XPath。

```python
elements = page.css("div")
links = elements.xpath(".//a")
```

### css(selector)

对列表中的所有元素应用 CSS 选择器。

```python
products = page.css("div.product")
titles = products.css("h2::text")
```

### re(pattern, **kwargs)

对列表中的所有元素应用正则表达式。

```python
elements = page.css("div.price")
prices = elements.re(r"\d+")
```

### filter(func)

使用函数过滤元素。

```python
elements = page.css("div.product")
active_products = elements.filter(lambda e: "active" in e.attrib.get("class", ""))
```

### get()

获取第一个结果。

```python
element = page.css("div.product").get()
```

### getall()

获取所有结果。

```python
elements = page.css("div.product").getall()
```

### __getstate__()

获取对象状态。

## 使用示例

### 基本解析

```python
from scrapling.parser import Selector

html = """
<html>
    <body>
        <h1>欢迎</h1>
        <div class="product">
            <h2>产品 A</h2>
            <p class="price">100 元</p>
        </div>
    </body>
</html>
"""

page = Selector(html)

# 提取标题
title = page.css("h1::text").get()
print(title)  # "欢迎"

# 提取产品信息
product = page.css("div.product").get()
print(product.css("h2::text").get())  # "产品 A"
print(product.css("p.price::text").get())  # "100 元"
```

### 链式调用

```python
# Selectors 支持链式调用
titles = page.css("div.product").css("h2::text").getall()
print(titles)  # ["产品 A", "产品 B", ...]
```

### 元素遍历

```python
# 遍历子元素
container = page.css("div.container").get()
for child in container.children:
    print(child.tag)

# 遍历祖先
element = page.css("span").get()
for ancestor in element.iterancestors():
    print(ancestor.tag)
```

### 属性访问

```python
# 字典式访问
link = page.css("a").get()
print(link.attrib["href"])
print(link["href"])

# 使用 CSS 选择器
href = page.css("a::attr(href)").get()
```

### 相似元素查找

```python
# 查找相似元素
product = page.css("div.product").get()
similar_products = product.find_similar()

for similar in similar_products:
    print(similar.css("h2::text").get())
```

### 正则表达式

```python
# 提取价格
prices = page.re(r"price:\s*(\d+)")
print(prices)  # ["100", "200", "300"]

# 在元素上使用
element = page.css("div.price").get()
price = element.re_first(r"\d+")
print(price)  # "100"
```

## 最佳实践

### 1. 使用 CSS 选择器（推荐）

```python
# 推荐：CSS 选择器更简洁
elements = page.css("div.product")
text = page.css("h1::text").get()

# 可以使用，但 XPath 更复杂
elements = page.xpath("//div[@class='product']")
text = page.xpath("//h1/text()").get()
```

### 2. 链式调用

```python
# 利用 Selectors 的链式调用
titles = page.css("div.product").css("h2::text").getall()
```

### 3. 错误处理

```python
# 使用 get() 的默认值
title = page.css("h1::text").get("默认标题")

# 检查是否存在
if page.css("div.product"):
    # 处理产品
    pass
```

### 4. 性能优化

```python
# 避免重复查询
container = page.css("div.container").get()
# 在容器内查找，而不是每次都在整个页面查找
titles = container.css("h2::text").getall()
```

### 5. 内存管理

```python
# 对于大文档，启用 huge_tree
page = Selector(large_html, huge_tree=True)
```

## 注意事项

1. **不可序列化**：lxml 原生对象不可序列化，Selector 通过包装解决了这个问题
2. **huge_tree**：对于大型 HTML 文档，务必启用 `huge_tree=True`
3. **属性访问**：`attrib` 属性提供了字典式的属性访问方式
4. **文本处理**：`text` 只返回直接文本，使用 `get_all_text()` 获取所有文本
5. **自适应功能**：需要启用 `adaptive=True` 并配置存储系统

## 总结

`Selector` 类提供了强大而灵活的 HTML 解析和元素选择功能：

1. **多种选择器**：CSS、XPath、文本匹配、正则表达式
2. **DOM 遍历**：父元素、子元素、兄弟元素、祖先元素
3. **相似性查找**：`find_similar()` 方法
4. **链式调用**：Selectors 支持流畅的链式操作
5. **自适应支持**：可选的自适应抓取功能
6. **属性处理**：便捷的属性访问和检查方法

这是 Scrapling 解析系统的核心类，是所有数据提取任务的基础。

---

# Fetchers 类 API 参考 (Fetchers API Reference)

## 概述

Scrapling 提供了多种抓取器(Fetcher)类，用于执行不同类型的 HTTP 请求和网页抓取任务。所有抓取器都支持 GET、POST、PUT 和 DELETE 方法，并提供浏览器指纹模拟、代理支持、会话管理等高级功能。

## 导入方式

```python
from scrapling.fetchers import (
    Fetcher, AsyncFetcher, StealthyFetcher, DynamicFetcher,
    FetcherSession, AsyncStealthySession, StealthySession, DynamicSession, AsyncDynamicSession
)
```

## Fetcher 类

### 基本介绍

`Fetcher` 是一个基础的抓取器类，能够基于 `curl_cffi` 执行基本的 GET、POST、PUT 和 DELETE HTTP 请求。适合用于低到中等防护级别的网站。

**基类**：`BaseFetcher`

### 类属性

#### huge_tree

```python
huge_tree = True
```

是否启用大型文档解析功能，默认为 True。对于大型 HTML 文档应始终启用。

#### adaptive

```python
adaptive = False
```

全局启用/禁用自适应抓取功能，默认为 False。

#### storage

```python
storage = SQLiteStorageSystem
```

用于自适应功能的存储类，默认为 SQLiteStorageSystem。

#### keep_cdata

```python
keep_cdata = False
```

解析 HTML 时是否保留 CDATA 部分，默认为 False。

#### storage_args

```python
storage_args = None
```

传递给存储类的参数字典。

#### keep_comments

```python
keep_comments = False
```

解析 HTML 时是否保留注释，默认为 False。

#### adaptive_domain

```python
adaptive_domain = ''
```

自适应抓取的域名设置。

#### parser_keywords

```python
parser_keywords = (
    "huge_tree",
    "adaptive",
    "storage",
    "keep_cdata",
    "storage_args",
    "keep_comments",
    "adaptive_domain",
)
```

解析器参数的关键字列表。

### 类方法

#### display_config()

显示当前解析器配置。

```python
@classmethod
def display_config(cls) -> dict:
    return dict(
        huge_tree=cls.huge_tree,
        keep_comments=cls.keep_comments,
        keep_cdata=cls.keep_cdata,
        adaptive=cls.adaptive,
        storage=cls.storage,
        storage_args=cls.storage_args,
        adaptive_domain=cls.adaptive_domain,
    )
```

**返回值**：包含当前配置的字典

**示例**：
```python
config = Fetcher.display_config()
print(config)
```

#### configure(**kwargs)

全局设置解析器的多个参数。

**参数说明**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|---------|------|
| `huge_tree` | bool | True | 启用大型文档解析 |
| `keep_comments` | bool | False | 保留 HTML 注释 |
| `keep_cdata` | bool | False | 保留 CDATA 部分 |
| `adaptive` | bool | False | 启用自适应抓取 |
| `storage` | Any | SQLiteStorageSystem | 存储类 |
| `storage_args` | dict | None | 存储类参数 |
| `adaptive_domain` | str | '' | 自适应域名 |

**示例**：
```python
# 全局配置 Fetcher
Fetcher.configure(
    huge_tree=True,
    keep_comments=True,
    adaptive=True
)
```

### 构造函数

```python
Fetcher(*args, **kwargs)
```

**注意**：在 v0.2.99 之前的版本中，构造函数可以接受参数，但现在已弃用。应在抓取前使用 `Fetcher.configure()` 方法进行配置。

### HTTP 方法

Fetcher 提供以下 HTTP 方法：

#### get(url, **kwargs)

执行 GET 请求。

**常用参数**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|---------|------|
| `url` | str | - | 请求的 URL |
| `impersonate` | str | 'chrome' | 浏览器指纹模拟 |
| `headers` | dict | None | 请求头 |
| `cookies` | dict | None | Cookies |
| `params` | dict | None | URL 查询参数 |
| `timeout` | int | 30 | 超时时间（秒） |
| `proxy` | str/dict | None | 代理设置 |
| `follow_redirects` | bool | True | 是否跟随重定向 |
| `max_redirects` | int | 30 | 最大重定向次数 |
| `retries` | int | 3 | 重试次数 |
| `retry_delay` | int | 1 | 重试延迟（秒） |
| `stealthy_headers` | bool | True | 使用真实浏览器头 |
| `auth` | dict | None | HTTP 基本认证 |
| `verify` | bool | True | 验证 HTTPS 证书 |
| `http3` | bool | False | 使用 HTTP/3 |

**返回值**：`Response` 对象

**示例**：
```python
from scrapling.fetchers import Fetcher

# 基本 GET 请求
response = Fetcher.get("https://example.com")

# 带参数的请求
response = Fetcher.get(
    "https://api.example.com/data",
    params={"page": 1, "limit": 10},
    headers={"Authorization": "Bearer token"}
)

# 使用代理
response = Fetcher.get(
    "https://example.com",
    proxy="http://proxy:8080"
)
```

#### post(url, **kwargs)

执行 POST 请求。

**额外参数**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|---------|------|
| `data` | dict/str | None | 请求体数据 |
| `json` | dict | None | JSON 数据 |
| `files` | dict | None | 文件上传 |

**示例**：
```python
# POST JSON 数据
response = Fetcher.post(
    "https://api.example.com/submit",
    json={"name": "John", "age": 30}
)

# POST 表单数据
response = Fetcher.post(
    "https://example.com/form",
    data={"username": "admin", "password": "secret"}
)

# 上传文件
response = Fetcher.post(
    "https://example.com/upload",
    files={"file": open("document.pdf", "rb")}
)
```

#### put(url, **kwargs)

执行 PUT 请求，参数与 POST 相同。

```python
response = Fetcher.put(
    "https://api.example.com/update/123",
    json={"status": "completed"}
)
```

#### delete(url, **kwargs)

执行 DELETE 请求。

```python
response = Fetcher.delete(
    "https://api.example.com/resource/123",
    headers={"Authorization": "Bearer token"}
)
```

## AsyncFetcher 类

### 基本介绍

`AsyncFetcher` 是 `Fetcher` 的异步版本，提供相同的 HTTP 请求功能，但使用异步 API，适合高并发场景。

**基类**：`BaseFetcher`

### 类属性

与 `Fetcher` 相同：
- `huge_tree = True`
- `adaptive = False`
- `storage = SQLiteStorageSystem`
- `keep_cdata = False`
- `storage_args = None`
- `keep_comments = False`
- `adaptive_domain = ''`

### 类方法

与 `Fetcher` 相同：
- `display_config()`: 显示配置
- `configure(**kwargs)`: 配置解析器

### 异步 HTTP 方法

#### async get(url, **kwargs)

异步执行 GET 请求。

```python
import asyncio
from scrapling.fetchers import AsyncFetcher

async def fetch_multiple():
    # 并发请求多个 URL
    tasks = [
        AsyncFetcher.get(f"https://example.com/page{i}")
        for i in range(10)
    ]
    responses = await asyncio.gather(*tasks)
    return responses

# 运行
responses = asyncio.run(fetch_multiple())
```

#### async post(url, **kwargs)

异步执行 POST 请求。

```python
response = await AsyncFetcher.post(
    "https://api.example.com/submit",
    json={"data": "value"}
)
```

#### async put(url, **kwargs)

异步执行 PUT 请求。

#### async delete(url, **kwargs)

异步执行 DELETE 请求。

**使用场景**：
- 需要高并发请求的场景
- 大规模网站抓取
- API 批量调用
- 实时数据监控

## DynamicFetcher 类

### 基本介绍

`DynamicFetcher` 提供通过基于 Chromium 的浏览器获取/加载网站页面的多种选项。支持 JavaScript 执行、页面交互、反机器人绕过等高级功能。

**基类**：`BaseFetcher`

### 类属性

与 `Fetcher` 相同：
- `huge_tree = True`
- `adaptive = False`
- `storage = SQLiteStorageSystem`
- 等等...

### 类方法

#### fetch(url, **kwargs)

通过浏览器执行请求。

**核心参数**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|---------|------|
| `url` | str | - | 目标 URL |
| `headless` | bool | True | 是否使用无头模式 |
| `disable_resources` | bool | False | 禁用不必要的资源请求 |
| `blocked_domains` | set | None | 阻止请求的域名列表 |
| `useragent` | str | None | 用户代理字符串 |
| `cookies` | dict | None | Cookies |
| `network_idle` | bool | True | 等待网络空闲 |
| `load_dom` | bool | True | 等待 DOM 完全加载 |
| `timeout` | int | 30000 | 超时时间（毫秒） |
| `wait` | int | 0 | 完成后等待时间（毫秒） |
| `page_action` | callable | None | 页面操作函数 |
| `wait_selector` | str | None | 等待特定选择器 |
| `wait_selector_state` | str | 'attached' | 选择器等待状态 |
| `init_script` | str | None | 初始化脚本路径 |
| `locale` | str | None | 浏览器语言设置 |
| `real_chrome` | bool | False | 使用本地 Chrome 浏览器 |
| `cdp_url` | str | None | 连接到现有 CDP |
| `google_search` | bool | True | 设置 Google Referer |
| `extra_headers` | dict | None | 额外的请求头 |
| `proxy` | str/dict | None | 代理设置 |
| `extra_flags` | list | None | 额外的浏览器标志 |
| `selector_config` | dict | None | Selector 配置 |
| `additional_args` | dict | None | Playwright 额外参数 |

**返回值**：`Response` 对象

### 使用示例

#### 基本使用

```python
from scrapling.fetchers import DynamicFetcher

# 基本动态抓取
response = DynamicFetcher.fetch(
    "https://example.com",
    headless=True
)

# 查看渲染后的内容
print(response.css("h1::text").get())
```

#### 等待特定元素

```python
# 等待某个元素出现
response = DynamicFetcher.fetch(
    "https://example.com",
    wait_selector="div.product-list",
    wait_selector_state="visible"
)
```

#### 页面交互

```python
# 执行页面操作
def page_action(page):
    # 点击按钮
    page.click("button.load-more")
    # 等待加载
    page.wait_for_selector("div.more-content")

response = DynamicFetcher.fetch(
    "https://example.com",
    page_action=page_action
)
```

#### 使用本地浏览器

```python
# 使用本地 Chrome 浏览器
response = DynamicFetcher.fetch(
    "https://example.com",
    real_chrome=True,
    headless=False  # 可见模式
)
```

#### 性能优化

```python
# 禁用不必要的资源
response = DynamicFetcher.fetch(
    "https://example.com",
    disable_resources=True,
    blocked_domains={
        "analytics.com",
        "tracking.com"
    }
)
```

#### 代理支持

```python
# 使用代理
response = DynamicFetcher.fetch(
    "https://example.com",
    proxy={
        "server": "http://proxy:8080",
        "username": "user",
        "password": "pass"
    }
)
```

## StealthyFetcher 类

### 基本介绍

`StealthyFetcher` 提供高级反机器人绕过功能，适用于具有严格防护机制的网站。结合 Playwright 的反检测技术与 Scrapling 的伪装功能。

**基类**：`BaseFetcher`

### 特性

- 浏览器指纹伪装
- WebSocket 轮询拦截
- 真实浏览器行为模拟
- 反检测增强

### 使用方法

```python
from scrapling.fetchers import StealthyFetcher

# 基本使用
response = StealthyFetcher.fetch(
    "https://protected-site.com",
    headless=True
)
```

### 高级选项

```python
# 使用代理和自定义配置
response = StealthyFetcher.fetch(
    "https://protected-site.com",
    proxy="http://proxy:8080",
    stealthy_headers=True,
    google_search=True
)
```

## Session 类

### FetcherSession

`FetcherSession` 提供会话管理功能，支持 Cookie 保持、连接池复用等。

```python
from scrapling.fetchers import FetcherSession

session = FetcherSession()

# 多个请求共享会话
response1 = session.get("https://example.com/login")
response2 = session.get("https://example.com/dashboard")  # Cookie 自动保持
```

### AsyncFetcherSession

`FetcherSession` 的异步版本。

```python
from scrapling.fetchers import AsyncFetcherSession

session = AsyncFetcherSession()

# 异步会话请求
response1 = await session.get("https://example.com/api/1")
response2 = await session.get("https://example.com/api/2")
```

### StealthySession

结合反检测功能的会话管理。

```python
from scrapling.fetchers import StealthySession

session = StealthySession()
response = session.fetch("https://protected-site.com")
```

### DynamicSession

动态抓取器的会话管理。

```python
from scrapling.fetchers import DynamicSession

session = DynamicSession()

# 保持浏览器会话
response1 = session.fetch("https://example.com")
response2 = session.fetch("https://example.com/page2")
```

### AsyncDynamicSession

动态抓取器的异步会话管理。

## 代理轮换支持

所有抓取器都支持代理轮换功能：

```python
from scrapling.fetchers import Fetcher, ProxyRotator

# 创建代理轮换器
rotator = ProxyRotator([
    "http://proxy1:8080",
    "http://proxy2:8080",
    {"server": "http://proxy3:8080", "username": "user", "password": "pass"}
])

# 使用代理轮换
for i in range(10):
    proxy = rotator.get_proxy()
    response = Fetcher.get(
        "https://example.com",
        proxy=proxy
    )
```

## 配置与优化

### 全局配置

```python
from scrapling.fetchers import Fetcher, AsyncFetcher, DynamicFetcher

# 配置所有 Fetcher 实例
Fetcher.configure(
    huge_tree=True,
    adaptive=True,
    keep_comments=False
)

# 配置 AsyncFetcher
AsyncFetcher.configure(
    huge_tree=True,
    timeout=60
)

# 配置 DynamicFetcher
DynamicFetcher.configure(
    huge_tree=True,
    keep_comments=True
)
```

### 性能优化建议

1. **选择合适的抓取器**
   - 静态页面：使用 `Fetcher` 或 `AsyncFetcher`
   - 动态页面：使用 `DynamicFetcher`
   - 高防护网站：使用 `StealthyFetcher`

2. **并发控制**
   - 使用异步版本提高吞吐量
   - 设置合理的 `concurrent_requests`

3. **资源优化**
   - 对于 DynamicFetcher，使用 `disable_resources=True`
   - 使用 `blocked_domains` 阻止不必要的域名

4. **错误处理**
   - 设置合理的 `retries` 和 `retry_delay`
   - 实现自定义的错误处理逻辑

## 常见使用场景

### 简单网站抓取

```python
from scrapling.fetchers import Fetcher

response = Fetcher.get("https://example.com")
title = response.css("h1::text").get()
print(title)
```

### API 调用

```python
from scrapling.fetchers import Fetcher

response = Fetcher.get(
    "https://api.example.com/data",
    headers={"Authorization": "Bearer token"},
    params={"page": 1}
)

data = response.json()
print(data)
```

### 批量抓取

```python
import asyncio
from scrapling.fetchers import AsyncFetcher

async def fetch_all():
    urls = [f"https://example.com/page/{i}" for i in range(1, 11)]
    tasks = [AsyncFetcher.get(url) for url in urls]
    return await asyncio.gather(*tasks)

responses = asyncio.run(fetch_all())
```

### JavaScript 渲染内容

```python
from scrapling.fetchers import DynamicFetcher

response = DynamicFetcher.fetch(
    "https://dynamic-site.com",
    load_dom=True,
    network_idle=True
)

content = response.css("div.dynamic-content::text").get()
print(content)
```

### 登录与会话保持

```python
from scrapling.fetchers import FetcherSession

session = FetcherSession()

# 登录
login_response = session.post(
    "https://example.com/login",
    data={"username": "user", "password": "pass"}
)

# 访问需要认证的页面
dashboard = session.get("https://example.com/dashboard")
```

## 错误处理

```python
from scrapling.fetchers import Fetcher
from scrapling.fetchers.errors import FetchError

try:
    response = Fetcher.get(
        "https://example.com",
        retries=3,
        timeout=10
    )
except FetchError as e:
    print(f"抓取失败: {e}")
except Exception as e:
    print(f"其他错误: {e}")
```

## 总结

Scrapling 的 Fetchers 系统提供了灵活而强大的网页抓取能力：

1. **多种抓取器**：从简单到复杂，满足不同需求
2. **同步/异步**：提供同步和异步两种 API
3. **会话管理**：支持 Cookie 保持和连接复用
4. **浏览器模拟**：真实的浏览器指纹和行为
5. **反检测**：针对不同防护级别的解决方案
6. **代理支持**：完整的代理轮换功能
7. **易于配置**：全局和实例级别的配置选项

根据网站的技术栈和防护级别选择合适的抓取器，可以有效地完成各种抓取任务。

---

# Response 类 API 参考 (Response API Reference)

## 概述

`Response` 类是所有抓取器返回的 HTTP 响应的包装器，提供了对状态码、响应头、响应体、Cookies 以及用于解析的 `Selector` 的统一访问接口。该类继承自 `Selector`，因此可以直接使用所有选择器和解析功能。

## 导入方式

```python
from scrapling.engines.toolbelt.custom import Response
```

## 类定义

```python
Response(
    url,
    content,
    status,
    reason,
    cookies,
    headers,
    request_headers,
    encoding="utf-8",
    method="GET",
    history=None,
    meta=None,
    **selector_config
)
```

**基类**：`Selector`

## 构造函数参数

| 参数 | 类型 | 默认值 | 说明 |
|------|------|---------|------|
| `url` | str | - | 请求的 URL |
| `content` | str \| bytes | - | HTML 内容，可以是字符串或字节 |
| `status` | int | - | HTTP 状态码 |
| `reason` | str | - | HTTP 原因短语 |
| `cookies` | Tuple[Dict, ...] \| Dict | - | Cookies |
| `headers` | Dict | - | 响应头 |
| `request_headers` | Dict | - | 请求头 |
| `encoding` | str | 'utf-8' | 编码类型 |
| `method` | str | 'GET' | HTTP 方法 |
| `history` | List | None | 重定向历史 |
| `meta` | Dict | None | 元数据 |
| **selector_config | Any | - | 传递给 Selector 的配置参数 |

## 继承自 Selector 的属性

由于 `Response` 继承自 `Selector`，它拥有所有 Selector 的属性和方法：

### 基础属性

```python
# URL 和编码
response.url        # 请求的 URL
response.encoding   # 编码类型

# 元素信息
response.tag         # 元素标签名
response.text        # 直接文本内容
response.attrib      # 元素属性字典
response.html_content # HTML 内容

# DOM 关系
response.parent      # 父元素
response.children    # 子元素列表
response.siblings    # 兄弟元素列表
response.path       # DOM 路径
response.next       # 下一个兄弟元素
response.previous   # 上一个兄弟元素
```

### 选择器方法

```python
# CSS 选择器
elements = response.css("div.product")
text = response.css("h1::text").get()

# XPath 选择器
elements = response.xpath("//div[@class='product']")

# 查找方法
elements = response.find_all("div")
element = response.find("div", class_="product")

# 文本提取
text = response.get_all_text()
```

## Response 特有属性

### status

HTTP 状态码。

```python
response = Fetcher.get("https://example.com")
print(response.status)  # 200, 404, 500 等
```

### reason

HTTP 原因短语。

```python
print(response.reason)  # "OK", "Not Found", "Internal Server Error" 等
```

### cookies

响应的 Cookies。

```python
print(response.cookies)
# 输出: {'session_id': 'abc123', 'user_pref': 'dark_mode'}

# 访问特定 Cookie
print(response.cookies['session_id'])
```

### headers

响应头字典。

```python
print(response.headers)
# 输出: {
#     'content-type': 'text/html; charset=utf-8',
#     'content-length': '1234',
#     'server': 'nginx'
# }

# 访问特定头
print(response.headers.get('content-type'))
```

### request_headers

请求头字典。

```python
print(response.request_headers)
# 输出发送的实际请求头
```

### history

重定向历史列表。

```python
print(response.history)
# 输出: [
#     Response(url='http://example.com', status=301, ...),
#     Response(url='https://example.com', status=200, ...)
# ]

# 访问重定向链中的特定响应
for resp in response.history:
    print(f"{resp.url} -> {resp.status}")
```

### meta

元数据字典，用于存储自定义信息。

```python
print(response.meta)
# 爬虫可以在这里存储额外的信息

# 在爬虫中设置 meta
meta = {'custom_field': 'value'}
response = Fetcher.get("https://example.com", meta=meta)
```

### request

关联的 `Request` 对象（由爬虫设置）。

```python
print(response.request)
print(response.request.url)
print(response.request.method)
print(response.request.headers)
```

### body

响应的原始 body（bytes 格式），用于二进制和非 HTML 请求。

```python
# 获取原始 bytes
raw_bytes = response.body

# 保存二进制文件
with open('image.jpg', 'wb') as f:
    f.write(response.body)

# 查看大小
print(len(response.body))
```

## Response 特有方法

### follow(url, **kwargs)

创建一个请求来跟随 URL，这是爬虫中常用的辅助方法。

**参数**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|---------|------|
| `url` | str | - | 要跟随的 URL（可以是相对 URL） |
| `sid` | str | '' | 使用的会话 ID |
| `callback` | Callable | None | 爬虫回调方法 |
| `priority` | int \| None | None | 优先级数字，数字越大优先级越高 |
| `dont_filter` | bool | False | 是否禁用请求过滤 |
| `meta` | dict | None | 包含在请求中的额外元数据 |
| `referer_flow` | bool | True | 是否设置当前响应 URL 为新请求的 referer |
| **kwargs | Any | - | 额外的 Request 参数 |

**返回值**：Request 对象

**示例**：

```python
# 在爬虫中使用
class MySpider(Spider):
    name = "myspider"
    start_urls = ["https://example.com"]

    async def parse(self, response):
        # 提取链接并跟随
        links = response.css("a::attr(href)").getall()
        for link in links:
            # 创建并 yield 新请求
            yield response.follow(
                link,
                callback=self.parse_detail,
                priority=10
            )

    async def parse_detail(self, response):
        # 解析详情页
        title = response.css("h1::text").get()
        yield {"title": title}
```

### 继承自 Selector 的方法

Response 可以使用 Selector 的所有方法：

```python
# 文本处理
response.get_all_text(separator='\n', strip=False)
response.prettify()

# 选择器生成
response.generate_css_selector()
response.generate_full_css_selector()
response.generate_xpath_selector()
response.generate_full_xpath_selector()

# 正则表达式
response.re(r"price:\s*(\d+)")
response.re_first(r"\d+")

# 类检查
response.has_class("active")

# URL 操作
response.urljoin(relative_url)
```

## 使用示例

### 基本使用

```python
from scrapling.fetchers import Fetcher

# 发起请求
response = Fetcher.get("https://example.com")

# 访问响应信息
print(f"状态码: {response.status}")
print(f"原因: {response.reason}")
print(f"URL: {response.url}")

# 解析内容
title = response.css("h1::text").get()
print(f"标题: {title}")
```

### 检查响应状态

```python
response = Fetcher.get("https://example.com")

# 检查是否成功
if response.status == 200:
    print("请求成功")
elif response.status == 404:
    print("页面不存在")
elif response.status >= 500:
    print("服务器错误")

# 检查是否被重定向
if response.history:
    print(f"重定向次数: {len(response.history)}")
    print(f"原始 URL: {response.history[0].url}")
    print(f"最终 URL: {response.url}")
```

### 处理 Cookies

```python
response = Fetcher.get("https://example.com")

# 查看所有 Cookies
print("Cookies:", response.cookies)

# 访问特定 Cookie
session_id = response.cookies.get('session_id')
if session_id:
    print(f"会话 ID: {session_id}")

# 在后续请求中使用 Cookies
from scrapling.fetchers import FetcherSession

session = FetcherSession()
response = session.get("https://example.com/login")
# Cookies 自动保存

# 访问需要认证的页面
dashboard = session.get("https://example.com/dashboard")
```

### 查看请求头和响应头

```python
response = Fetcher.get("https://example.com")

print("请求头:")
for key, value in response.request_headers.items():
    print(f"  {key}: {value}")

print("\n响应头:")
for key, value in response.headers.items():
    print(f"  {key}: {value}")
```

### 处理二进制内容

```python
# 下载图片
response = Fetcher.get("https://example.com/image.jpg")
with open('image.jpg', 'wb') as f:
    f.write(response.body)

# 下载 PDF
response = Fetcher.get("https://example.com/document.pdf")
with open('document.pdf', 'wb') as f:
    f.write(response.body)
```

### 在爬虫中使用

```python
from scrapling.spiders import Spider

class MySpider(Spider):
    name = "example_spider"
    start_urls = ["https://example.com"]

    async def parse(self, response):
        # 检查响应状态
        if response.status != 200:
            self.logger.error(f"请求失败: {response.status}")
            return

        # 提取数据
        items = []
        for product in response.css("div.product"):
            item = {
                'title': product.css("h2::text").get(),
                'price': product.css(".price::text").get(),
                'url': response.url,
                'status': response.status,
            }
            items.append(item)

        # 提取并跟随链接
        for link in response.css("a::attr(href)").getall():
            yield response.follow(link, callback=self.parse_detail)

    async def parse_detail(self, response):
        yield {
            'title': response.css("h1::text").get(),
            'content': response.css("div.content::text").getall(),
            'meta': response.meta
        }
```

### 使用元数据

```python
response = Fetcher.get(
    "https://example.com",
    meta={
        'custom_id': 123,
        'category': 'products'
    }
)

# 访问元数据
print(response.meta.get('custom_id'))  # 123
print(response.meta.get('category'))  # 'products'
```

### 错误处理

```python
from scrapling.fetchers import Fetcher

try:
    response = Fetcher.get(
        "https://example.com",
        timeout=30,
        retries=3
    )

    # 检查状态码
    if response.status >= 400:
        raise Exception(f"HTTP 错误: {response.status}")

    # 解析响应
    content = response.css("div.content").get()

except Exception as e:
    print(f"错误: {e}")
```

### 流式处理大响应

```python
from scrapling.fetchers import FetcherSession

session = FetcherSession()

response = session.get("https://example.com/large-data")

# 逐块处理
if response.status == 200:
    # 使用 Selector 的方法处理
    for item in response.css("div.item"):
        title = item.css("h3::text").get()
        print(title)
```

## Response 与爬虫集成

### Request-Response 关系

```python
class MySpider(Spider):
    name = "example"

    async def parse(self, response):
        # 访问原始请求
        print(response.request.url)
        print(response.request.method)
        print(response.request.headers)

        # 跟随新请求，保留原始请求的属性
        yield response.follow(
            "/next-page",
            meta={'original_url': response.url}
        )
```

### 错误处理钩子

```python
class MySpider(Spider):
    name = "example"

    async def on_error(self, request, error):
        self.logger.error(
            f"请求失败 {request.url}: {error}"
        )
        # 可以选择重试或记录
```

## 性能优化

### 避免重复解析

```python
# 好的做法：解析一次，多次使用
response = Fetcher.get("https://example.com")
container = response.css("div.container").get()

# 在容器内查找
titles = container.css("h2::text").getall()
prices = container.css(".price::text").getall()
```

### 选择性提取

```python
# 只提取需要的内容，避免处理整个文档
response = Fetcher.get("https://example.com")

# 使用 CSS 选择器精确提取
title = response.css("h1.title::text").get()
content = response.css("div#main::text").getall()
```

### 缓存响应

```python
# 如果响应很大，可以缓存关键部分
response = Fetcher.get("https://example.com")
products = response.css("div.product").getall()

# 处理时不再次访问 response
for product in products:
    # 只使用提取的数据
    print(product.css("h2::text").get())
```

## 常见模式

### 基础数据提取

```python
response = Fetcher.get("https://example.com")

data = {
    'title': response.css("h1::text").get(),
    'description': response.css("meta[name='description']::attr(content)").get(),
    'status': response.status,
    'url': response.url
}
```

### 列表数据提取

```python
response = Fetcher.get("https://example.com/products")

products = []
for item in response.css("div.product-item"):
    product = {
        'name': item.css("h3::text").get(),
        'price': item.css(".price::text").get(),
        'available': 'stock' in item.attrib.get('class', '')
    }
    products.append(product)
```

### 分页处理

```python
async def parse(self, response):
    # 提取当前页数据
    for item in response.css("div.item"):
        yield self.extract_item(item)

    # 提取下一页链接
    next_page = response.css("a.next::attr(href)").get()
    if next_page:
        yield response.follow(next_page)
```

### 表单提交

```python
# 获取表单页面
form_page = Fetcher.get("https://example.com/form")

# 提交表单
submit_response = Fetcher.post(
    "https://example.com/submit",
    data={
        'username': form_page.css("#username::attr(value)").get(),
        'token': form_page.css("#token::attr(value)").get()
    }
)
```

## 注意事项

1. **响应体大小**：对于大文件，使用 `body` 属性时要考虑内存使用
2. **编码问题**：确保使用正确的编码，默认为 UTF-8
3. **重定向**：`response.history` 包含重定向链，`response.url` 是最终 URL
4. **Cookies**：使用 Session 类可以自动管理 Cookies
5. **元数据**：`meta` 是传递自定义信息的便捷方式
6. **错误处理**：始终检查 `response.status` 和处理异常情况
7. **资源释放**：对于流式下载，及时释放资源

## 总结

`Response` 类是 Scrapling 的核心组件之一，提供了：

1. **统一接口**：所有抓取器返回相同类型的 Response
2. **丰富信息**：状态码、头、Cookies、元数据等
3. **解析能力**：继承 Selector，支持所有选择器和解析功能
4. **爬虫集成**：与 Spider 类无缝配合
5. **链式跟踪**：支持请求-响应链的追踪
6. **灵活扩展**：通过 meta 字段支持自定义信息

理解和使用 Response 类是进行有效网页抓取的基础。

---

# Spiders 类 API 参考 (Spiders API Reference)

## 概述

Scrapling 的 Spider 框架提供了完整的网络爬虫构建能力。Spider 类是所有爬虫的基类，支持异步请求、会话管理、错误处理、暂停恢复等高级功能。

## 导入方式

```python
from scrapling.spiders import Spider, Request, CrawlResult, SessionManager, Response
```

## Spider 类

### 基本介绍

`Spider` 是创建网络爬虫的抽象基类，提供了完整的爬虫生命周期管理。

**基类**：`ABC` (抽象基类)

### 构造函数

```python
Spider(crawldir=None, interval=300.0)
```

**参数说明**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|---------|------|
| `crawldir` | Optional[str\|Path] | None | 检查点文件目录，如果提供则启用暂停/恢复功能 |
| `interval` | float | 300.0 | 定期保存检查点的间隔（秒），默认 5 分钟 |

**示例**：

```python
# 基本爬虫
class MySpider(Spider):
    name = "myspider"
    start_urls = ["https://example.com"]

    async def parse(self, response):
        yield {"url": response.url, "title": response.css("h1::text").get()}

# 启用暂停/恢复
class MySpider(Spider):
    name = "myspider"

    def __init__(self):
        super().__init__(crawldir="./crawl_state", interval=180)
```

### 类属性

#### name

爬虫名称，必须定义。

```python
name = None  # 必须在子类中设置
```

**示例**：
```python
class MySpider(Spider):
    name = "example_spider"  # 必须设置
```

#### start_urls

起始 URL 列表。

```python
start_urls = []  # 默认为空列表
```

**示例**：
```python
class MySpider(Spider):
    name = "myspider"
    start_urls = [
        "https://example.com/page1",
        "https://example.com/page2",
        "https://example.com/page3"
    ]
```

#### allowed_domains

允许的域名集合，用于过滤 URL。

```python
allowed_domains = set()  # 默认为空集合
```

**示例**：
```python
class MySpider(Spider):
    name = "myspider"
    allowed_domains = {
        "example.com",
        "subdomain.example.com"
    }
```

#### concurrent_requests

并发请求数量。

```python
concurrent_requests = 4  # 默认值为 4
```

**示例**：
```python
class MySpider(Spider):
    name = "myspider"
    concurrent_requests = 10  # 增加并发数
```

#### concurrent_requests_per_domain

每个域名的并发请求数量。

```python
concurrent_requests_per_domain = 0  # 0 表示不限制
```

**示例**：
```python
class MySpider(Spider):
    name = "myspider"
    concurrent_requests = 20
    concurrent_requests_per_domain = 5  # 每个域名最多 5 个并发
```

#### download_delay

下载延迟（秒）。

```python
download_delay = 0.0  # 默认无延迟
```

**示例**：
```python
class MySpider(Spider):
    name = "myspider"
    download_delay = 1.0  # 每个请求之间延迟 1 秒
```

#### max_blocked_retries

被阻塞请求的最大重试次数。

```python
max_blocked_retries = 3  # 默认重试 3 次
```

**示例**：
```python
class MySpider(Spider):
    name = "myspider"
    max_blocked_retries = 5  # 增加重试次数
```

#### fp_keep_fragments

是否在过滤时保留 URL 片段。

```python
fp_keep_fragments = False  # 默认不保留
```

#### fp_include_headers

是否在过滤时包含请求头。

```python
fp_include_headers = False  # 默认不包含
```

#### fp_include_kwargs

是否在过滤时包含关键字参数。

```python
fp_include_kwargs = False  # 默认不包含
```

#### logging_level

日志级别。

```python
logging_level = DEBUG  # 默认为 DEBUG
```

**可选级别**：
- `logging.DEBUG`
- `logging.INFO`
- `logging.WARNING`
- `logging.ERROR`
- `logging.CRITICAL`

**示例**：
```python
import logging

class MySpider(Spider):
    name = "myspider"
    logging_level = logging.INFO  # 只显示 INFO 及以上级别
```

#### logging_format

日志格式字符串。

```python
logging_format = "[%(asctime)s]:({spider_name}) %(levelname)s: %(message)s"
```

#### logging_date_format

日志日期格式。

```python
logging_date_format = '%Y-%m-%d %H:%M:%S'
```

#### log_file

日志文件路径。

```python
log_file = None  # 默认不写入文件
```

**示例**：
```python
class MySpider(Spider):
    name = "myspider"
    log_file = "./logs/spider.log"
```

### 实例属性

#### logger

爬虫的日志记录器。

```python
logger = logging.getLogger(f'scrapling.spiders.{name}')
```

**示例**：
```python
async def parse(self, response):
    self.logger.info(f"正在处理: {response.url}")
    self.logger.error(f"错误: {error}")
    self.logger.debug(f"调试信息")
```

#### crawldir

爬取目录（检查点保存位置）。

```python
crawldir = Path(crawldir) if crawldir else None
```

#### stats

爬取统计信息（属性）。

```python
@property
def stats(self):
    # 返回当前统计信息
    pass
```

**访问统计信息**：
```python
# 在运行时访问
spider = MySpider()
result = spider.start()
print(spider.stats)
```

## 核心方法

### start_requests()

生成初始请求以启动爬取。

```python
async def start_requests(self) -> AsyncGenerator[Request, None]:
    """生成初始请求以启动爬取。

    默认情况下，这为 `start_urls` 中的每个 URL 生成 Request 对象，
    使用会话管理器的默认会话和 `parse()` 作为回调。

    重写此方法以对初始请求进行更多控制
    （例如，添加自定义头、使用不同的回调等）
    """
    if not self.start_urls:
        raise RuntimeError(
            "Spider has no starting point, either set `start_urls` or override `start_requests` function."
        )

    for url in self.start_urls:
        yield Request(url, sid=self._session_manager.default_session_id)
```

**重写示例**：
```python
class MySpider(Spider):
    name = "myspider"
    start_urls = ["https://example.com"]

    async def start_requests(self):
        for url in self.start_urls:
            # 添加自定义头
            yield Request(
                url,
                headers={
                    "User-Agent": "Custom Agent",
                    "Authorization": "Bearer token"
                },
                meta={"custom": "value"}
            )
```

### parse(response)

处理响应的默认回调方法（抽象方法）。

```python
@abstractmethod
async def parse(self, response: "Response") -> AsyncGenerator[Dict[str, Any] | Request | None, None]:
    """处理响应的默认回调"""
    raise NotImplementedError(f"{self.__class__.__name__} must implement parse() method")
    yield  # 使其成为生成器
```

**实现示例**：
```python
class MySpider(Spider):
    name = "myspider"
    start_urls = ["https://example.com"]

    async def parse(self, response):
        # 提取数据
        title = response.css("h1::text").get()

        # yield 数据
        yield {"title": title, "url": response.url}

        # yield 新请求
        for link in response.css("a::attr(href)").getall():
            yield Request(link)
```

### configure_sessions(manager)

为爬虫配置会话。

```python
def configure_sessions(self, manager: SessionManager) -> None:
    """为爬虫配置会话。

    重写此方法以添加自定义会话。
    默认实现创建一个 FetcherSession 会话。

    添加的第一个会话成为 `start_requests()` 的默认会话，
    除非另有指定。

    :param manager: 要配置的 SessionManager
    """
    from scrapling.fetchers import FetcherSession

    manager.add("default", FetcherSession())
```

**重写示例**：
```python
from scrapling.fetchers import FetcherSession, AsyncFetcherSession, DynamicFetcherSession

class MySpider(Spider):
    name = "myspider"

    def configure_sessions(self, manager):
        # 添加多个会话
        manager.add("default", FetcherSession())
        manager.add("async", AsyncFetcherSession())
        manager.add("dynamic", DynamicFetcherSession())

        # 设置默认会话
        manager.set_default("default")
```

## 生命周期钩子

### on_start(resuming=False)

在爬取开始前调用，用于初始化逻辑。

```python
async def on_start(self, resuming: bool = False) -> None:
    """在爬取开始前调用。重写以进行设置逻辑。

    :param resuming: 如果爬虫从检查点恢复，则为 True，留给用户使用。
    """
    if resuming:
        self.logger.debug("从检查点恢复爬虫")
    else:
        self.logger.debug("启动爬虫")
```

**使用示例**：
```python
class MySpider(Spider):
    name = "myspider"

    async def on_start(self, resuming=False):
        if not resuming:
            # 初始化数据库连接
            self.db = await connect_to_database()
            self.logger.info("数据库已连接")
        else:
            # 重新连接数据库
            self.db = await reconnect_to_database()
            self.logger.info("数据库已重新连接")
```

### on_close()

在爬取完成后调用，用于清理逻辑。

```python
async def on_close(self) -> None:
    """在爬取完成后调用。重写以进行清理逻辑。"""
    self.logger.debug("爬虫已关闭")
```

**使用示例**：
```python
class MySpider(Spider):
    name = "myspider"

    async def on_close(self):
        # 关闭数据库连接
        await self.db.close()
        self.logger.info("数据库已关闭")

        # 清理临时文件
        cleanup_temp_files()
```

### on_error(request, error)

处理所有爬虫请求的错误。

```python
async def on_error(self, request: Request, error: Exception) -> None:
    """处理所有爬虫请求的错误。

    重写以进行自定义错误处理。
    """
    pass
```

**使用示例**：
```python
class MySpider(Spider):
    name = "myspider"

    async def on_error(self, request, error):
        # 记录错误
        self.logger.error(f"请求失败 {request.url}: {error}")

        # 发送通知
        await send_notification(
            subject="爬虫错误",
            message=f"URL: {request.url}, Error: {str(error)}"
        )
```

### on_scraped_item(item)

在抓取项目上处理的钩子，可以返回 None 以静默丢弃项目。

```python
async def on_scraped_item(self, item: Dict[str, Any]) -> Dict[str, Any] | None:
    """一个供用户重写的钩子，用于对抓取的项目进行一些处理，
    返回 `None` 以静默丢弃项目。
    """
    return item
```

**使用示例**：
```python
class MySpider(Spider):
    name = "myspider"

    async def on_scraped_item(self, item):
        # 数据清洗
        if item.get('price', 0) < 0:
            return None  # 丢弃无效数据

        # 数据转换
        item['price'] = float(item['price'])
        item['timestamp'] = datetime.now()

        # 数据验证
        if not item.get('title'):
            return None

        return item
```

## 阻塞检测与重试

### is_blocked(response)

检查响应是否被阻塞。

```python
async def is_blocked(self, response: "Response") -> bool:
    """检查响应是否被阻塞。用户应该重写此方法以进行自定义检测逻辑。"""
    if response.status in BLOCKED_CODES:
        return True
    return False
```

**BLOCKED_CODES** 通常包括：403, 429 等。

**重写示例**：
```python
class MySpider(Spider):
    name = "myspider"

    async def is_blocked(self, response):
        # 检查状态码
        if response.status in [403, 429, 401]:
            return True

        # 检查响应内容
        if "Access Denied" in response.text:
            return True

        if "CAPTCHA" in response.text:
            return True

        return False
```

### retry_blocked_request(request, response)

在重试被阻塞的请求之前准备请求（如果需要）。

```python
async def retry_blocked_request(self, request: Request, response: "Response") -> Request:
    """用户应该重写此方法以在重试之前准备被阻塞的请求（如果需要）。"""
    return request
```

**重写示例**：
```python
class MySpider(Spider):
    name = "myspider"

    async def retry_blocked_request(self, request, response):
        # 更换代理
        new_proxy = self.get_new_proxy()
        request.meta['proxy'] = new_proxy

        # 更换用户代理
        request.headers['User-Agent'] = self.get_new_useragent()

        # 增加重试延迟
        request.meta['retry_delay'] = 5

        return request
```

## 运行爬虫

### start(use_uvloop=False, **backend_options)

运行爬虫并返回结果。

```python
def start(self, use_uvloop: bool = False, **backend_options: Any) -> CrawlResult:
    """运行爬虫并返回结果。

    这是运行爬虫的主要入口点。
    通过 anyio 在内部处理异步执行。

    按下 Ctrl+C 将启动优雅关闭（等待活动任务完成）。
    按下 Ctrl+C 第二次将强制立即停止。

    如果设置了 crawldir，在优雅关闭时也会保存检查点，
    允许您稍后通过再次运行爬虫来恢复爬取。

    :param use_uvloop: 是否使用更快的 uvloop/winloop 事件循环实现（如果可用）。
    :param backend_options: 与 `anyio.run` 一起使用的 Asyncio 后端选项
    """
```

**返回值**：`CrawlResult` 对象

**示例**：
```python
# 基本使用
spider = MySpider()
result = spider.start()

# 使用 uvloop（更快）
spider = MySpider()
result = spider.start(use_uvloop=True)

# 传递后端选项
spider = MySpider()
result = spider.start(use_uvloop=True, backend_options={"debug": True})
```

**信号处理**：
- 第一次 `Ctrl+C`：优雅关闭，等待活动任务完成，保存检查点（如果设置了 crawldir）
- 第二次 `Ctrl+C`：强制立即停止

### stream()

流式返回抓取的项目，适用于长时间运行的爬虫。

```python
async def stream(self) -> AsyncGenerator[Dict[str, Any], None]:
    """在抓取时流式返回项目。

    适用于长时间运行的爬虫或基于爬虫构建应用程序。

    必须从异步上下文调用。
    在抓取时逐个 yield 项目。
    在迭代期间访问 `spider.stats` 以获取实时统计信息。

    注意：流模式下不可用 SIGINT 的暂停/恢复处理。
    """
```

**示例**：
```python
import asyncio

async def main():
    spider = MySpider()

    # 流式处理项目
    async for item in spider.stream():
        # 实时处理每个项目
        process_item(item)

        # 查看统计信息
        if spider.stats.get('item_count', 0) % 100 == 0:
            print(f"已处理: {spider.stats['item_count']} 个项目")

# 运行
asyncio.run(main())
```

### pause()

请求爬取过程的优雅关闭。

```python
def pause(self):
    """请求爬取过程的优雅关闭。"""
    if self._engine:
        self._engine.request_pause()
    else:
        raise RuntimeError("No active crawl to stop")
```

**示例**：
```python
spider = MySpider()

# 在另一个线程或信号处理器中
def signal_handler(signum, frame):
    print("收到暂停信号...")
    spider.pause()

# 注册信号处理
signal.signal(signal.SIGINT, signal_handler)
```

## Request 类

### 基本介绍

`Request` 类表示一个爬取请求，包含 URL、方法、头、回调等信息。

### 构造函数

```python
Request(
    url,
    sid="",
    method="GET",
    headers=None,
    cookies=None,
    params=None,
    data=None,
    json=None,
    meta=None,
    callback=None,
    priority=None,
    dont_filter=False,
    **kwargs
)
```

**参数说明**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|---------|------|
| `url` | str | - | 请求的 URL |
| `sid` | str | '' | 会话 ID |
| `method` | str | 'GET' | HTTP 方法 |
| `headers` | dict | None | 请求头 |
| `cookies` | dict | None | Cookies |
| `params` | dict | None | 查询参数 |
| `data` | dict/str | None | 请求体数据 |
| `json` | dict | None | JSON 数据 |
| `meta` | dict | None | 元数据 |
| `callback` | Callable | None | 回调函数 |
| `priority` | int | None | 优先级 |
| `dont_filter` | bool | False | 是否禁用过滤 |
| `**kwargs` | Any | - | 额外的请求参数 |

**示例**：
```python
# 基本 GET 请求
request = Request("https://example.com")

# POST 请求
request = Request(
    "https://api.example.com/submit",
    method="POST",
    json={"key": "value"}
)

# 带元数据的请求
request = Request(
    "https://example.com",
    headers={"User-Agent": "Custom"},
    meta={"custom_field": "value"},
    priority=10
)

# 不过滤的请求
request = Request(
    "https://example.com",
    dont_filter=True
)
```

## SessionManager 类

### 基本介绍

管理爬虫的会话，支持多种抓取器类型。

### 方法

#### add(sid, session)

添加会话。

```python
manager.add("default", FetcherSession())
manager.add("async", AsyncFetcherSession())
manager.add("dynamic", DynamicFetcherSession())
```

#### set_default(sid)

设置默认会话。

```python
manager.set_default("async")
```

#### get(sid)

获取会话。

```python
session = manager.get("default")
```

## 完整示例

### 基本爬虫

```python
from scrapling.spiders import Spider

class MySpider(Spider):
    name = "example_spider"
    start_urls = ["https://example.com"]

    async def parse(self, response):
        # 提取数据
        title = response.css("h1::text").get()

        # yield 数据
        yield {
            "title": title,
            "url": response.url,
            "status": response.status
        }

# 运行爬虫
spider = MySpider()
result = spider.start()

# 查看结果
for item in result.items:
    print(item)
```

### 分页爬虫

```python
from scrapling.spiders import Spider

class PaginationSpider(Spider):
    name = "pagination_spider"
    start_urls = ["https://example.com/products?page=1"]

    async def parse(self, response):
        # 提取产品
        for product in response.css("div.product"):
            yield {
                "name": product.css("h3::text").get(),
                "price": product.css(".price::text").get()
            }

        # 提取下一页链接
        next_page = response.css("a.next::attr(href)").get()
        if next_page:
            yield Request(next_page)

# 运行
spider = PaginationSpider()
result = spider.start()
```

### 会话配置

```python
from scrapling.spiders import Spider
from scrapling.fetchers import FetcherSession, DynamicFetcherSession, ProxyRotator

class AdvancedSpider(Spider):
    name = "advanced_spider"

    def configure_sessions(self, manager):
        # 添加代理轮换
        rotator = ProxyRotator([
            "http://proxy1:8080",
            "http://proxy2:8080"
        ])

        # 添加多个会话
        manager.add("default", FetcherSession(proxy=rotator))
        manager.add("dynamic", DynamicFetcherSession(headless=True))

    async def parse(self, response):
        yield {"url": response.url}
```

### 错误处理

```python
from scrapling.spiders import Spider

class RobustSpider(Spider):
    name = "robust_spider"
    start_urls = ["https://example.com"]

    async def is_blocked(self, response):
        # 自定义阻塞检测
        return response.status in [403, 429] or "blocked" in response.text.lower()

    async def retry_blocked_request(self, request, response):
        # 重试时更改代理
        request.meta['retry_count'] = request.meta.get('retry_count', 0) + 1
        if request.meta['retry_count'] > self.max_blocked_retries:
            return request

        # 更换代理
        request.proxy = self.get_new_proxy()
        return request

    async def on_error(self, request, error):
        self.logger.error(f"错误: {error} - URL: {request.url}")

    async def parse(self, response):
        yield {"url": response.url}
```

### 数据处理钩子

```python
from scrapling.spiders import Spider

class ProcessedSpider(Spider):
    name = "processed_spider"
    start_urls = ["https://example.com"]

    async def on_scraped_item(self, item):
        # 数据清洗
        if not item.get('title'):
            return None  # 丢弃无效数据

        # 数据转换
        item['title'] = item['title'].strip()
        if 'price' in item:
            item['price'] = float(item['price'].replace('$', ''))

        # 添加元数据
        item['scraped_at'] = datetime.now().isoformat()

        return item

    async def parse(self, response):
        yield {"title": "示例", "price": "$100"}
```

### 暂停/恢复

```python
from scrapling.spiders import Spider

class PersistentSpider(Spider):
    name = "persistent_spider"
    start_urls = ["https://example.com"]

    def __init__(self):
        super().__init__(crawldir="./crawl_state", interval=180)

    async def on_start(self, resuming):
        if resuming:
            self.logger.info("从检查点恢复...")
        else:
            self.logger.info("开始新的爬取...")

    async def parse(self, response):
        yield {"url": response.url}

# 第一次运行
spider = PersistentSpider()
result = spider.start()  # 按 Ctrl+C 保存检查点

# 后续恢复
spider = PersistentSpider()
result = spider.start()  # 自动从检查点恢复
```

### 流式处理

```python
import asyncio
from scrapling.spiders import Spider

class StreamSpider(Spider):
    name = "stream_spider"
    start_urls = ["https://example.com"]

    async def parse(self, response):
        yield {"data": "example"}

async def main():
    spider = StreamSpider()

    # 流式处理项目
    async for item in spider.stream():
        print(f"处理: {item}")

        # 查看实时统计
        print(f"统计: {spider.stats}")

asyncio.run(main())
```

## 总结

Spider 框架提供了完整的爬虫构建能力：

1. **生命周期管理**：完整的启动、运行、关闭流程
2. **异步支持**：原生异步，高并发性能
3. **会话管理**：灵活的会话配置
4. **错误处理**：自定义的错误检测和重试机制
5. **数据钩子**：项目处理和清洗
6. **暂停/恢复**：检查点机制支持长时间运行
7. **流式处理**：实时数据流处理
8. **日志记录**：完善的日志系统

通过组合这些功能，可以构建强大而灵活的网络爬虫。

---


# MCP 服务器 API 参考 (MCP Server API Reference)

## 概述

Scrapling MCP (Model Context Protocol) 服务器提供了六个强大的网页抓取工具，通过 MCP 协议将 Scrapling 的能力直接集成到 AI 聊天机器人和代理中，支持高级反机器人绕过功能的对话式网页抓取。

## 启动 MCP 服务器

### 命令行启动

```bash
scrapling mcp
```

### 程序化启动

```python
from scrapling.core.ai import ScraplingMCPServer

server = ScraplingMCPServer()
server.serve(http=False, host="0.0.0.0", port=8000)
```

## ResponseModel 类

### 基本介绍

所有 MCP 服务器工具返回的标准化响应结构。

**基类**：`BaseModel`

### 属性

#### status

```python
status = Field(description="网站返回的状态码。")
```

HTTP 状态码。

**示例**：
```python
response = server.get("https://example.com")
print(response.status)  # 200, 404, 500 等
```

#### content

```python
content = Field(description="页面的 Markdown/HTML 内容或文本内容。")
```

页面内容，根据 `extraction_type` 参数返回不同格式。

**示例**：
```python
# Markdown 格式（默认）
response = server.get(
    "https://example.com",
    extraction_type="markdown"
)
print(response.content)  # Markdown 格式的页面内容

# HTML 格式
response = server.get(
    "https://example.com",
    extraction_type="html"
)
print(response.content)  # 原始 HTML

# 文本格式
response = server.get(
    "https://example.com",
    extraction_type="text"
)
print(response.content)  # 纯文本内容
```

#### url

```python
url = Field(description="用户提供的 URL，导致此响应。")
```

请求的 URL。

## ScraplingMCPServer 类

### 基本介绍

MCP 服务器的主类，提供所有网页抓取工具。

### 方法

#### get(url, **kwargs)

对 URL 执行 GET HTTP 请求并返回结果的结构化输出。仅适用于低到中等防护级别的网站。对于高防护级别或需要 JS 加载的网站，使用其他工具。

**参数说明**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|---------|------|
| `url` | str | - | 请求的 URL |
| `impersonate` | str | 'chrome' | 要模拟的浏览器版本，默认使用最新 Chrome 版本 |
| `extraction_type` | str | 'markdown' | 要从页面提取的内容类型。选项：<br>- "markdown": 将页面内容转换为 Markdown 格式<br>- "html": 返回页面的原始 HTML 内容<br>- "text": 返回页面的文本内容 |
| `css_selector` | str | None | 从页面提取内容的 CSS 选择器。如果 `main_content_only` 为 True，则在页面的主要内容上执行。默认为 None |
| `main_content_only` | bool | True | 是否仅提取页面的主要内容。默认为 True。主要内容是 `<body>` 标签内的数据 |
| `params` | dict | None | 请求的查询字符串参数 |
| `headers` | dict | None | 请求中包含的头部 |
| `cookies` | dict | None | 请求中使用的 Cookies |
| `timeout` | int\|float | 30 | 超时前的秒数 |
| `follow_redirects` | bool | True | 是否跟随重定向。默认为 True |
| `max_redirects` | int | 30 | 最大重定向次数。默认 30，使用 -1 表示无限制 |
| `retries` | int | 3 | 重试次数。默认为 3 |
| `retry_delay` | int | 1 | 重试尝试之间等待的秒数。默认为 1 秒 |
| `proxy` | str | None | 要使用的代理 URL。格式: "http://username:password@localhost:8030"。不能与 `proxies` 参数一起使用 |
| `proxy_auth` | dict | None | 代理的 HTTP 基本认证，包含 `username` 和 `password` 键的字典格式 |
| `auth` | dict | None | HTTP 基本认证，包含 `username` 和 `password` 键的字典格式 |
| `verify` | bool | True | 是否验证 HTTPS 证书 |
| `http3` | bool | False | 是否使用 HTTP3。默认为 False。与 `impersonate` 一起使用可能会有问题 |
| `stealthy_headers` | bool | True | 如果启用（默认），创建并添加真实的浏览器头部。它还设置 Google referer 头部 |

**返回值**：`ResponseModel` 对象

**注意**：如果 `css_selector` 解析到多个元素，将返回所有元素。

**示例**：

```python
# 基本 GET 请求
response = server.get("https://example.com")
print(f"状态: {response.status}")
print(f"内容: {response.content}")

# 提取特定元素
response = server.get(
    "https://example.com",
    css_selector="h1",
    extraction_type="text"
)

# 使用代理
response = server.get(
    "https://example.com",
    proxy="http://proxy:8080"
)

# 自定义头部
response = server.get(
    "https://example.com",
    headers={
        "User-Agent": "Custom Agent",
        "Authorization": "Bearer token"
    }
)

# 提取 Markdown
response = server.get(
    "https://example.com",
    extraction_type="markdown"
)

# 提取 HTML
response = server.get(
    "https://example.com",
    extraction_type="html"
)
```

#### bulk_get(urls, **kwargs)

对一组 URL 执行 GET HTTP 请求，每个 URL 返回一个结果的结构化输出。仅适用于低到中等防护级别的网站。

**参数**：

与 `get()` 相同，但：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|---------|------|
| `urls` | list[str] | - | 要请求的 URL 列表 |

**返回值**：`ResponseModel` 对象列表

**示例**：

```python
# 批量请求多个 URL
urls = [
    "https://example.com/page1",
    "https://example.com/page2",
    "https://example.com/page3"
]

responses = await server.bulk_get(urls)

for response in responses:
    print(f"{response.url}: {response.status}")

# 带参数的批量请求
responses = await server.bulk_get(
    urls,
    timeout=30,
    retries=2
)
```

#### fetch(url, **kwargs)

通过浏览器获取 URL。使用 Playwright 驱动的浏览器，适合低到中等防护级别，或需要 JS 加载但不要求高级反机器人绕过的网站。

**参数说明**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|---------|------|
| `url` | str | - | 目标 URL |
| `headless` | bool | True | 浏览器是否以无头/隐藏（默认）或有头/可见模式运行 |
| `disable_resources` | bool | False | 放弃对不必要的资源的请求以提升速度 |
| `blocked_domains` | set | None | 阻止对其发出请求的域名集合。也会匹配子域名（例如，"example.com" 也会阻止 "sub.example.com"） |
| `useragent` | str | None | 要使用的用户代理字符串。否则，抓取器将生成相同浏览器的真实用户代理并使用它 |
| `cookies` | dict | None | 为下一个请求设置的 Cookies |
| `network_idle` | bool | True | 等待页面直到至少 500 毫秒没有网络连接 |
| `load_dom` | bool | True | 默认启用，等待页面上的所有 JavaScript 完全加载和执行 |
| `timeout` | int | 30000 | 在页面的所有操作和等待中使用的超时（毫秒）。默认为 30000 |
| `wait` | int | 0 | 抓取器在关闭页面并返回 Response 对象之前等待的时间（毫秒） |
| `page_action` | callable | None | 为自动化添加的函数。接受 `page` 对象并执行您需要的自动化 |
| `wait_selector` | str | None | 等待特定的 CSS 选择器处于特定状态 |
| `init_script` | str | None | 使用此请求在页面创建时要执行的 JavaScript 文件的绝对路径 |
| `locale` | str | None | 如果需要，设置浏览器的语言环境。默认为系统默认语言环境 |
| `wait_selector_state` | str | 'attached' | 等待 `wait_selector` 给定的选择器的状态。默认状态为 `attached` |
| `real_chrome` | bool | False | 如果您的设备上安装了 Chrome 浏览器，请启用此项，抓取器将启动您的浏览器实例并使用它 |
| `cdp_url` | str | None | 不启动新的浏览器实例，而是连接到此 CDP URL 以通过 CDP 控制真实浏览器 |
| `google_search` | bool | True | 默认启用，Scrapling 将设置 Google referer 头部 |
| `extra_headers` | dict | None | 要添加到请求的额外头部字典 |
| `proxy` | str\|dict | None | 要在请求中使用的代理。可以是字符串或仅包含 'server'、'username' 和 'password' 键的字典 |
| `extra_flags` | list | None | 在浏览器启动时要传递的额外浏览器标志列表 |
| `selector_config` | dict | None | 将在创建最终 Selector 类时传递的参数 |
| `additional_args` | dict | None | 要作为额外设置传递给 Playwright 上下文的参数 |
| `extraction_type` | str | 'markdown' | 内容提取类型（与 `get()` 相同） |
| `css_selector` | str | None | CSS 选择器（与 `get()` 相同） |
| `main_content_only` | bool | True | 是否仅提取主要内容（与 `get()` 相同） |

**返回值**：`ResponseModel` 对象

**示例**：

```python
# 基本动态抓取
response = await server.fetch(
    "https://dynamic-site.com",
    headless=True
)

# 等待特定元素
response = await server.fetch(
    "https://dynamic-site.com",
    wait_selector="div.product-list",
    wait_selector_state="visible"
)

# 页面交互
def page_action(page):
    page.click("button.load-more")
    page.wait_for_selector("div.more-content")

response = await server.fetch(
    "https://dynamic-site.com",
    page_action=page_action
)

# 使用本地浏览器
response = await server.fetch(
    "https://dynamic-site.com",
    real_chrome=True,
    headless=False
)

# 性能优化
response = await server.fetch(
    "https://dynamic-site.com",
    disable_resources=True,
    blocked_domains={
        "analytics.com",
        "tracking.com"
    }
)
```

#### bulk_fetch(urls, **kwargs)

批量通过浏览器获取多个 URL。参数与 `fetch()` 相同，但接受 URL 列表。

**参数**：

与 `fetch()` 相同，但：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|---------|------|
| `urls` | list[str] | - | 要获取的 URL 列表 |

**返回值**：`ResponseModel` 对象列表

**示例**：

```python
# 批量动态抓取
urls = [
    "https://dynamic-site.com/page1",
    "https://dynamic-site.com/page2",
    "https://dynamic-site.com/page3"
]

responses = await server.bulk_fetch(urls)

for response in responses:
    print(f"{response.url}: {response.status}")
```

#### stealthy_fetch(url, **kwargs)

通过浏览器获取 URL，使用高级反机器人绕过功能。适用于具有严格防护机制的网站。

**参数**：

与 `fetch()` 相同的参数，加上：

- 所有 DynamicFetcher 的参数
- 所有 `fetch()` 的内容提取参数

**额外特性**：

- 浏览器指纹伪装
- WebSocket 轮询拦截
- 真实浏览器行为模拟
- 反检测增强

**示例**：

```python
# 基本隐身抓取
response = await server.stealthy_fetch(
    "https://protected-site.com",
    headless=True
)

# 使用代理
response = await server.stealthy_fetch(
    "https://protected-site.com",
    proxy="http://proxy:8080"
)

# 等待页面加载
response = await server.stealthy_fetch(
    "https://protected-site.com",
    network_idle=True,
    load_dom=True
)
```

#### bulk_stealthy_fetch(urls, **kwargs)

批量通过浏览器获取多个 URL，使用高级反机器人绕过功能。参数与 `stealthy_fetch()` 相同，但接受 URL 列表。

**参数**：

与 `stealthy_fetch()` 相同，但：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|---------|------|
| `urls` | list[str] | - | 要获取的 URL 列表 |

**返回值**：`ResponseModel` 对象列表

**示例**：

```python
# 批量隐身抓取
urls = [
    "https://protected-site.com/page1",
    "https://protected-site.com/page2",
    "https://protected-site.com/page3"
]

responses = await server.bulk_stealthy_fetch(urls)

for response in responses:
    print(f"{response.url}: {response.status}")
```

#### serve(http=False, host="0.0.0.0", port=8000)

启动 MCP 服务器。

**参数说明**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|---------|------|
| `http` | bool | False | 是否启用 HTTP 模式 |
| `host` | str | '0.0.0.0' | 服务器主机地址 |
| `port` | int | 8000 | 服务器端口 |

**示例**：

```python
# 基本启动
server = ScraplingMCPServer()
server.serve()

# 自定义配置
server = ScraplingMCPServer()
server.serve(http=False, host="0.0.0.0", port=8000)
```

## 使用场景示例

### 静态网站抓取

```python
from scrapling.core.ai import ScraplingMCPServer

server = ScraplingMCPServer()

# 抓取静态网站
response = server.get(
    "https://example.com",
    extraction_type="markdown"
)

print(response.content)
```

### 动态网站抓取

```python
# 抓取需要 JavaScript 的网站
response = await server.fetch(
    "https://dynamic-site.com",
    headless=True,
    load_dom=True
)

print(response.content)
```

### 高防护网站抓取

```python
# 抓取有反机器人保护的网站
response = await server.stealthy_fetch(
    "https://protected-site.com",
    headless=True
)

print(response.content)
```

### 批量抓取

```python
# 批量抓取多个 URL
urls = [
    "https://example.com/page1",
    "https://example.com/page2",
    "https://example.com/page3"
]

responses = await server.bulk_get(urls)

for response in responses:
    print(f"{response.url}: {response.status}")
```

### 提取特定内容

```python
# 使用 CSS 选择器提取特定内容
response = server.get(
    "https://example.com",
    css_selector="div.article h2",
    extraction_type="text",
    main_content_only=True
)

print(response.content)
```

### 使用代理

```python
# 使用代理抓取
response = server.get(
    "https://example.com",
    proxy="http://proxy:8080",
    proxy_auth={
        "username": "user",
        "password": "pass"
    }
)
```

### 页面交互

```python
# 执行页面操作
def page_action(page):
    # 滚动到页面底部
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    
    # 点击按钮
    page.click("button.load-more")
    
    # 等待内容加载
    page.wait_for_selector("div.more-content")

response = await server.fetch(
    "https://dynamic-site.com",
    page_action=page_action
)
```

### 与 AI 集成

MCP 服务器专门设计用于与 AI 助手集成：

```python
# AI 助手可以使用 MCP 工具
# 例如：Claude Desktop 或其他支持 MCP 的 AI

# AI 调用示例
"""
AI: "抓取 https://example.com 的内容"

MCP 服务器自动调用:
server.get("https://example.com", extraction_type="markdown")

返回结果给 AI，AI 可以：
- 分析内容
- 回答问题
- 提取特定信息
- 执行后续操作
"""
```

## 工具选择指南

### 何时使用 get()

- 静态网站
- 低防护级别的网站
- 不需要 JavaScript 执行的页面
- 快速批量抓取

### 何时使用 fetch()

- 动态网站（需要 JavaScript）
- SPA (单页应用)
- 需要等待页面加载的网站
- 需要执行简单的页面交互

### 何时使用 stealthy_fetch()

- 高防护级别的网站
- 有反机器人机制的网站
- 需要高级浏览器伪装的网站
- 对检测敏感的抓取任务

## 性能优化

### 批量操作

```python
# 使用批量方法提高效率
responses = await server.bulk_get(urls)

# 而不是循环调用单个请求
# responses = [server.get(url) for url in urls]
```

### 资源优化

```python
# 对于动态抓取，禁用不必要的资源
response = await server.fetch(
    "https://example.com",
    disable_resources=True,
    blocked_domains={
        "analytics.com",
        "ads.com"
    }
)
```

### 内容提取

```python
# 只提取需要的内容
response = server.get(
    "https://example.com",
    css_selector="div.content",
    main_content_only=True
)
```

## 错误处理

```python
from scrapling.core.ai import ScraplingMCPServer

server = ScraplingMCPServer()

try:
    response = server.get(
        "https://example.com",
        timeout=30,
        retries=3
    )
    
    if response.status != 200:
        print(f"请求失败: {response.status}")
    else:
        print(f"成功: {len(response.content)} 字符")
        
except Exception as e:
    print(f"错误: {e}")
```

## 总结

Scrapling MCP 服务器提供了六个强大的工具：

1. **get()**: 静态网站抓取（低防护）
2. **bulk_get()**: 批量静态抓取
3. **fetch()**: 动态网站抓取（中等防护）
4. **bulk_fetch()**: 批量动态抓取
5. **stealthy_fetch()**: 高防护网站抓取（高级反机器人）
6. **bulk_stealthy_fetch()**: 批量高防护抓取

**核心特性**：

- 标准化的响应格式 (`ResponseModel`)
- 灵活的内容提取（Markdown/HTML/Text）
- 完整的代理和认证支持
- 高级反机器人绕过能力
- 与 AI 助手无缝集成
- 批量操作支持
- 自定义头部和 Cookies

MCP 服务器使 Scrapling 的功能可以通过标准化的 MCP 协议被 AI 系统使用，实现了对话式的网页抓取体验。

---

# 自定义类型 API 参考 (Custom Types API Reference)

## 概述

Scrapling 实现了三个自定义类型，用于增强文本处理和属性操作能力：

1. **TextHandler**: 扩展 Python 字符串类，增加数据处理、文本处理和正则匹配功能
2. **TextHandlers**: TextHandler 的集合/列表版本，用于批量处理多个文本
3. **AttributesHandler**: 处理元素属性，提供属性访问、搜索和检查功能

这些类型与 Selector 类紧密集成，提供了更强大和灵活的数据提取能力。

---

## TextHandler 类

### 基本介绍

`TextHandler` 是 `str` (Python 字符串) 的子类，继承所有字符串方法，并添加了 Scrapling 特有的功能，包括数据提取、文本处理和正则匹配。

**基类**：`str`

**特点**：
- 完全兼容标准 Python 字符串操作
- 增强的文本处理能力
- 内置正则表达式匹配
- 数据提取和清理功能

### 初始化

```python
from scrapling.core.types import TextHandler

text = TextHandler("Hello World")
```

TextHandler 可以从任何 Python 字符串创建，保持所有字符串特性。

### 继承的字符串方法

由于 TextHandler 继承自 `str`，所有标准字符串方法都可用：

```python
text = TextHandler("Hello World")

# 标准字符串操作
print(text.upper())           # "HELLO WORLD"
print(text.lower())           # "hello world"
print(text.replace("World", "Python"))  # "Hello Python"
print(len(text))              # 11
```

### 主要方法

#### split(sep=None, maxsplit=-1)

分割文本，返回 `TextHandlers` 对象。

**参数**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|---------|------|
| `sep` | str | None | 分隔符。如果为 None，则使用空白字符分割 |
| `maxsplit` | int | -1 | 最大分割次数。-1 表示无限制 |

**返回值**：`TextHandlers` 对象（TextHandler 的列表）

**示例**：

```python
from scrapling.core.types import TextHandler

text = TextHandler("apple,banana,cherry,dates")

# 使用逗号分割
result = text.split(",")
print(result)  # TextHandlers(['apple', 'banana', 'cherry', 'dates'])

# 限制分割次数
result = text.split(",", maxsplit=2)
print(result)  # TextHandlers(['apple', 'banana', 'cherry,dates'])

# 默认空白分割
text2 = TextHandler("Hello    World   Python")
result = text2.split()
print(result)  # TextHandlers(['Hello', 'World', 'Python'])
```

#### strip(chars=None)

移除字符串两端的空白字符（或指定字符）。

**参数**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|---------|------|
| `chars` | str | None | 要移除的字符集合。如果为 None，则移除空白字符 |

**返回值**：`TextHandler` 对象

**示例**：

```python
text = TextHandler("   Hello World   ")

# 移除两端空白
result = text.strip()
print(result)  # "Hello World"

# 移除指定字符
text2 = TextHandler("xxxHello Worldxxx")
result = text2.strip("x")
print(result)  # "Hello World"

# 移除多个字符
text3 = TextHandler("##Hello World##")
result = text3.strip("#")
print(result)  # "Hello World"
```

#### lstrip(chars=None)

移除字符串左端的空白字符（或指定字符）。

**参数**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|---------|------|
| `chars` | str | None | 要移除的字符集合 |

**返回值**：`TextHandler` 对象

**示例**：

```python
text = TextHandler("   Hello World   ")

result = text.lstrip()
print(result)  # "Hello World   "

text2 = TextHandler("xxxHello World")
result = text2.lstrip("x")
print(result)  # "Hello World"
```

#### rstrip(chars=None)

移除字符串右端的空白字符（或指定字符）。

**参数**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|---------|------|
| `chars` | str | None | 要移除的字符集合 |

**返回值**：`TextHandler` 对象

**示例**：

```python
text = TextHandler("   Hello World   ")

result = text.rstrip()
print(result)  # "   Hello World"

text2 = TextHandler("Hello Worldxxx")
result = text2.rstrip("x")
print(result)  # "Hello World"
```

#### clean(remove_entities=False)

清理文本，去除多余空白和换行符，使文本更整洁。

**参数**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|---------|------|
| `remove_entities` | bool | False | 是否移除 HTML 实体（如 `&nbsp;`、`&amp;` 等） |

**返回值**：`TextHandler` 对象

**示例**：

```python
from scrapling.core.types import TextHandler

text = TextHandler("   Hello    \n\n   World   \n\n   Python   ")

# 基本清理
result = text.clean()
print(result)  # "Hello World Python"

# 移除 HTML 实体
text2 = TextHandler("Hello&nbsp;World&nbsp;&nbsp;&amp;&nbsp;Python")
result = text2.clean(remove_entities=True)
print(result)  # "Hello World & Python"
```

#### json()

解析 JSON 格式的文本，返回 Python 对象。

**返回值**：解析后的 Python 对象（字典、列表等）

**示例**：

```python
from scrapling.core.types import TextHandler

# 解析 JSON 对象
text = TextHandler('{"name": "John", "age": 30, "city": "New York"}')
data = text.json()
print(data)  # {'name': 'John', 'age': 30, 'city': 'New York'}

# 解析 JSON 数组
text2 = TextHandler('[1, 2, 3, 4, 5]')
data2 = text2.json()
print(data2)  # [1, 2, 3, 4, 5]

# 解析嵌套 JSON
text3 = TextHandler('{"users": [{"name": "Alice"}, {"name": "Bob"}]}')
data3 = text3.json()
print(data3)  # {'users': [{'name': 'Alice'}, {'name': 'Bob'}]}
```

**注意**：如果文本不是有效的 JSON 格式，会抛出 `json.JSONDecodeError` 异常。

#### re(pattern, flags=0)

使用正则表达式匹配文本，返回所有匹配结果。

**参数**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|---------|------|
| `pattern` | str | - | 正则表达式模式 |
| `flags` | int | 0 | 正则表达式标志（如 re.IGNORECASE, re.MULTILINE 等） |

**返回值**：匹配结果的列表

**示例**：

```python
from scrapling.core.types import TextHandler
import re

text = TextHandler("Email: user1@example.com, user2@example.com, user3@test.org")

# 提取所有邮箱
emails = text.re(r'\w+@\w+\.\w+')
print(emails)  # ['user1@example.com', 'user2@example.com', 'user3@test.org']

# 忽略大小写
text2 = TextHandler("Hello HELLO hello")
result = text2.re(r'hello', flags=re.IGNORECASE)
print(result)  # ['Hello', 'HELLO', 'hello']

# 提取数字
text3 = TextHandler("Numbers: 123, 456, 789")
numbers = text3.re(r'\d+')
print(numbers)  # ['123', '456', '789']

# 提取 URL
text4 = TextHandler("Visit https://example.com and http://test.org")
urls = text4.re(r'https?://\w+\.\w+')
print(urls)  # ['https://example.com', 'http://test.org']
```

#### re_first(pattern, default=None, flags=0)

使用正则表达式匹配文本，返回第一个匹配结果。

**参数**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|---------|------|
| `pattern` | str | - | 正则表达式模式 |
| `default` | Any | None | 如果没有匹配，返回的默认值 |
| `flags` | int | 0 | 正则表达式标志 |

**返回值**：第一个匹配结果，或默认值

**示例**：

```python
from scrapling.core.types import TextHandler

text = TextHandler("Email: user1@example.com, user2@example.com")

# 提取第一个邮箱
first_email = text.re_first(r'\w+@\w+\.\w+')
print(first_email)  # "user1@example.com"

# 使用默认值
text2 = TextHandler("No emails here")
result = text2.re_first(r'\w+@\w+\.\w+', default="Not found")
print(result)  # "Not found"

# 提取第一个数字
text3 = TextHandler("Numbers: 123, 456, 789")
first_number = text3.re_first(r'\d+')
print(first_number)  # "123"
```

#### get()

获取文本内容。对于 TextHandler，这等同于返回字符串本身。

**返回值**：字符串（TextHandler 对象）

**示例**：

```python
from scrapling.core.types import TextHandler

text = TextHandler("Hello World")

result = text.get()
print(result)  # "Hello World"
```

#### get_all()

获取所有文本内容。对于单个 TextHandler，返回包含该文本的列表。

**返回值**：包含文本的列表

**示例**：

```python
from scrapling.core.types import TextHandler

text = TextHandler("Hello World")

result = text.get_all()
print(result)  # ['Hello World']
```

### 在 Selector 中的使用

TextHandler 在 Selector 类中被广泛使用，用于文本提取：

```python
from scrapling.fetchers import Fetcher

response = Fetcher.fetch("https://example.com")

# Selector.text 返回 TextHandler 对象
text = response.text
print(type(text))  # <class 'scrapling.core.types.TextHandler'>

# 可以直接使用 TextHandler 的方法
clean_text = text.clean()
emails = text.re(r'\w+@\w+\.\w+')
```

### 与标准字符串的比较

```python
from scrapling.core.types import TextHandler

# TextHandler 是 str 的子类
text = TextHandler("Hello World")

print(isinstance(text, str))  # True

# 可以用于任何期望字符串的地方
print(text.upper())  # "HELLO WORLD"
print(text.replace("World", "Python"))  # "Hello Python"

# 额外功能
emails = text.re(r'\w+')  # ['Hello', 'World']
```

---

## TextHandlers 类

### 基本介绍

`TextHandlers` 是 `TextHandler` 对象的集合/列表，用于批量处理多个文本字符串。它继承自 `list`，并提供了批量文本处理的方法。

**基类**：`list`

**特点**：
- 批量文本处理
- 支持列表操作
- 集合方法支持
- 与 Selector 配合使用

### 初始化

TextHandlers 通常不是直接创建的，而是通过 Selector 或 TextHandler 的方法返回：

```python
from scrapling.fetchers import Fetcher

response = Fetcher.fetch("https://example.com")

# Selector.css() 或 Selector.xpath() 返回 Selectors
# Selectors 的某些方法返回 TextHandlers
texts = response.css('div.title::text')
# texts 是 TextHandlers 对象
```

### 主要方法

#### extract()

提取所有文本，返回标准 Python 字符串列表。

**返回值**：字符串列表

**示例**：

```python
from scrapling.fetchers import Fetcher

response = Fetcher.fetch("https://example.com")

# 获取所有标题文本
titles = response.css('h1::text')
# titles 是 TextHandlers 对象

# 提取为标准列表
title_list = titles.extract()
print(title_list)  # ['Title 1', 'Title 2', 'Title 3']
print(type(title_list))  # <class 'list'>
```

#### extract_first(default=None)

提取第一个文本，如果不存在则返回默认值。

**参数**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|---------|------|
| `default` | Any | None | 如果没有元素，返回的默认值 |

**返回值**：第一个文本字符串，或默认值

**示例**：

```python
from scrapling.fetchers import Fetcher

response = Fetcher.fetch("https://example.com")

# 获取第一个标题
titles = response.css('h1::text')

first_title = titles.extract_first()
print(first_title)  # "Main Title"

# 使用默认值
not_found = response.css('h999::text').extract_first(default="Not found")
print(not_found)  # "Not found"
```

#### get()

获取第一个文本，等同于 `extract_first()`。

**返回值**：第一个文本字符串

**示例**：

```python
titles = response.css('h1::text')

first = titles.get()
print(first)  # "Main Title"
```

#### get_all()

获取所有文本，等同于 `extract()`。

**返回值**：字符串列表

**示例**：

```python
titles = response.css('h1::text')

all_titles = titles.get_all()
print(all_titles)  # ['Title 1', 'Title 2', 'Title 3']
```

#### re(pattern)

对集合中的所有文本应用正则表达式匹配。

**参数**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|---------|------|
| `pattern` | str | - | 正则表达式模式 |

**返回值**：所有匹配结果的列表

**示例**：

```python
from scrapling.fetchers import Fetcher

response = Fetcher.fetch("https://example.com")

# 获取包含数字的文本
texts = response.css('div.number::text')

# 提取所有数字
numbers = texts.re(r'\d+')
print(numbers)  # ['123', '456', '789', ...]
```

### 列表操作

TextHandlers 支持所有标准列表操作：

```python
from scrapling.fetchers import Fetcher

response = Fetcher.fetch("https://example.com")

texts = response.css('h1::text')

# 切片
print(texts[0])  # 第一个文本
print(texts[1:3])  # 第二到第三个

# 遍历
for text in texts:
    print(text)

# 长度
print(len(texts))

# 检查存在
if texts:
    print("Found texts")

# 排序
sorted_texts = sorted(texts)

# 过滤
long_texts = [t for t in texts if len(t) > 10]
```

### 与 Selector 的集成

TextHandlers 是 Selector 查询结果的常见返回类型：

```python
# 获取多个元素的文本
texts = response.css('div.item::text')

# TextHandlers 对象
print(type(texts))  # <class 'scrapling.core.types.TextHandlers'>

# 可以使用列表索引
print(texts[0])

# 可以使用列表方法
print(texts[:3])

# 可以使用 TextHandlers 特有方法
all_texts = texts.get_all()
first_text = texts.extract_first()

# 可以在列表推导式中使用
processed = [t.strip().lower() for t in texts]
```

---

## AttributesHandler 类

### 基本介绍

`AttributesHandler` 类用于处理 HTML 元素的属性，提供属性访问、搜索和检查功能。它是一个字典的增强版本，专门用于属性操作。

**基类**：内部实现，不直接继承标准类型

**特点**：
- 灵活的属性访问
- 属性搜索功能
- JSON 序列化支持
- 属性存在检查

### 初始化

AttributesHandler 通常不是直接创建的，而是通过 Selector 的属性访问返回：

```python
from scrapling.fetchers import Fetcher

response = Fetcher.fetch("https://example.com")

# Selector.attrib 返回 AttributesHandler
element = response.css('div.container')
attrs = element.attrib  # AttributesHandler 对象
```

### 主要方法

#### __getitem__(key)

使用键访问属性值，支持字典式访问。

**参数**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|---------|------|
| `key` | str | - | 属性名称 |

**返回值**：属性值（字符串）

**异常**：`KeyError` - 如果属性不存在

**示例**：

```python
from scrapling.fetchers import Fetcher

response = Fetcher.fetch("https://example.com")

link = response.css('a.external')

# 访问 href 属性
url = link.attrib['href']
print(url)  # "https://example.com/page"

# 访问 class 属性
classes = link.attrib['class']
print(classes)  # "external link blue"

# 访问 title 属性
title = link.attrib['title']
print(title)  # "Click to visit"

# 不存在的属性会引发 KeyError
try:
    target = link.attrib['target']
except KeyError as e:
    print(f"Attribute not found: {e}")
```

#### get(key, default=None)

获取属性值，支持默认值。

**参数**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|---------|------|
| `key` | str | - | 属性名称 |
| `default` | Any | None | 如果属性不存在，返回的默认值 |

**返回值**：属性值，或默认值

**示例**：

```python
from scrapling.fetchers import Fetcher

response = Fetcher.fetch("https://example.com")

link = response.css('a.external')

# 获取存在的属性
url = link.attrib.get('href')
print(url)  # "https://example.com/page"

# 获取不存在的属性，使用默认值
target = link.attrib.get('target', '_self')
print(target)  # "_self"

# 检查属性是否存在
if link.attrib.get('data-id'):
    print("Element has data-id")
else:
    print("Element does not have data-id")
```

#### search_values(pattern)

在所有属性值中搜索匹配指定模式（正则表达式）的值。

**参数**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|---------|------|
| `pattern` | str | - | 正则表达式模式 |

**返回值**：匹配的属性值列表

**示例**：

```python
from scrapling.fetchers import Fetcher

response = Fetcher.fetch("https://example.com")

element = response.css('div')

# 搜索包含 URL 的属性
urls = element.attrib.search_values(r'https?://\w+')
print(urls)  # ['https://example.com', 'https://cdn.example.com']

# 搜索包含数字的属性
numbers = element.attrib.search_values(r'\d+')
print(numbers)  # ['123', '456', '789']

# 搜索包含特定域名的属性
domain_links = element.attrib.search_values(r'example\.com')
print(domain_links)  # 所有包含 example.com 的属性值
```

#### json_string

返回属性字典的 JSON 格式字符串。

**返回值**：JSON 字符串

**示例**：

```python
from scrapling.fetchers import Fetcher

response = Fetcher.fetch("https://example.com")

element = response.css('div')

# 获取 JSON 格式的属性
json_attrs = element.attrib.json_string
print(json_attrs)  # '{"href": "https://example.com", "class": "container", "id": "main"}'

# 可以解析回 Python 对象
import json
attrs_dict = json.loads(json_attrs)
print(attrs_dict)  # {'href': 'https://example.com', 'class': 'container', 'id': 'main'}
```

#### __contains__(key)

检查属性是否存在。

**参数**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|---------|------|
| `key` | str | - | 属性名称 |

**返回值**：布尔值

**示例**：

```python
from scrapling.fetchers import Fetcher

response = Fetcher.fetch("https://example.com")

link = response.css('a.external')

# 检查属性存在性
if 'href' in link.attrib:
    print("Link has href")

if 'data-id' not in link.attrib:
    print("Link does not have data-id")

# 条件访问
if 'target' in link.attrib:
    target = link.attrib['target']
else:
    target = '_self'

print(target)  # "_self"
```

### 字典式访问

AttributesHandler 支持标准字典操作：

```python
from scrapling.fetchers import Fetcher

response = Fetcher.fetch("https://example.com")

element = response.css('div')
attrs = element.attrib

# 遍历所有属性
for key, value in attrs.items():
    print(f"{key}: {value}")

# 获取所有属性名
attr_names = attrs.keys()
print(list(attr_names))  # ['href', 'class', 'id', 'data-value']

# 获取所有属性值
attr_values = attrs.values()
print(list(attr_values))

# 获取所有属性对
attr_items = attrs.items()
print(list(attr_items))

# 转换为标准字典
attrs_dict = dict(attrs)
print(attrs_dict)  # {'href': 'https://example.com', 'class': 'container', ...}
```

### 在 Selector 中的使用

AttributesHandler 是 Selector.attrib 的返回类型：

```python
from scrapling.fetchers import Fetcher

response = Fetcher.fetch("https://example.com")

# 获取单个元素
link = response.css('a.external')

# 访问属性
url = link.attrib['href']
classes = link.attrib.get('class', '')

# 搜索属性
all_urls = link.attrib.search_values(r'https?://\w+')

# 检查属性
if 'data-id' in link.attrib:
    data_id = link.attrib['data-id']

# JSON 输出
json_attrs = link.attrib.json_string
```

### 批量处理多个元素的属性

```python
from scrapling.fetchers import Fetcher

response = Fetcher.fetch("https://example.com")

# 获取所有链接
links = response.css('a')

# 提取所有 href
hrefs = [link.attrib['href'] for link in links if 'href' in link.attrib]

# 提取带有特定属性的元素
external_links = [
    link for link in links
    if 'target' in link.attrib and link.attrib['target'] == '_blank'
]

# 提取所有 data-* 属性
data_attrs = []
for link in links:
    for key, value in link.attrib.items():
        if key.startswith('data-'):
            data_attrs.append((key, value))
```

---

## 综合示例

### 示例 1：文本处理链

```python
from scrapling.fetchers import Fetcher

response = Fetcher.fetch("https://example.com")

# 获取标题文本
title = response.css('h1::text')

# 使用 TextHandler 清理和提取
clean_title = title.clean()
words = clean_title.re(r'\w+')

print(clean_title)  # "Clean Title"
print(words)  # ['Clean', 'Title']
```

### 示例 2：批量文本处理

```python
from scrapling.fetchers import Fetcher

response = Fetcher.fetch("https://example.com")

# 获取所有段落
paragraphs = response.css('p::text')

# TextHandlers 批量操作
all_text = paragraphs.extract()
first_text = paragraphs.extract_first()

# 清理所有文本
clean_paragraphs = [p.clean() for p in paragraphs]

# 提取所有邮箱
all_emails = paragraphs.re(r'\w+@\w+\.\w+')

print(all_emails)
```

### 示例 3：属性提取和搜索

```python
from scrapling.fetchers import Fetcher

response = Fetcher.fetch("https://example.com")

# 获取所有链接
links = response.css('a')

# 提取所有 href
hrefs = [link.attrib.get('href') for link in links]

# 搜索包含特定域名的属性
for link in links:
    matching = link.attrib.search_values(r'example\.com')
    if matching:
        print(f"Found: {matching}")

# 检查外部链接
external = []
for link in links:
    if 'target' in link.attrib and link.attrib['target'] == '_blank':
        external.append(link.attrib['href'])

print(external)
```

### 示例 4：数据提取管道

```python
from scrapling.fetchers import Fetcher
import re

response = Fetcher.fetch("https://example.com")

# 提取文章数据
article = {
    'title': response.css('h1::text').get(),
    'content': response.css('div.content p::text').get_all(),
    'author': response.css('span.author::text').get(),
    'tags': response.css('meta[name="keywords"]::attr(content)').get().split(','),
    'published': response.css('time::attr(datetime)').get(),
    'canonical': response.css('link[rel="canonical"]::attr(href)').get()
}

# 清理数据
article['title'] = article['title'].clean() if article['title'] else ''
article['content'] = [p.clean() for p in article['content']]
article['tags'] = [tag.strip() for tag in article['tags']]

# 提取邮箱
emails = response.re(r'\w+@\w+\.\w+')

# 提取所有外部链接
external_links = [
    link.attrib['href']
    for link in response.css('a')
    if 'href' in link.attrib and 'example.com' not in link.attrib['href']
]

print(article)
print(emails)
print(external_links)
```

### 示例 5：复杂文本处理

```python
from scrapling.fetchers import Fetcher

response = Fetcher.fetch("https://example.com")

# 获取价格文本
price_text = response.css('span.price::text')

# 提取数字价格
price_numbers = price_text.re(r'\d+\.?\d*')

# 获取第一个价格
first_price = price_text.re_first(r'\d+\.?\d*')

# 清理文本
clean_price = price_text.clean()

# 处理多个价格
prices = []
for price in response.css('span.price::text'):
    # 清理并提取数字
    clean = price.clean()
    number = clean.re_first(r'\d+\.?\d*')
    if number:
        prices.append(float(number))

print(prices)  # [19.99, 29.99, 39.99]
```

## 类型关系图

```
str (Python 字符串)
  └─ TextHandler
       ├─ 继承所有字符串方法
       ├─ clean()
       ├─ json()
       ├─ re()
       └─ re_first()

list (Python 列表)
  └─ TextHandlers
       ├─ 继承所有列表方法
       ├─ extract()
       ├─ extract_first()
       ├─ get()
       ├─ get_all()
       └─ re()

dict (Python 字典)
  └─ AttributesHandler
       ├─ 支持字典式访问
       ├─ get()
       ├─ search_values()
       ├─ __contains__()
       └─ json_string
```

## 与 Selector 的集成

所有三种类型都与 Selector 类紧密集成：

```python
from scrapling.fetchers import Fetcher

response = Fetcher.fetch("https://example.com")

# Selector.text 返回 TextHandler
text = response.text
clean_text = text.clean()
emails = text.re(r'\w+@\w+\.\w+')

# Selector.css() 返回 Selectors
# Selectors 某些方法返回 TextHandlers
titles = response.css('h1::text')
all_titles = titles.extract()

# Selector.attrib 返回 AttributesHandler
element = response.css('div')
attrs = element.attrib
url = attrs.get('href', '')
```

## 总结

Scrapling 的三种自定义类型提供了强大的文本和属性处理能力：

1. **TextHandler**: 增强的字符串，支持清理、正则匹配、JSON 解析
2. **TextHandlers**: 批量文本处理，支持提取和正则操作
3. **AttributesHandler**: 属性处理，支持访问、搜索和检查

这些类型与 Selector 类无缝集成，为网页抓取和数据提取提供了简洁而强大的 API。通过使用这些自定义类型，可以更高效地处理和转换抓取到的数据。

---

# 代理轮换 API 参考 (Proxy Rotation API Reference)

## 概述

Scrapling 的代理轮换功能由 `ProxyRotator` 类提供，这是一个线程安全的代理轮换器，支持多种轮换策略，可以轻松集成到各种抓取器和会话中。

**核心特性**：

- 线程安全的设计
- 支持多种代理格式
- 可自定义轮换策略
- 与所有抓取器无缝集成
- 支持代理验证

---

## ProxyRotator 类

### 基本介绍

`ProxyRotator` 是 Scrapling 的代理轮换核心类，提供代理管理和轮换功能。它支持线程安全的代理访问，适用于并发抓取场景。

### 导入

```python
from scrapling.fetchers import ProxyRotator
```

### 初始化

#### ProxyRotator(proxies, strategy=cyclic_rotation)

创建代理轮换器实例。

**参数说明**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|---------|------|
| `proxies` | list | - | 代理列表，每个代理可以是字符串或字典格式 |
| `strategy` | callable | cyclic_rotation | 轮换策略函数，默认为循环轮换 |

**代理格式**：

1. **字符串格式**：
   ```python
   "http://proxy.example.com:8080"
   "http://user:password@proxy.example.com:8080"
   "socks5://proxy.example.com:1080"
   ```

2. **字典格式**（兼容 Playwright 代理格式）：
   ```python
   {
       "server": "http://proxy.example.com:8080",
       "username": "user",
       "password": "pass"
   }
   ```

**示例**：

```python
from scrapling.fetchers import ProxyRotator

# 字符串格式代理
rotator = ProxyRotator([
    "http://proxy1.example.com:8080",
    "http://proxy2.example.com:8080",
    "http://user:pass@proxy3.example.com:8080"
])

# 字典格式代理
rotator2 = ProxyRotator([
    {
        "server": "http://proxy1.example.com:8080",
        "username": "user1",
        "password": "pass1"
    },
    {
        "server": "http://proxy2.example.com:8080",
        "username": "user2",
        "password": "pass2"
    }
])

# 混合格式
rotator3 = ProxyRotator([
    "http://proxy1.example.com:8080",
    {
        "server": "http://proxy2.example.com:8080",
        "username": "user",
        "password": "pass"
    },
    "socks5://proxy3.example.com:1080"
])
```

### 属性

#### proxies

返回代理列表（只读）。

**类型**：`list`

**示例**：

```python
rotator = ProxyRotator(["http://proxy1:8080", "http://proxy2:8080"])

# 获取所有代理
print(rotator.proxies)
# ['http://proxy1:8080', 'http://proxy2:8080']

# 只读，不能直接修改
# rotator.proxies.append("http://proxy3:8080")  # 错误
```

### 方法

#### get_proxy()

获取下一个代理。

**返回值**：下一个代理（字符串或字典格式）

**说明**：
- 默认使用循环轮换策略
- 线程安全，可以在多线程环境中使用
- 每次调用返回列表中的下一个代理
- 到达列表末尾后，返回第一个代理

**示例**：

```python
from scrapling.fetchers import ProxyRotator

rotator = ProxyRotator([
    "http://proxy1:8080",
    "http://proxy2:8080",
    "http://proxy3:8080"
])

# 获取代理
proxy1 = rotator.get_proxy()
print(proxy1)  # "http://proxy1:8080"

proxy2 = rotator.get_proxy()
print(proxy2)  # "http://proxy2:8080"

proxy3 = rotator.get_proxy()
print(proxy3)  # "http://proxy3:8080"

# 循环回到开始
proxy4 = rotator.get_proxy()
print(proxy4)  # "http://proxy1:8080"
```

#### __len__()

返回代理数量。

**返回值**：`int` - 代理总数

**示例**：

```python
rotator = ProxyRotator([
    "http://proxy1:8080",
    "http://proxy2:8080",
    "http://proxy3:8080"
])

print(len(rotator))  # 3
```

---

## 轮换策略

### 默认策略：循环轮换 (cyclic_rotation)

默认的轮换策略按顺序循环使用代理。

**函数签名**：
```python
def cyclic_rotation(proxies, current_index):
    """
    循环轮换策略
    
    参数:
        proxies: 代理列表
        current_index: 当前索引
        
    返回:
        (proxy, next_index): 下一个代理和下一个索引
    """
```

**行为**：
- 从第一个代理开始
- 每次调用返回下一个代理
- 到达末尾后返回第一个代理

**示例**：

```python
from scrapling.fetchers import ProxyRotator

# 默认使用循环轮换
rotator = ProxyRotator([
    "http://proxy1:8080",
    "http://proxy2:8080",
    "http://proxy3:8080"
])

# 按顺序循环
for i in range(10):
    proxy = rotator.get_proxy()
    print(f"{i+1}: {proxy}")

# 输出：
# 1: http://proxy1:8080
# 2: http://proxy2:8080
# 3: http://proxy3:8080
# 4: http://proxy1:8080
# 5: http://proxy2:8080
# 6: http://proxy3:8080
# 7: http://proxy1:8080
# 8: http://proxy2:8080
# 9: http://proxy3:8080
# 10: http://proxy1:8080
```

### 自定义策略

您可以创建自定义的轮换策略。

**策略函数签名**：
```python
def custom_strategy(proxies, current_index):
    """
    自定义轮换策略
    
    参数:
        proxies: 代理列表
        current_index: 当前索引
        
    返回:
        (proxy, next_index): 下一个代理和下一个索引
    """
    # 您的轮换逻辑
    return proxy, next_index
```

#### 示例 1：随机轮换策略

```python
import random

def random_rotation(proxies, current_index):
    """随机选择代理"""
    proxy = random.choice(proxies)
    # 随机索引不影响轮换
    next_index = random.randint(0, len(proxies) - 1)
    return proxy, next_index

# 使用随机轮换
rotator = ProxyRotator(
    ["http://proxy1:8080", "http://proxy2:8080", "http://proxy3:8080"],
    strategy=random_rotation
)

for i in range(5):
    print(rotator.get_proxy())
# 每次随机返回一个代理
```

#### 示例 2：加权轮换策略

```python
import random

def weighted_rotation(proxies, current_index):
    """根据权重选择代理（示例）"""
    weights = [0.5, 0.3, 0.2]  # 对应每个代理的使用权重
    proxy = random.choices(proxies, weights=weights, k=1)[0]
    next_index = random.randint(0, len(proxies) - 1)
    return proxy, next_index

rotator = ProxyRotator(
    ["http://proxy1:8080", "http://proxy2:8080", "http://proxy3:8080"],
    strategy=weighted_rotation
)
```

#### 示例 3：智能轮换策略（基于成功率）

```python
class SmartRotation:
    def __init__(self):
        self.success_count = {}
        self.fail_count = {}
    
    def __call__(self, proxies, current_index):
        """基于成功率的智能轮换"""
        # 初始化统计
        for proxy in proxies:
            if proxy not in self.success_count:
                self.success_count[proxy] = 0
            if proxy not in self.fail_count:
                self.fail_count[proxy] = 0
        
        # 计算每个代理的成功率
        success_rates = []
        for proxy in proxies:
            total = self.success_count[proxy] + self.fail_count[proxy]
            if total == 0:
                rate = 1.0  # 未使用过，默认高优先级
            else:
                rate = self.success_count[proxy] / total
            success_rates.append(rate)
        
        # 选择成功率最高的代理
        proxy = proxies[success_rates.index(max(success_rates))]
        next_index = proxies.index(proxy)
        
        return proxy, next_index
    
    def record_success(self, proxy):
        """记录成功"""
        if proxy in self.success_count:
            self.success_count[proxy] += 1
    
    def record_failure(self, proxy):
        """记录失败"""
        if proxy in self.fail_count:
            self.fail_count[proxy] += 1

# 使用智能轮换
smart_rotator = SmartRotation()
rotator = ProxyRotator(
    ["http://proxy1:8080", "http://proxy2:8080", "http://proxy3:8080"],
    strategy=smart_rotator
)

# 记录成功和失败
proxy = rotator.get_proxy()
try:
    # 执行请求
    response = Fetcher.fetch("https://example.com", proxy=proxy)
    smart_rotator.record_success(proxy)
except Exception as e:
    smart_rotator.record_failure(proxy)
```

#### 示例 4：顺序轮换（不循环）

```python
def sequential_rotation(proxies, current_index):
    """顺序使用代理，不循环"""
    if current_index >= len(proxies) - 1:
        # 返回最后一个代理并保持索引
        return proxies[-1], len(proxies) - 1
    else:
        return proxies[current_index + 1], current_index + 1

rotator = ProxyRotator(
    ["http://proxy1:8080", "http://proxy2:8080", "http://proxy3:8080"],
    strategy=sequential_rotation
)

# 只会轮换 3 次
for i in range(5):
    print(rotator.get_proxy())

# 输出：
# http://proxy2:8080
# http://proxy3:8080
# http://proxy3:8080
# http://proxy3:8080
# http://proxy3:8080
```

---

## 与抓取器集成

### 与 Fetcher 集成

```python
from scrapling.fetchers import Fetcher, ProxyRotator

# 创建代理轮换器
rotator = ProxyRotator([
    "http://proxy1:8080",
    "http://proxy2:8080",
    "http://proxy3:8080"
])

# 使用代理发送请求
for url in urls:
    proxy = rotator.get_proxy()
    response = Fetcher.fetch(url, proxy=proxy)
    print(f"Used proxy: {proxy}, Status: {response.status}")
```

### 与 AsyncFetcher 集成

```python
import asyncio
from scrapling.fetchers import AsyncFetcher, ProxyRotator

async def fetch_with_proxies(urls):
    rotator = ProxyRotator([
        "http://proxy1:8080",
        "http://proxy2:8080"
    ])
    
    async with AsyncFetcher() as fetcher:
        tasks = []
        for url in urls:
            proxy = rotator.get_proxy()
            task = fetcher.fetch(url, proxy=proxy)
            tasks.append(task)
        
        responses = await asyncio.gather(*tasks)
        return responses

# 使用
urls = ["https://example.com/page1", "https://example.com/page2"]
responses = asyncio.run(fetch_with_proxies(urls))
```

### 与 DynamicFetcher 集成

```python
from scrapling.fetchers import DynamicFetcher, ProxyRotator

rotator = ProxyRotator([
    "http://proxy1:8080",
    "http://proxy2:8080"
])

# 动态抓取使用代理
proxy = rotator.get_proxy()
response = DynamicFetcher.fetch(
    "https://dynamic-site.com",
    proxy=proxy,
    headless=True
)
```

### 与 StealthyFetcher 集成

```python
from scrapling.fetchers import StealthyFetcher, ProxyRotator

rotator = ProxyRotator([
    "http://proxy1:8080",
    {"server": "http://proxy2:8080", "username": "user", "password": "pass"}
])

# 隐身抓取使用代理
proxy = rotator.get_proxy()
response = StealthyFetcher.fetch(
    "https://protected-site.com",
    proxy=proxy
)
```

### 与 Spiders 集成

```python
from scrapling.spiders import Spider
from scrapling.fetchers import ProxyRotator

class ProxySpider(Spider):
    name = "proxy_spider"
    start_urls = ["https://example.com/page1", "https://example.com/page2"]
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 创建代理轮换器
        self.proxy_rotator = ProxyRotator([
            "http://proxy1:8080",
            "http://proxy2:8080",
            "http://proxy3:8080"
        ])
    
    async def parse(self, response):
        # 使用代理
        proxy = self.proxy_rotator.get_proxy()
        print(f"Using proxy: {proxy}")
        
        # 处理响应
        title = response.css('title::text').get()
        yield {'title': title, 'proxy_used': proxy}

# 运行爬虫
spider = ProxySpider()
result = spider.start()
```

---

## 会话管理中的代理轮换

### FetcherSession 集成

```python
from scrapling.fetchers import FetcherSession, ProxyRotator

# 创建代理轮换器
rotator = ProxyRotator([
    "http://proxy1:8080",
    "http://proxy2:8080",
    "http://proxy3:8080"
])

# 创建会话
session = FetcherSession()

# 在会话中使用代理轮换
for url in urls:
    proxy = rotator.get_proxy()
    response = session.get(url, proxy=proxy)
    print(f"Status: {response.status}, Proxy: {proxy}")
```

### StealthySession 集成

```python
from scrapling.fetchers import StealthySession, ProxyRotator

rotator = ProxyRotator([
    "http://proxy1:8080",
    "http://proxy2:8080"
])

with StealthySession() as session:
    for url in urls:
        proxy = rotator.get_proxy()
        response = session.get(url, proxy=proxy)
        print(f"Fetched {url} with {proxy}")
```

### DynamicSession 集成

```python
from scrapling.fetchers import DynamicSession, ProxyRotator

rotator = ProxyRotator([
    "http://proxy1:8080",
    "http://proxy2:8080"
])

with DynamicSession() as session:
    for url in urls:
        proxy = rotator.get_proxy()
        response = session.fetch(url, proxy=proxy)
        print(f"Fetched {url} with {proxy}")
```

---

## 高级用法

### 多线程环境使用

```python
import threading
from scrapling.fetchers import Fetcher, ProxyRotator

# 创建代理轮换器（线程安全）
rotator = ProxyRotator([
    "http://proxy1:8080",
    "http://proxy2:8080",
    "http://proxy3:8080",
    "http://proxy4:8080"
])

def worker(url):
    """工作线程函数"""
    # 每个线程获取代理（线程安全）
    proxy = rotator.get_proxy()
    
    try:
        response = Fetcher.fetch(url, proxy=proxy)
        print(f"Thread {threading.current_thread().name}: "
              f"URL {url}, Proxy {proxy}, Status {response.status}")
    except Exception as e:
        print(f"Error: {e}")

# 创建多个线程
urls = [
    "https://example.com/page1",
    "https://example.com/page2",
    "https://example.com/page3",
    "https://example.com/page4"
]

threads = []
for url in urls:
    thread = threading.Thread(target=worker, args=(url,))
    threads.append(thread)
    thread.start()

for thread in threads:
    thread.join()
```

### 代理池管理

```python
from scrapling.fetchers import ProxyRotator
import time

class ProxyPool:
    def __init__(self, proxies):
        self.rotator = ProxyRotator(proxies)
        self.proxy_stats = {
            proxy: {'success': 0, 'fail': 0, 'last_used': None}
            for proxy in proxies
        }
    
    def get_proxy(self):
        """获取代理并记录使用时间"""
        proxy = self.rotator.get_proxy()
        self.proxy_stats[proxy]['last_used'] = time.time()
        return proxy
    
    def record_success(self, proxy):
        """记录成功"""
        if proxy in self.proxy_stats:
            self.proxy_stats[proxy]['success'] += 1
    
    def record_failure(self, proxy):
        """记录失败"""
        if proxy in self.proxy_stats:
            self.proxy_stats[proxy]['fail'] += 1
    
    def get_stats(self):
        """获取统计信息"""
        return self.proxy_stats

# 使用代理池
proxies = [
    "http://proxy1:8080",
    "http://proxy2:8080",
    "http://proxy3:8080"
]

pool = ProxyPool(proxies)

# 抓取并记录
for url in urls:
    proxy = pool.get_proxy()
    try:
        response = Fetcher.fetch(url, proxy=proxy)
        pool.record_success(proxy)
    except Exception as e:
        pool.record_failure(proxy)

# 查看统计
stats = pool.get_stats()
for proxy, stat in stats.items():
    print(f"{proxy}: Success {stat['success']}, Fail {stat['fail']}")
```

### 代理验证

```python
from scrapling.fetchers import Fetcher, ProxyRotator
import concurrent.futures

def verify_proxy(proxy):
    """验证代理是否可用"""
    try:
        response = Fetcher.fetch("https://httpbin.org/ip", proxy=proxy, timeout=5)
        if response.status == 200:
            return proxy, True
        return proxy, False
    except Exception:
        return proxy, False

def get_valid_proxies(proxies):
    """获取可用代理"""
    valid_proxies = []
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(verify_proxy, proxy) for proxy in proxies]
        
        for future in concurrent.futures.as_completed(futures):
            proxy, is_valid = future.result()
            if is_valid:
                print(f"✓ Valid: {proxy}")
                valid_proxies.append(proxy)
            else:
                print(f"✗ Invalid: {proxy}")
    
    return valid_proxies

# 验证代理池
all_proxies = [
    "http://proxy1:8080",
    "http://proxy2:8080",
    "http://proxy3:8080",
    "http://proxy4:8080"
]

valid_proxies = get_valid_proxies(all_proxies)

# 使用验证通过的代理创建轮换器
if valid_proxies:
    rotator = ProxyRotator(valid_proxies)
    print(f"Created rotator with {len(rotator)} valid proxies")
else:
    print("No valid proxies available")
```

### 动态代理池

```python
from scrapling.fetchers import ProxyRotator, Fetcher
import time

class DynamicProxyPool:
    def __init__(self, initial_proxies):
        self.rotator = ProxyRotator(initial_proxies)
        self.bad_proxies = set()
        self.last_check = time.time()
        self.check_interval = 3600  # 每小时检查一次
    
    def get_proxy(self):
        """获取代理，跳过坏代理"""
        max_attempts = 10
        for _ in range(max_attempts):
            proxy = self.rotator.get_proxy()
            if proxy not in self.bad_proxies:
                return proxy
        return proxy
    
    def mark_bad(self, proxy):
        """标记坏代理"""
        self.bad_proxies.add(proxy)
        print(f"Marked as bad: {proxy}")
    
    def check_proxies(self):
        """定期检查代理"""
        if time.time() - self.last_check > self.check_interval:
            print("Checking proxies...")
            self.bad_proxies.clear()
            self.last_check = time.time()

# 使用动态代理池
pool = DynamicProxyPool([
    "http://proxy1:8080",
    "http://proxy2:8080",
    "http://proxy3:8080"
])

for url in urls:
    pool.check_proxies()
    proxy = pool.get_proxy()
    
    try:
        response = Fetcher.fetch(url, proxy=proxy)
        if response.status != 200:
            pool.mark_bad(proxy)
    except Exception as e:
        pool.mark_bad(proxy)
```

---

## 代理格式详解

### HTTP 代理

```python
# 基本 HTTP 代理
"http://proxy.example.com:8080"

# 带认证的 HTTP 代理
"http://username:password@proxy.example.com:8080"
```

### HTTPS 代理

```python
# 基本 HTTPS 代理
"https://proxy.example.com:8443"

# 带认证的 HTTPS 代理
"https://username:password@proxy.example.com:8443"
```

### SOCKS 代理

```python
# SOCKS5 代理
"socks5://proxy.example.com:1080"

# 带认证的 SOCKS5 代理
"socks5://username:password@proxy.example.com:1080"

# SOCKS4 代理
"socks4://proxy.example.com:1080"
```

### 字典格式（Playwright 兼容）

```python
{
    "server": "http://proxy.example.com:8080",
    "username": "user",
    "password": "pass"
}

# 仅服务器
{
    "server": "http://proxy.example.com:8080"
}

# SOCKS5
{
    "server": "socks5://proxy.example.com:1080",
    "username": "user",
    "password": "pass"
}
```

---

## 完整示例

### 示例 1：基础代理轮换

```python
from scrapling.fetchers import Fetcher, ProxyRotator

# 创建代理列表
proxies = [
    "http://proxy1.example.com:8080",
    "http://proxy2.example.com:8080",
    "http://proxy3.example.com:8080"
]

# 创建轮换器
rotator = ProxyRotator(proxies)

# 抓取多个 URL
urls = [
    "https://example.com/page1",
    "https://example.com/page2",
    "https://example.com/page3",
    "https://example.com/page4",
    "https://example.com/page5"
]

for url in urls:
    proxy = rotator.get_proxy()
    try:
        response = Fetcher.fetch(url, proxy=proxy)
        print(f"✓ {url[:30]}... | Proxy: {proxy} | Status: {response.status}")
    except Exception as e:
        print(f"✗ {url[:30]}... | Proxy: {proxy} | Error: {e}")
```

### 示例 2：多线程并发抓取

```python
import threading
from scrapling.fetchers import FetcherSession, ProxyRotator

proxies = [
    "http://proxy1.example.com:8080",
    "http://proxy2.example.com:8080",
    "http://proxy3.example.com:8080",
    "http://proxy4.example.com:8080"
]

rotator = ProxyRotator(proxies)

def worker(urls):
    """工作线程"""
    session = FetcherSession()
    
    for url in urls:
        proxy = rotator.get_proxy()
        try:
            response = session.get(url, proxy=proxy)
            print(f"Thread {threading.current_thread().name}: {response.status}")
        except Exception as e:
            print(f"Error: {e}")

# 分割 URL 给不同线程
urls = [f"https://example.com/page{i}" for i in range(1, 21)]
chunk_size = len(urls) // 4

threads = []
for i in range(4):
    start = i * chunk_size
    end = start + chunk_size if i < 3 else len(urls)
    thread = threading.Thread(
        target=worker,
        args=(urls[start:end],)
    )
    threads.append(thread)
    thread.start()

for thread in threads:
    thread.join()
```

### 示例 3：带重试的代理轮换

```python
from scrapling.fetchers import Fetcher, ProxyRotator

proxies = [
    "http://proxy1.example.com:8080",
    "http://proxy2.example.com:8080",
    "http://proxy3.example.com:8080"
]

rotator = ProxyRotator(proxies)

def fetch_with_retry(url, max_retries=3):
    """带重试的抓取"""
    for attempt in range(max_retries):
        proxy = rotator.get_proxy()
        try:
            response = Fetcher.fetch(url, proxy=proxy, timeout=10)
            if response.status == 200:
                return response
            print(f"Attempt {attempt + 1}: Status {response.status}, retrying...")
        except Exception as e:
            print(f"Attempt {attempt + 1}: Error {e}, retrying...")
    
    raise Exception(f"Failed after {max_retries} attempts")

# 使用
for url in urls:
    try:
        response = fetch_with_retry(url)
        print(f"Success: {url}")
    except Exception as e:
        print(f"Failed: {url} - {e}")
```

### 示例 4：代理性能监控

```python
from scrapling.fetchers import Fetcher, ProxyRotator
import time

proxies = [
    "http://proxy1.example.com:8080",
    "http://proxy2.example.com:8080",
    "http://proxy3.example.com:8080"
]

rotator = ProxyRotator(proxies)

class ProxyMonitor:
    def __init__(self):
        self.stats = {}
    
    def record(self, proxy, success, response_time):
        """记录代理性能"""
        if proxy not in self.stats:
            self.stats[proxy] = {
                'total': 0,
                'success': 0,
                'fail': 0,
                'total_time': 0
            }
        
        self.stats[proxy]['total'] += 1
        self.stats[proxy]['total_time'] += response_time
        
        if success:
            self.stats[proxy]['success'] += 1
        else:
            self.stats[proxy]['fail'] += 1
    
    def get_report(self):
        """生成报告"""
        print("\nProxy Performance Report:")
        print("-" * 60)
        print(f"{'Proxy':<40} {'Success':>8} {'Fail':>8} {'Avg Time':>10}")
        print("-" * 60)
        
        for proxy, stat in self.stats.items():
            avg_time = stat['total_time'] / stat['total'] if stat['total'] > 0 else 0
            print(f"{proxy[:40]:<40} {stat['success']:>8} {stat['fail']:>8} {avg_time:>10.2f}s")

monitor = ProxyMonitor()

for url in urls:
    proxy = rotator.get_proxy()
    start_time = time.time()
    
    try:
        response = Fetcher.fetch(url, proxy=proxy, timeout=10)
        elapsed = time.time() - start_time
        
        success = response.status == 200
        monitor.record(proxy, success, elapsed)
        
    except Exception as e:
        elapsed = time.time() - start_time
        monitor.record(proxy, False, elapsed)

monitor.get_report()
```

### 示例 5：智能代理选择

```python
from scrapling.fetchers import Fetcher, ProxyRotator

class SmartProxyRotator:
    def __init__(self, proxies):
        self.proxies = proxies
        self.stats = {proxy: {'success': 0, 'fail': 0} for proxy in proxies}
    
    def get_best_proxy(self):
        """获取成功率最高的代理"""
        best_proxy = None
        best_rate = -1
        
        for proxy, stats in self.stats.items():
            total = stats['success'] + stats['fail']
            if total == 0:
                rate = 1.0
            else:
                rate = stats['success'] / total
            
            if rate > best_rate:
                best_rate = rate
                best_proxy = proxy
        
        return best_proxy
    
    def record_result(self, proxy, success):
        """记录结果"""
        if success:
            self.stats[proxy]['success'] += 1
        else:
            self.stats[proxy]['fail'] += 1

# 使用智能轮换器
proxies = [
    "http://proxy1.example.com:8080",
    "http://proxy2.example.com:8080",
    "http://proxy3.example.com:8080"
]

smart_rotator = SmartProxyRotator(proxies)

for url in urls:
    proxy = smart_rotator.get_best_proxy()
    
    try:
        response = Fetcher.fetch(url, proxy=proxy, timeout=10)
        smart_rotator.record_result(proxy, response.status == 200)
        print(f"Success with {proxy}")
    except Exception as e:
        smart_rotator.record_result(proxy, False)
        print(f"Failed with {proxy}: {e}")
```

---

## 最佳实践

### 1. 代理验证

在使用代理之前，先验证代理的可用性：

```python
# 先验证，后使用
valid_proxies = get_valid_proxies(all_proxies)
if valid_proxies:
    rotator = ProxyRotator(valid_proxies)
```

### 2. 错误处理

始终处理代理失败的情况：

```python
proxy = rotator.get_proxy()
try:
    response = Fetcher.fetch(url, proxy=proxy)
    # 处理响应
except Exception as e:
    # 标记坏代理或重试
    print(f"Proxy {proxy} failed: {e}")
```

### 3. 限速

不要过度使用单个代理：

```python
import time

for url in urls:
    proxy = rotator.get_proxy()
    response = Fetcher.fetch(url, proxy=proxy)
    time.sleep(1)  # 每个请求间隔 1 秒
```

### 4. 监控性能

记录代理的成功率和响应时间：

```python
monitor = ProxyMonitor()
# ... 抓取并记录
monitor.get_report()
```

### 5. 代理池管理

定期检查和更新代理池：

```python
def update_proxy_pool():
    """定期更新代理池"""
    new_proxies = fetch_new_proxies()
    valid_proxies = get_valid_proxies(new_proxies)
    rotator = ProxyRotator(valid_proxies)
    return rotator
```

---

## 常见问题

### Q: 如何处理代理认证？

**A**: 使用包含用户名和密码的代理 URL 或字典格式：

```python
# 字符串格式
"http://username:password@proxy.example.com:8080"

# 字典格式
{
    "server": "http://proxy.example.com:8080",
    "username": "username",
    "password": "password"
}
```

### Q: 如何实现代理的热交换？

**A**: 创建新的 `ProxyRotator` 实例：

```python
# 旧轮换器
old_rotator = ProxyRotator(old_proxies)

# 添加新代理
new_proxies = old_proxies + ["http://new-proxy:8080"]

# 创建新轮换器
new_rotator = ProxyRotator(new_proxies)

# 使用新轮换器
rotator = new_rotator
```

### Q: 如何实现代理的负载均衡？

**A**: 使用随机轮换或自定义加权策略：

```python
import random

def random_rotation(proxies, current_index):
    """随机选择代理"""
    return random.choice(proxies), random.randint(0, len(proxies) - 1)

rotator = ProxyRotator(proxies, strategy=random_rotation)
```

### Q: 如何避免被目标网站封禁？

**A**:
1. 使用多个代理轮换
2. 控制请求频率（添加延迟）
3. 模拟真实用户行为
4. 使用隐身抓取器
5. 监控响应状态

```python
import time

for url in urls:
    proxy = rotator.get_proxy()
    response = Fetcher.fetch(url, proxy=proxy, impersonate="chrome")
    time.sleep(random.uniform(1, 3))  # 随机延迟
```

---

## 总结

Scrapling 的代理轮换功能提供了强大而灵活的代理管理能力：

**核心特性**：
- `ProxyRotator` 类：线程安全的代理轮换器
- 多种代理格式支持：字符串和字典格式
- 可自定义轮换策略：循环、随机、智能等
- 与所有抓取器无缝集成
- 支持多线程并发

**使用场景**：
- 规避 IP 封禁
- 提高抓取成功率
- 分布式抓取
- 负载均衡

**最佳实践**：
1. 验证代理可用性
2. 处理代理失败
3. 监控代理性能
4. 定期更新代理池
5. 控制请求频率

通过合理使用代理轮换功能，可以显著提高爬虫的稳定性和成功率。

---

