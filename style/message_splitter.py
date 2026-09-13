"""MessageSplitter：确定性的中文消息拆分规则。

硬规则（不允许随机、不允许 LLM 决定）：
    句号     拆分 + 删除句号
    叹号问号  拆分 + 保留
    逗号     永远不拆分，原样保留
    省略号   不可拆分（"..." 同样视作省略号，不当作三个句号）
    连续标点  ？！ / ！？！ 算同一个边界，不能拆开
    引号括号  引号/括号内部的标点不作为句子边界

再按 MessagePlan 的目标条数把"自然句"合并（不是机械等长切割）。
"""

from __future__ import annotations

import re

SENTENCE_MARKS = "。！？!?"
DROP_MARKS = "。"
ELLIPSIS_CHARS = "…"

# 引号里的一句话常常是完整的（“真的可以吗？”→ 就在引号后断）；
# 括号里的内容通常是句中补充（他昨天（其实我不确定？）没来）→ 不在括号后断。
PAIR_OPEN = {"“": "”", "‘": "’", "（": "）", "(": ")", "【": "】", "[": "]"}
QUOTE_PAIRS = {"“": "”", "‘": "’"}
PAIR_CLOSE = set(PAIR_OPEN.values())

ASCII_DOTS_RE = re.compile(r"\.{2,}")


def split_sentences(text: str) -> list[str]:
    """按硬规则切成自然句（还没合并）。"""
    raw = (text or "").strip()
    if not raw:
        return []
    sentences: list[str] = []
    buffer: list[str] = []
    stack: list[str] = []
    pending_marks: list[str] = []
    quote_depth = 0
    index = 0
    length = len(raw)
    while index < length:
        char = raw[index]
        if char == ELLIPSIS_CHARS:
            while index < length and raw[index] == ELLIPSIS_CHARS:
                buffer.append(raw[index])
                index += 1
            continue
        if char == ".":
            match = ASCII_DOTS_RE.match(raw, index)
            if match:
                buffer.append(match.group(0))
                index = match.end()
                continue
        if char in PAIR_OPEN:
            stack.append(PAIR_OPEN[char])
            if char in QUOTE_PAIRS:
                quote_depth += 1
            buffer.append(char)
            index += 1
            continue
        if char in PAIR_CLOSE and stack and stack[-1] == char:
            stack.pop()
            was_quote = stack.count("”") >= 0 and raw[index] in set(QUOTE_PAIRS.values())
            if was_quote:
                quote_depth = max(0, quote_depth - 1)
            buffer.append(char)
            index += 1
            # 引号/括号里出现过句尾标点、且现在已经闭合 → 就在这里断句
            if was_quote and pending_marks:
                text_out = "".join(buffer).strip()
                for close_mark in PAIR_CLOSE:
                    text_out = text_out.replace("。" + close_mark, close_mark)
                buffer = []
                pending_marks = []
                if text_out:
                    sentences.append(text_out)
            continue
        if char in SENTENCE_MARKS and stack:
            # 引号/括号里的标点先记下来，等闭合时再断
            while index < length and raw[index] in SENTENCE_MARKS:
                pending_marks.append(raw[index])
                buffer.append(raw[index])
                index += 1
            continue
        if char in SENTENCE_MARKS and not stack:
            marks = []
            while index < length and raw[index] in SENTENCE_MARKS:
                marks.append(raw[index])
                index += 1
            while index < length and raw[index] == ELLIPSIS_CHARS:
                marks.append(raw[index])
                index += 1
            kept = "".join(mark for mark in marks if mark not in DROP_MARKS)
            text_out = "".join(buffer).strip()
            buffer = []
            if kept:
                sentences.append((text_out + kept).strip())
            elif text_out:
                sentences.append(text_out)
            continue
        buffer.append(char)
        index += 1
    tail = "".join(buffer).strip()
    if tail:
        sentences.append(tail)
    return [item for item in sentences if item]


def _merge_to_count(sentences: list[str], target: int) -> list[str]:
    """把自然句合并成 target 条，尽量让每条长度接近（避免 10 字 + 800 字）。"""
    if target <= 1:
        return ["".join(sentences)] if sentences else []
    if len(sentences) <= target:
        return list(sentences)
    total = sum(len(item) for item in sentences)
    ideal = total / target
    groups: list[str] = []
    current = ""
    remaining_groups = target
    for index, sentence in enumerate(sentences):
        sentences_left = len(sentences) - index
        must_split = sentences_left == remaining_groups
        if current and (len(current) >= ideal * 0.75 or must_split):
            groups.append(current)
            current = sentence
            remaining_groups -= 1
        else:
            current += sentence
    if current:
        groups.append(current)
    while len(groups) > target:
        groups[-2] += groups[-1]
        groups.pop()
    return [item for item in groups if item]


def render_messages(text: str, *, target_count: int = 1, max_messages: int = 4) -> list[str]:
    """对外入口：文本 + 计划条数 -> 最终要发出去的消息列表。"""
    raw = (text or "").strip()
    if not raw:
        return []
    if "\n" in raw:
        lines = [line.strip() for line in raw.split("\n") if line.strip()]
        if len(lines) > 1:
            target = max(int(target_count or 1), len(lines))
            if len(lines) <= max_messages and target <= max_messages:
                return lines
            return _merge_to_count(["".join(lines)], min(target, max_messages))
    sentences = split_sentences(raw)
    if not sentences:
        return []
    target = max(1, min(int(target_count or 1), int(max_messages)))
    if target <= 1:
        return ["".join(sentences)]
    if len(sentences) <= target:
        return sentences
    return _merge_to_count(sentences, target)
