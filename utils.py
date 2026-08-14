# -*- coding: utf-8 -*-
"""移植自 Amiya-Bot core/util/common.py 的通用工具函数（仅保留所需部分）。"""

import difflib
import re

from string import punctuation

# zhon.hanzi.punctuation（中文标点）
punctuation_cn = "。，、；：？！“”‘’（）【】《》〈〉「」『』〔〕〖〗〘〙〚〛〜～．…·"


def remove_punctuation(text: str, ignore: list = None):
    punc = punctuation + punctuation_cn
    if ignore:
        for i in ignore:
            punc = punc.replace(i, "")
    for i in punc:
        text = text.replace(i, "")
    return text


def remove_xml_tag(text: str):
    return re.compile(r"<[^>]+>", re.S).sub("", text)


def integer(value):
    if isinstance(value, float) and int(value) == value:
        value = int(value)
    return value


def find_similar_list(text: str, text_list: list):
    result = {}
    for item in text_list:
        rate = float(
            difflib.SequenceMatcher(None, text, item).quick_ratio()
            * len([n for n in text if n in set(item)])
        )
        if rate > 0:
            if rate not in result:
                result[rate] = []
            result[rate].append(item)

    if result:
        return result[sorted(result.keys())[-1]]
    return []


def find_most_similar(text: str, text_list: list):
    res = find_similar_list(text, text_list)
    if res:
        return res[0]
    return None


def get_longest(text: str, items: list):
    res = ""
    for item in items:
        if item in text and len(item) >= len(res):
            res = item
    return res


def get_index_from_text(text: str, array: list):
    r = re.search(r"(\d+)", text)
    if r:
        index = abs(int(r.group(1))) - 1
        if index >= len(array):
            index = len(array) - 1
        return index
    return None


def snake_case_to_pascal_case(snake_case: str):
    words = snake_case.split("_")
    return "".join(
        word.title() if i > 0 else word.lower() for i, word in enumerate(words)
    )


def sorted_dict(data: dict, *args, **kwargs):
    return {n: data[n] for n in sorted(data, *args, **kwargs)}
