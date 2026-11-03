"""把分类目录里的立绘编进 CCIP 外观索引。可中断后续跑。"""
from __future__ import annotations

import argparse
import json

from ccip_index import GAMES, build_index


def main() -> None:
    parser = argparse.ArgumentParser(description="用现成 CCIP 权重为本地立绘建索引，不微调模型")
    parser.add_argument("--games", nargs="*", default=list(GAMES))
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 张，用于抽样")
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--device", default="auto", choices=("auto", "cpu", "mps"))
    args = parser.parse_args()
    stats = build_index(
        games=tuple(args.games),
        limit=args.limit,
        batch_size=max(1, args.batch_size),
        device_name=args.device,
    )
    print("[汇总] " + json.dumps(stats, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
