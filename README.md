# ZK_Data Fabric — 数据基座 ETL

面向药学领域的数据采集与处理流水线，支持网页爬取、本地 PDF 批量解析、AI 结构化提取、质量门控与人工审核回灌。

---

## 环境要求

- Python **3.11.9**

---

## 快速开始

### 1. 创建虚拟环境

```bash
python -m venv .venv
```

激活虚拟环境：

- Windows（CMD）：
  ```cmd
  .venv\Scripts\activate.bat
  ```
- Windows（PowerShell）：
  ```powershell
  .venv\Scripts\Activate.ps1
  ```
- macOS / Linux：
  ```bash
  source .venv/bin/activate
  ```

### 2. 安装依赖

```bash
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```



## 运行

### 完整流水线（爬取 + 本地 PDF + 导出审核）

```bash
python main_etl.py
```

### 仅爬取网页

```bash
python main_etl.py --mode crawl
```

### 仅处理本地 PDF

```bash
python main_etl.py --mode local
```

### 仅导出待审核 CSV

```bash
python main_etl.py --mode export_review
```

### 人工审核回灌

1. 运行 `export_review` 导出待审核 CSV
2. 用 Excel 打开 CSV，填写 `manual_score` 和 `reviewer_note` 列
3. 保存后执行回灌：

```bash
python feedback_ingest.py --csv /data/review/pending_review_20240315.csv
```

---

## 项目结构

```
├── main_etl.py              # ETL 主入口（Prefect 调度）
├── feedback_ingest.py       # 人工审核回灌脚本
├── requirements.txt         # 依赖清单
├── .env.example             # 环境变量模板
├── config/
│   ├── settings.py          # 全局配置（API Key、路径、阈值）
│   └── crawler_settings.py  # 爬虫站点配置
├── core_engine/             # 核心引擎（采集、解析、质检、存储）
├── biz_rules/               # 业务规则（清洗、提取、Schema）
└── logs/                    # 运行日志（按日期滚动）
```

---

## 数据输出目录（DATA_ROOT 下）

| 目录 | 内容 |
|---|---|
| `raw/` | 原始文件（HTML、PDF） |
| `markdown/` | 解析后的 Markdown 全文 |
| `json_envelopes/` | 结构化 JSON 元数据信封 |
| `quarantine/` | 质量不达标的隔离数据 |
| `review/` | 待人工审核的 CSV 导出 |

---

## 新增爬取站点

编辑 `config/crawler_settings.py`，在 `SITE_PROFILES` 字典中追加条目即可：

```python
"your_site_key": {
    "name": "站点名称",
    "url_pattern": "example.com",
    "start_urls": ["https://www.example.com/news/"],
    "use_css_extractor": False,
    "request_delay_seconds": 2.0,
    "headers": DEFAULT_HEADERS,
    "max_pages": 30,
},
```
