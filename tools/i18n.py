#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
i18n.py — 화면에 나가는 말을 영어/한국어로 고른다.

이 프로젝트는 한국어로 시작했는데 공개하고 나니 읽는 사람 대부분이 영어권이다.
그래서 **기본을 영어로 바꾸고** 한국어는 환경변수로 남긴다.

    t("windows", "창")            → 기본 "windows", CLAUDEUSAGE_LANG=ko 면 "창"
    CLAUDEUSAGE_LANG=ko python3 tools/cc-limit.py

번역 파일을 따로 두지 않고 부르는 자리에 두 언어를 나란히 적는다. 문구를 고칠 때
둘을 같이 보게 되고, 키 이름을 지어내지 않아도 되고, 빠뜨린 번역이 눈에 띈다.
표가 많은 도구라 **자리 폭이 중요하다** — 영어가 길어지면 표가 깨지므로 짧게 쓴다.

주석과 코드 설명은 한국어 그대로 둔다. 그건 만든 사람이 읽는 것이다.
"""

import os

LANG = (os.environ.get("CLAUDEUSAGE_LANG") or "en").lower()[:2]


def t(en, ko):
    """영어가 기본. CLAUDEUSAGE_LANG=ko 면 한국어."""
    return ko if LANG == "ko" else en


def is_ko():
    return LANG == "ko"
