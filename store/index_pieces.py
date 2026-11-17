"""索引分片（v3 3.6）：一份清洗产物太长、一次放不进索引时，切成几片分别进索引。

- 两层身份：产物（artifact_key = 处理器版本:全篇摘要）不变；每片另有分片身份——规则版本、序号、片数、在全篇里
  的绝对范围 [start, end)、上下文前缀长度、字符数、UTF-8 字节数、片摘要。一次放得下的产物不分片、不带分片
  身份（与原有的单条身份完全一致）。
- 上传预算：字符数和 UTF-8 字节数同时满足，上下文前缀和桥接上传时加的一行固定版本注释都算在内。
  实际块数由桥接按实际分块核验，这里不估算冒充。
- 切点：优先清洗段落的边界（segments），一段放不下按行，一行放不下才按字符有界切（尽量落在空白处）。
  每个字都在且只在一片里：各片范围首尾相接，覆盖全篇。
- 表格：从表格中间开始的片，把该表的表头行和分隔行作为上下文前缀写在片首；前缀长度单独记，不算原件内容，
  检索命中换算回全篇偏移时跳过它。
"""

from __future__ import annotations

import functools
import hashlib
import re

RULE = "datafabric.index_piece.v1"
MAX_CHARS = 400_000
MAX_BYTES = 1_200_000
UPLOAD_OVERHEAD = 100  # 余量：桥接 v1 上传时加过一行注释（约 90 字节），v2 起不加；数不改，分片计划不变
_TABLE_RULE = re.compile(r"^\|?\s*:?-{3,}")


def _line_start(text, pos):
    return text.rfind("\n", 0, pos) + 1


def context(text, start):
    """片从表格中间开始时的上下文前缀：该表的表头行 + 分隔行（各带换行）；否则为空。"""
    if start == 0 or not text.startswith("|", start) or text[start - 1] != "\n":
        return ""
    block = start
    while block > 0:
        previous = _line_start(text, block - 1)
        if not text.startswith("|", previous):
            break
        block = previous
    lines = text[block:start].split("\n")
    if len(lines) >= 3 and _TABLE_RULE.match(lines[1].strip()):
        return lines[0] + "\n" + lines[1] + "\n"
    return ""


def _byte_prefix(text):
    out, total = [0], 0
    for ch in text:
        total += len(ch.encode())
        out.append(total)
    return out


def _hard_cuts(text, start, end, step):
    """一行太长时的字符切点：每隔约 step 个字，尽量退到前面 200 字以内的空白处。"""
    cuts, pos = [], start + step
    while pos < end:
        space = text.rfind(" ", max(start, pos - 200), pos)
        cut = space + 1 if space > start else pos
        cuts.append(cut)
        pos = cut + step
    return cuts


def key(piece):
    """分片的身份键（唯一键、本地编号、发布编号都用它）：规则版本、序号、全篇范围、片摘要；不分片为空串。
    规则换了、范围或内容变了都是另一片，不会与旧片相撞。"""
    if not piece:
        return ""
    return "%s:%d:%d-%d:%s" % (piece["rule"], piece["index"], piece["start"], piece["end"], piece["sha256"])


def keys(text, segments=()):
    """按当前计划，这份产物应有的全部分片身份键；不分片为 {""}（整篇一条）。交接判定按这个完整集合，
    不按数据库里现存的行。"""
    return {key(p) for p in plan(text, segments)} or {""}


def fits(text):
    """整篇一次放得下（不用分片）：字符数和 UTF-8 字节数（加上上传附加的一行）都不超过上限。"""
    return (len(text) + UPLOAD_OVERHEAD <= MAX_CHARS
            and len(text.encode()) + UPLOAD_OVERHEAD <= MAX_BYTES)


def plan(text, segments=(), *, max_chars=None, max_bytes=None):
    """整篇正文 → 分片计划 [分片身份…]；一次放得下返回 []（不分片）。上限默认取模块当前的 MAX_CHARS / MAX_BYTES。"""
    max_chars = MAX_CHARS if max_chars is None else max_chars
    max_bytes = MAX_BYTES if max_bytes is None else max_bytes
    n = len(text)
    fits_range = functools.partial(_range_fits, prefix=_byte_prefix(text), max_chars=max_chars,
                                   max_bytes=max_bytes)
    if fits_range("", 0, n):
        return []
    paragraph = sorted({s.get("start", 0) for s in segments or [] if 0 < s.get("start", 0) < n} | {n})
    lines = sorted({i + 1 for i, ch in enumerate(text) if ch == "\n" and i + 1 < n} | {n})
    step = max(1, (max_chars - UPLOAD_OVERHEAD) // 4)
    ranges, start = [], 0
    while start < n:
        end = _range_end(text, start, (paragraph, lines), step, fits_range)
        ranges.append((start, end))
        start = end
    return _pieces(text, ranges)


def _range_fits(ctx, start, end, *, prefix, max_chars, max_bytes):
    """The context prefix plus text[start:end] fit one upload (characters and UTF-8 bytes)."""
    return (len(ctx) + end - start + UPLOAD_OVERHEAD <= max_chars
            and len(ctx.encode()) + prefix[end] - prefix[start] + UPLOAD_OVERHEAD <= max_bytes)


def _range_end(text, start, tiers, step, fits_range):
    """Where the piece starting at `start` ends: the last fitting paragraph boundary, else the last fitting line
    boundary, else a bounded cut inside the line."""
    ctx = context(text, start)
    for tier in tiers:  # 先按段落，段落放不下按行
        fitting = [b for b in tier if b > start and fits_range(ctx, start, b)]
        if fitting:
            return fitting[-1]
    # 一行放不下：在这一行里按字符有界切
    line_end = next(b for b in tiers[-1] if b > start)
    fitting = [c for c in _hard_cuts(text, start, line_end, step) if fits_range(ctx, start, c)]
    return fitting[-1] if fitting else min(line_end, start + step)


def _pieces(text, ranges):
    pieces = []
    for index, (start, end) in enumerate(ranges, 1):
        content = context(text, start) + text[start:end]
        pieces.append({"rule": RULE, "index": index, "count": len(ranges), "start": start, "end": end,
                       "context_chars": len(content) - (end - start), "chars": len(content),
                       "bytes": len(content.encode()), "sha256": hashlib.sha256(content.encode()).hexdigest()})
    return pieces


def content(text, piece):
    """一片实际上传的正文：上下文前缀 + 全篇里这一段。"""
    return context(text, piece["start"]) + text[piece["start"]:piece["end"]]
