"""评测敏感词过滤器：误报率、召回率与阈值曲线。

模型指标有两个数，**必须一起看**：

- **误报率（FPR）**：无害请求被拦下的比例。误杀一个正常请求比漏掉一条敏感
  请求伤得多——用户会直接不信任这个服务，然后绕开它。
- **召回率（TPR）**：敏感请求被拦下的比例。

把阈值往上调，FPR 归零、TPR 也归零，那不叫修好，叫把过滤器关掉。所以本脚本
除了给单点指标，还会打印**整条阈值曲线**，让人自己挑工作点。

评测集分三份：

1. **留出集**（:mod:`scripts.sensitive_eval_corpus`）——从不参与训练，是这份
   报告里最可信的两个数字；
2. **训练集**（:mod:`scripts.sensitive_corpus`）——已拟合，只作对照，看有没有
   过拟合的迹象；
3. **仓库文档段落**——真实中文长文本的独立样本，风格与上面两份都不同，
   用来交叉验证。

另外还报两个容易漏的角度：**难负例**（训练语料里的对抗性负例，误报率最容易
在这里冒头）与**按类别的召回率**（防止「总体好看、某一类全漏」）。

用法：

```bash
python -m scripts.eval_sensitive                      # 评测仓库里的模型
python -m scripts.eval_sensitive --model other.npz    # 评测别的模型
python -m scripts.eval_sensitive --threshold 0.8      # 指定阈值看单点
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config.settings import settings  # noqa: E402
from app.core.sensitive import SensitiveFilter  # noqa: E402
from scripts.sensitive_eval_corpus import (  # noqa: E402
    EVAL_NEGATIVE,
    EVAL_POSITIVE,
    build_eval_set,
)
from scripts.sensitive_corpus import (  # noqa: E402
    ALL_HARD_NEGATIVE,
    FRAUD_POSITIVE,
    HATE_POSITIVE,
    INDIRECT_POSITIVE,
    POLITICAL_POSITIVE,
    PORN_POSITIVE,
    VIOLENCE_POSITIVE,
    build_dataset,
)

# 正例分类 → 语料组。分类维度是「模型最可能整类失效」的地方：
# 哈希权重是共享的，某一类权重过大就会挤掉别的类，总体指标好看、某一类全漏
# 的情况靠总召回率看不出来。
CATEGORIES: list[tuple[str, list[str]]] = [
    ("违法暴力", VIOLENCE_POSITIVE),
    ("色情", PORN_POSITIVE),
    ("政治敏感", POLITICAL_POSITIVE),
    ("隐私诈骗", FRAUD_POSITIVE),
    ("仇恨歧视", HATE_POSITIVE),
    ("间接说法", INDIRECT_POSITIVE),
]


def repo_doc_segments() -> list[str]:
    """把仓库自己的中文文档切成段落——真实中文长文本的独立样本。

    这些段落**不会**进入训练集（训练语料是手写的短句），所以它们是第三个
    独立的观测角度。

    Returns:
        含中文的段落列表。
    """
    segs: list[str] = []
    for rel in ("README.md", "docs/design.md", "docs/performance.md"):
        p = ROOT / rel
        if not p.exists():
            continue
        for para in re.split(r"\n\s*\n", p.read_text(encoding="utf-8")):
            para = para.strip()
            if len(para) >= 30 and re.search(r"[一-鿿]", para):
                segs.append(para)
    return segs


def rate(scores: np.ndarray, labels: np.ndarray, threshold: float) -> tuple[float, float]:
    """算 (误报率, 召回率)。

    Args:
        scores: 每条文本的分数。
        labels: 真值标签（1=敏感）。
        threshold: 判定阈值。

    Returns:
        ``(误报率, 召回率)``。
    """
    pred = scores >= threshold
    truth = labels == 1
    fp = int(np.sum(pred & ~truth))
    tn = int(np.sum(~pred & ~truth))
    tp = int(np.sum(pred & truth))
    fn = int(np.sum(~pred & truth))
    fpr = fp / (fp + tn) if (fp + tn) else 0.0
    tpr = tp / (tp + fn) if (tp + fn) else 0.0
    return fpr, tpr


def main(argv: list[str] | None = None) -> int:
    """评测入口。"""
    ap = argparse.ArgumentParser(description="评测敏感词过滤器")
    ap.add_argument("--model", type=Path, default=None, help="模型路径（默认用仓库里的）")
    ap.add_argument("--threshold", type=float, default=None, help="指定阈值；不给就用模型自带的")
    ap.add_argument("--show-fp", type=int, default=12, help="列出多少条误报样本")
    ap.add_argument("--no-curve", action="store_true", help="不打印阈值曲线")
    args = ap.parse_args(argv)

    model = args.model or Path(settings.sensitive_model_path)
    f = SensitiveFilter(path=model)
    f.load()
    threshold = args.threshold if args.threshold is not None else float(f.threshold)

    print(f"模型 {model}")
    print(f"维度 {f._dim}，窗口 {f.window}/{f.stride}，归一化 {f.norm_alpha}，阈值 {threshold:.3f}")

    # ---- 留出集
    ev_texts, ev_labels = build_eval_set()
    ev_scores = np.array([f.check(t).score for t in ev_texts], dtype=np.float64)
    ev_lab = np.asarray(ev_labels)
    fpr, tpr = rate(ev_scores, ev_lab, threshold)
    print(
        f"\n【留出集】负例 {len(EVAL_NEGATIVE)} 条 / 正例 {len(EVAL_POSITIVE)} 条"
        f" → 误报率 {fpr:.2%}，召回率 {tpr:.2%}"
    )

    # ---- 训练集（已拟合，只作对照）
    tr_texts, tr_labels, _ = build_dataset()
    tr_scores = np.array([f.check(t).score for t in tr_texts], dtype=np.float64)
    tr_lab = np.asarray(tr_labels)
    fpr_tr, tpr_tr = rate(tr_scores, tr_lab, threshold)
    print(f"【训练集】对照 → 误报率 {fpr_tr:.2%}，召回率 {tpr_tr:.2%}")

    # ---- 仓库文档段落
    docs = repo_doc_segments()
    doc_scores = np.array([f.check(t).score for t in docs], dtype=np.float64)
    doc_fpr = float(np.mean(doc_scores >= threshold))
    print(f"【仓库文档】{len(docs)} 段中文长文本 → 误报率 {doc_fpr:.2%}")

    # ---- 难负例（训练语料里的对抗性负例）
    hard_scores = np.array([f.check(t).score for t in ALL_HARD_NEGATIVE], dtype=np.float64)
    hard_fpr = float(np.mean(hard_scores >= threshold))
    print(
        f"【难负例】{len(ALL_HARD_NEGATIVE)} 条含敏感词但无害的请求 → 误报率 {hard_fpr:.2%}"
    )

    # ---- 按类别的召回率
    print("\n按类别的召回率（训练语料）")
    for name, group in CATEGORIES:
        if not group:
            continue
        hit = sum(1 for t in group if f.check(t).hit)
        print(f"  {name:<8} {hit:>3}/{len(group):<3} = {hit / len(group):.2%}")

    # ---- 误报明细
    fp_idx = [
        i for i, (s, l) in enumerate(zip(ev_scores, ev_lab, strict=True)) if s >= threshold and l != 1
    ]
    if fp_idx:
        print(f"\n留出集里被误杀的 {len(fp_idx)} 条：")
        for i in sorted(fp_idx, key=lambda j: -ev_scores[j])[: args.show_fp]:
            print(f"  [{ev_scores[i]:.3f}] {ev_texts[i]}")
    fn_idx = [
        i for i, (s, l) in enumerate(zip(ev_scores, ev_lab, strict=True)) if s < threshold and l == 1
    ]
    if fn_idx:
        print(f"\n留出集里没拦住的 {len(fn_idx)} 条：")
        for i in fn_idx[: args.show_fp]:
            print(f"  [{ev_scores[i]:.3f}] {ev_texts[i]}")

    # ---- 阈值曲线
    if not args.no_curve:
        print("\n阈值曲线（留出集）：")
        print("  阈值    误报率    召回率")
        for t in np.arange(0.05, 0.999, 0.025):
            fp_r, tp_r = rate(ev_scores, ev_lab, float(t))
            mark = "  ← 当前" if abs(float(t) - threshold) < 0.013 else ""
            print(f"  {t:.3f}   {fp_r:7.2%}   {tp_r:7.2%}{mark}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
