# 0002 处理器版本的来历

- 状态：记录（从代码注释里移出来的版本变更说明）
- 日期：2026-11-06

处理器版本（`PROCESSOR_VERSION`）的规则只要一改就升版本，旧版本的产物保留，新旧结果按版本并存。代码里的注释只讲
当前规则为什么这样；每一版改了什么记在这里。逐条的评审讨论在提交说明里。

## processing/clean_text（当前 `datafabric.clean_text.v12`）

- v1 按 4,000 字截断正文，丢过入组标准和终点；之后的版本一律不截断。
- v6 把结构化字段写成 JSON 块，在 4,496 个真实 ClinicalTrials 版本的正文里全都有 JSON；v7 起改为可读的逐行文本。
- v7/v8 对结果表和研究地点封顶，会丢不良事件、分组、单位、分母、离散度；之后取消封顶，长度预算只用在索引分片、
  展示摘要和模型阅读窗口。
- v5 起，“投影正文出自版本记录本身的 `/clean_text` 字段、不在原件里”这一定位依据存进每段（`db/clean_stage`），
  API 和契约照原样带出。
- v12：WHO DON 改从原件 JSON 清洗（标题写成一级标题，`<table>` 写成 markdown 表格）；其余来源与 v11 相同。

## processing/registry_diff（当前 `datafabric.registry_diff.v3`）

- v2：分诊路由从“列出值得判断的变化”改为“只有能证明不影响判断的变化才不送模型”（起因是一个漏送的反例）。
- v3：等价白名单收窄到确实可等价的字段——正式题目只差大小写/空白、名称类列表只差顺序、地点只差联系人；其余按原值比较。

## processing/profile（当前 `datafabric.profile.v5`）

- v4：监管机构公告/通知的大类按内容判（写明监管动作的才是监管决定，其余是官方通报）；v3 把这类公告整体算作决定。
- v5：加入 v3 6.2 的可复用注释（实验类型候选、可取得的材料层级）。

## processing/measure_extract（当前 `datafabric.measure_extract.v2`）

- v2：提示词带上输出模型的 JSON Schema 和一个最小骨架。v1 只用 `response_format=json_object` 要求“是 JSON”，D1 首批测量
  的 24 个窗口全部返回了顶层 `measures`、空串枚举值和列表形式的 `sources`，按产品的输出形状被拒（`output_not_the_schema`）。
  解析器不放宽，机械修补后的答案不算模型成功。生产者标识里的版本随之变化，v1 的运行原样保留。
