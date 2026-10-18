"""占位测试文件：归属窗口见 plan/windows.yaml。

保留此文件是为了让"目录所有权"在骨架阶段就可被脚本校验（边界检查要求
allow 路径存在）。该窗口的第一个 issue 落地时，用真实测试替换本文件全文。
"""

import pytest

pytestmark = pytest.mark.skip(reason="占位：待对应窗口实现")
