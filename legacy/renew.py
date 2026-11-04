#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ACLClouds 自动续期脚本 (入口)
=============================
逻辑统一在 renew_fixed.py (适配 2026-09 aclclouds.com 改版)。
本文件仅为兼容旧调用方式:  python renew.py
"""

import os
import sys

# 确保同目录下的 renew_fixed.py 可被导入
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from renew_fixed import main  # noqa: E402

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n用户中断", flush=True)
        sys.exit(130)
