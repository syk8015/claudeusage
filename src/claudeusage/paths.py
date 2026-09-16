#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
paths.py — 기록을 어디에 둘지 한 곳에서 정한다.

저장소에서 그냥 돌릴 때는 `<저장소>/data/` 에 쌓였다. 그런데 pip 로 설치하면 코드가
site-packages 안에 들어가므로 거기에 쓰면 안 된다(업그레이드하면 날아가고, 권한도
없을 수 있다). 그래서 다음 순서로 고른다.

  1. 환경변수 CLAUDEUSAGE_DATA 가 있으면 그것
  2. 저장소 안에서 돌고 있고 <저장소>/data 가 이미 있으면 그것  ← 기존 사용자
  3. 없으면 ~/.claudeusage

2번이 있는 이유는 하나다. 이미 표본을 몇 만 줄 쌓아 둔 사람의 기록이 갑자기 안
보이면 안 된다. 저장소에서 돌리면 예전과 똑같이 동작한다.
"""

import os

_HERE = os.path.dirname(os.path.abspath(__file__))
# src/claudeusage/paths.py → 저장소 뿌리는 두 단계 위
_REPO_DATA = os.path.abspath(os.path.join(_HERE, "..", "..", "data"))


def data_dir():
    env = os.environ.get("CLAUDEUSAGE_DATA")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    if os.path.isdir(_REPO_DATA):
        return _REPO_DATA
    return os.path.expanduser("~/.claudeusage")


def data_file(name):
    return os.path.join(data_dir(), name)


def ensure_data_dir():
    d = data_dir()
    os.makedirs(d, exist_ok=True)
    return d


def ratelimit_log():
    return data_file("ratelimit-log.jsonl")


def usage_log():
    return data_file("usage-log.jsonl")
