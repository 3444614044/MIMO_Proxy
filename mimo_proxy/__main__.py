# -*- coding: utf-8 -*-
"""入口: python -m mimo_proxy"""

import os
import sys
import json


def main():
    # 支持 --config / --init-config / --show-config
    args = sys.argv[1:]

    if "--init-config" in args:
        from .config import save_default
        target = "config.json"
        for a in args:
            if a.startswith("--init-config="):
                target = a.split("=", 1)[1]
        ok = save_default(target)
        print(f"✅ 已生成配置文件: {target}" if ok
              else f"⚠️  {target} 已存在，未覆盖")
        return 0

    if "--show-config" in args:
        from .config import CFG
        d = {k: v for k, v in CFG.data.items() if not k.startswith("_")}
        print(json.dumps(d, ensure_ascii=False, indent=2))
        return 0

    from .server import main as run
    return run()


if __name__ == "__main__":
    sys.exit(main() or 0)
