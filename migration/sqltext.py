"""SQL text helpers of the migration runners: the top-level statement splitter."""

import re

# 美元引用起始标记：$$ 或 $tag$
_DOLLAR_TAG = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$")


def split_sql(sql: str) -> list[str]:
    """按「顶层」分号拆分 SQL 语句。

    不能用 `sql.split(";")`：PostgreSQL 里分号合法出现在四处非语句边界 ——
    `$tag$ ... $tag$` 美元引用块（函数体）、单引号字符串、双引号标识符、
    注释。硬拆会把一条语句劈成几段非法 SQL。实测 V019 的三个 plpgsql 函数体
    被拆成 61 段、其中 8 段美元符不平衡，直接用旧实现执行必然报语法错。

    这里用状态机逐字符扫描，只认顶层分号为语句边界：
      - `'...'`  单引号字符串，`''` 为转义
      - `"..."`  双引号标识符，`""` 为转义
      - `$tag$`  美元引用块，tag 可为空（`$$`）
      - `--`     行注释，吞到行尾
      - `/* */`  块注释，支持嵌套
    """
    statements: list[str] = []
    buf: list[str] = []
    i, n = 0, len(sql)

    while i < n:
        ch = sql[i]

        # -- 行注释：连同换行前的内容原样保留，交给 PG 忽略
        if ch == "-" and sql.startswith("--", i):
            j = sql.find("\n", i)
            j = n if j == -1 else j
            buf.append(sql[i:j])
            i = j
            continue

        # /* */ 块注释（PG 允许嵌套）
        if ch == "/" and sql.startswith("/*", i):
            depth, j = 1, i + 2
            while j < n and depth:
                if sql.startswith("/*", j):
                    depth, j = depth + 1, j + 2
                elif sql.startswith("*/", j):
                    depth, j = depth - 1, j + 2
                else:
                    j += 1
            buf.append(sql[i:j])
            i = j
            continue

        # 单引号字符串 / 双引号标识符：成对双写即为转义
        if ch in ("'", '"'):
            quote = ch
            j = i + 1
            while j < n:
                if sql[j] == quote:
                    if j + 1 < n and sql[j + 1] == quote:
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            buf.append(sql[i:j])
            i = j
            continue

        # 美元引用块：找到同 tag 的闭合标记为止
        if ch == "$":
            m = _DOLLAR_TAG.match(sql, i)
            if m:
                tag = m.group(0)
                end = sql.find(tag, i + len(tag))
                if end == -1:
                    # 未闭合：原样交给 PG 报错，避免静默吞掉内容
                    buf.append(sql[i:])
                    i = n
                else:
                    j = end + len(tag)
                    buf.append(sql[i:j])
                    i = j
                continue

        # 顶层分号 → 断句
        if ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
            i += 1
            continue

        buf.append(ch)
        i += 1

    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements
