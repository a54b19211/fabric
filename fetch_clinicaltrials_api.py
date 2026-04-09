"""
ClinicalTrials.gov API 采集器
直接调用官方 API，将数据转换为框架格式并复用存储模块
"""

import json
import requests
from pathlib import Path
from datetime import datetime
from loguru import logger
from config.settings import APP


def fetch_clinicaltrials_api(condition="cancer", max_studies=20):
    """从 ClinicalTrials.gov API 获取数据"""

    base_url = "https://clinicaltrials.gov/api/v2/studies"
    all_studies = []
    page_token = None
    page_size = min(20, max_studies)

    while len(all_studies) < max_studies:
        # 简化参数，只用最基本的
        params = {
            "format": "json",
            "query.term": condition,  # 简化查询，不用 AREA 语法
            "pageSize": page_size
        }
        if page_token:
            params["pageToken"] = page_token

        logger.info(f"请求 API: {base_url}")
        logger.debug(f"请求参数: {params}")

        try:
            response = requests.get(base_url, params=params, timeout=30)
            logger.info(f"响应状态码: {response.status_code}")

            if response.status_code != 200:
                logger.error(f"API 请求失败: {response.status_code}")
                logger.error(f"响应内容: {response.text[:300]}")
                break

            data = response.json()
            studies = data.get("studies", [])

            if not studies:
                logger.warning("响应中没有 studies 字段")
                break

            all_studies.extend(studies)
            logger.info(f"已获取 {len(all_studies)} 条记录")

            # 检查下一页
            page_token = data.get("nextPageToken")
            if not page_token or len(all_studies) >= max_studies:
                break

        except requests.exceptions.RequestException as e:
            logger.error(f"网络请求异常: {e}")
            break
        except json.JSONDecodeError as e:
            logger.error(f"JSON解析失败: {e}")
            if 'response' in locals():
                logger.error(f"响应内容前500字符: {response.text[:500]}")
            break

    return all_studies[:max_studies]


def extract_study_info(study):
    """从API响应中提取研究信息"""
    protocol = study.get("protocolSection", {})
    identification = protocol.get("identificationModule", {})
    status = protocol.get("statusModule", {})
    design = protocol.get("designModule", {})
    conditions = protocol.get("conditionsModule", {})
    description = protocol.get("descriptionModule", {})

    # 获取条件列表 - 修复：处理不同的数据格式
    condition_list = []
    conditions_data = conditions.get("conditions", [])
    if conditions_data:
        for c in conditions_data:
            if isinstance(c, dict):
                condition_list.append(c.get("name", ""))
            elif isinstance(c, str):
                condition_list.append(c)

    # 获取阶段 - 修复：处理不同的数据格式
    phase_list = design.get("phaseList", [])
    if isinstance(phase_list, str):
        phase_list = [phase_list]

    # 获取地点
    locations = []
    loc_module = protocol.get("contactsLocationsModule", {})
    if loc_module.get("locations"):
        for loc in loc_module.get("locations", []):
            if isinstance(loc, dict):
                facility = loc.get("facility", "")
                city = loc.get("city", "")
                country = loc.get("country", "")
                loc_str = ", ".join(filter(None, [facility, city, country]))
                if loc_str:
                    locations.append(loc_str)

    # 获取 enrollment
    enrollment = ""
    enrollment_info = design.get("enrollmentInfo", {})
    if enrollment_info:
        enrollment = enrollment_info.get("count", "")

    return {
        "nct_id": identification.get("nctId", ""),
        "brief_title": identification.get("briefTitle", ""),
        "official_title": identification.get("officialTitle", ""),
        "status": status.get("overallStatus", ""),
        "phase": phase_list,
        "conditions": condition_list,
        "enrollment": enrollment,
        "start_date": status.get("startDateStruct", {}).get("date", ""),
        "completion_date": status.get("completionDateStruct", {}).get("date", ""),
        "brief_summary": description.get("briefSummary", ""),
        "detailed_description": description.get("detailedDescription", ""),
        "locations": locations
    }


