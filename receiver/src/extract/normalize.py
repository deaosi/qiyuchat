"""文本归一化。目的是让「同一个问题」的不同说法能撞到一起。

这一步做好了，聚类准确率能上一个台阶；这一步做不好，后面用什么模型都白搭。

规则：
1. 全角 → 半角
2. 去 emoji / 特殊符号 / 标点
3. 去开头寒暄（你好、在吗、请问……）
4. 去结尾语气词（啊、呢、吧、哦……）
5. 数字归一（订单号、型号、日期里的数字统一成 #）
6. 英文小写
"""

from __future__ import annotations

import re
import unicodedata
from .text import clean_visible_text

# 开头寒暄/客套，按长度倒序匹配，先长后短
_PREFIXES = sorted([
    "麻烦问一下", "麻烦问下", "想请问一下", "想问一下", "问一下", "咨询一下",
    "请问一下", "请问下", "麻烦你", "麻烦您", "不好意思", "打扰一下",
    "你好", "您好", "在吗", "在么", "在不在", "有人吗", "哈喽", "hello", "hi",
    "我想问", "我想知道", "想问下", "问下", "请问", "麻烦", "谢谢", "好的",
], key=len, reverse=True)

# 结尾语气词/客套
_SUFFIXES = sorted([
    "谢谢啦", "谢谢了", "谢谢你", "谢谢您", "谢谢", "多谢",
    "呢", "吗", "吧", "啊", "呀", "哦", "噢", "嘛", "啦", "哈", "呗", "咯",
    "的", "了", "请问",
], key=len, reverse=True)

# 需要整体删掉的符号类
_EMOJI = re.compile(
    "[" "\U0001F300-\U0001FAFF" "\U00002600-\U000027BF" "\U0001F000-\U0001F2FF"
    "\U0000FE00-\U0000FE0F" "\U00002190-\U000021FF" "\U00002B00-\U00002BFF" "]+"
)
_PUNCT = re.compile(r"[\s\u3000!-/:-@\[-`{-~，。！？；：、“”‘’（）《》【】…—·~]+")
_NUM = re.compile(r"\d+")
# 数字占位符。不能用 # 之类的符号 —— 会在下面被标点正则删掉
_NUM_TOKEN = "0"

# 明显不是问题、纯情绪/确认类，抽取阶段就该丢掉
_NOISE_ONLY = {
    "好", "好的", "嗯", "嗯嗯", "哦", "ok", "okay", "行", "可以", "收到", "谢谢",
    "在", "在的", "对", "是的", "不是", "没有", "有", "知道了", "明白",
}


def to_halfwidth(s: str) -> str:
    out = []
    for ch in s:
        code = ord(ch)
        if code == 0x3000:
            out.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    return "".join(out)


def normalize(text: str) -> str:
    """归一化后的字符串，用于聚类比较。空串表示无有效内容。"""
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", clean_visible_text(text))
    s = to_halfwidth(s)
    s = _EMOJI.sub("", s)
    s = s.lower()

    # 数字归一：保留数量级的差异已经足够，具体值对「高频问题」没意义
    s = _NUM.sub(_NUM_TOKEN, s)
    s = _PUNCT.sub("", s)

    changed = True
    while changed and s:
        changed = False
        for p in _PREFIXES:
            if s.startswith(p) and len(s) > len(p):
                s = s[len(p):]
                changed = True
                break
        for suf in _SUFFIXES:
            if s.endswith(suf) and len(s) > len(suf):
                s = s[: -len(suf)]
                changed = True
                break

    return s.strip()


def is_noise_question(text: str) -> bool:
    """纯情绪/确认类的短句不算问题。"""
    n = normalize(text)
    if len(n) < 3:
        return True
    if n in _NOISE_ONLY:
        return True
    # 全是同一个字重复（哈哈哈、嗯嗯嗯）
    if len(set(n)) == 1:
        return True
    return False


# 客户发来的「信息」而不是「问题」：地址、单号、纯数字
_ADDR_RE = re.compile(r"(省|市|区|县|镇|路|街|道|号|栋|幢|单元|室|楼|层|小区|花园|大厦|广场)")
_ALNUM_RE = re.compile(r"^[A-Za-z0-9\-_#\s]+$")
_QUESTION_MARK = re.compile(r"[?？]")
_QUESTION_WORDS = (
    "怎么", "怎样", "什么", "多少", "多久", "几", "哪", "能不能", "可不可以",
    "可以吗", "是否", "为什么", "为啥", "如何", "有没有", "是不是", "会不会",
    "能不能", "行不行", "对不对", "怎么办", "咋",
)


def is_info_not_question(text: str) -> bool:
    """客户只是提供了信息（地址、单号、型号），不是在提问。

    这类不该进「高频问题」统计，否则 Top 榜会被地址和订单号占满。
    """
    if not text:
        return True
    has_q = bool(_QUESTION_MARK.search(text)) or any(w in text for w in _QUESTION_WORDS)
    if has_q:
        return False
    # 纯字母数字（单号、型号、身份证、手机号）
    if _ALNUM_RE.match(text.strip()):
        return True
    # 地址特征明显且很短
    n = normalize(text)
    if _ADDR_RE.search(text) and len(n) <= 40:
        return True
    return False


def grams(text: str, n: int = 2) -> set[str]:
    """字符 n-gram 集合。中文用 2-gram 效果最稳。"""
    if not text:
        return set()
    if len(text) < n:
        return {text}
    return {text[i:i + n] for i in range(len(text) - n + 1)}