def convert_to_framework_format(api_studies):
    """将 API 数据转换为框架的标准化格式"""
    results = []

    for study in api_studies:
        try:
            info = extract_study_info(study)
        except Exception as e:
            logger.warning(f"解析研究数据时出错: {e}")
            continue

        if not info["nct_id"]:
            continue

        # 构造框架需要的格式
        parsed_data = {
            "uuid": info["nct_id"],
            "source_url": f"https://clinicaltrials.gov/ct2/show/{info['nct_id']}",
            "source_site": "clinicaltrials.gov",
            "content_type": "api_json",
            "title": info["brief_title"] or info["official_title"] or "Untitled",
            "content": json.dumps(info, indent=2, ensure_ascii=False),
            "markdown": f"""# {info['brief_title'] or info['official_title'] or 'Untitled'}

**NCT ID:** {info['nct_id']}
**Status:** {info['status']}
**Phase:** {', '.join(info['phase']) if info['phase'] else 'Not specified'}
**Enrollment:** {info['enrollment'] if info['enrollment'] else 'Not specified'}

## Conditions
{', '.join(info['conditions']) if info['conditions'] else 'Not specified'}

## Brief Summary
{info['brief_summary'] if info['brief_summary'] else 'No summary available'}

## Locations
{chr(10).join(f'- {loc}' for loc in info['locations']) if info['locations'] else 'No location information'}

---
*Data fetched from ClinicalTrials.gov API*
"""
        }

        results.append(parsed_data)

    return results


def test_api_connection():
    """测试API连接"""
    logger.info("测试 API 连接...")

    # 最简单的请求测试
    test_url = "https://clinicaltrials.gov/api/v2/studies?format=json&pageSize=1"

    try:
        response = requests.get(test_url, timeout=30)
        logger.info(f"测试响应状态码: {response.status_code}")

        if response.status_code == 200:
            data = response.json()
            logger.info(f"API 连接正常")
            return True
        else:
            logger.error(f"API 测试失败: {response.status_code}")
            return False
    except Exception as e:
        logger.error(f"API 测试异常: {e}")
        return False


def main():
    """主函数：获取数据 -> 存储"""
    logger.info("=" * 60)
    logger.info("开始采集 ClinicalTrials.gov 数据")
    logger.info("=" * 60)

    # 先测试API连接
    if not test_api_connection():
        logger.error("API连接测试失败，请检查网络或稍后重试")
        return

    # 1. 从 API 获取数据
    api_data = fetch_clinicaltrials_api(condition="cancer", max_studies=10)  # 先测试10条
    logger.info(f"共获取 {len(api_data)} 条临床试验记录")

    if not api_data:
        logger.error("没有获取到数据，请检查API参数")
        return

    # 2. 转换为框架格式
    parsed_results = convert_to_framework_format(api_data)
    logger.info(f"成功转换 {len(parsed_results)} 条数据")

    if not parsed_results:
        logger.error("没有成功转换任何数据")
        return

    # 3. 确保输出目录存在
    APP.JSON_DIR.mkdir(parents=True, exist_ok=True)
    APP.MARKDOWN_DIR.mkdir(parents=True, exist_ok=True)

    # 4. 保存数据
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # 保存为JSON文件
    output_json = APP.JSON_DIR / f"clinicaltrials_{timestamp}.json"
    with open(output_json, "w", encoding="utf-8") as f:
        json.dump(parsed_results, f, indent=2, ensure_ascii=False)

    # 保存为Markdown文件（可选）
    output_md = APP.MARKDOWN_DIR / f"clinicaltrials_{timestamp}.md"
    with open(output_md, "w", encoding="utf-8") as f:
        for result in parsed_results:
            f.write(result["markdown"])
            f.write("\n\n---\n\n")

    logger.info("=" * 60)
    logger.info(f"✅ 完成！共获取 {len(parsed_results)} 条临床试验数据")
    logger.info(f"📁 JSON 文件: {output_json}")
    logger.info(f"📁 Markdown 文件: {output_md}")
    logger.info("=" * 60)

    # 打印第一条数据的标题作为示例
    if parsed_results:
        logger.info(f"📋 示例数据标题: {parsed_results[0]['title']}")
        logger.info(f"🔗 示例链接: {parsed_results[0]['source_url']}")


if __name__ == "__main__":
    main()