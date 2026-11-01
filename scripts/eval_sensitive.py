"""敏感词前置过滤的**评估脚本**：出指标表、标定阈值。

过滤器里写死的阈值必须能被人复核、也能在换了模型之后重新算出来。这个脚本就是
干这个的：拿同一份中文语料，把规则层、模型层、两层并集分别评一遍，并给出建议
阈值。

```bash
python -m scripts.eval_sensitive                 # 完整评估（需要先拉模型）
python -m scripts.eval_sensitive --rules-only    # 只评规则层（不需要模型）
python -m scripts.eval_sensitive --target-fpr 0.02
```

## 评估口径

和训练/标定用的口径完全一致，别自己另立一套：

- **按文本**做 80/20 划分（seed 固定），不按窗口划分——按窗口划分会让同一句话
  的窗口同时落在两边，验证分数虚高；
- 阈值在**标定集**上选，在**验证集**上评；
- 同时评估两种输入形态：短句，以及包在正常上下文里的长句（真实输入两种都有，
  只按一种挑阈值，另一半的召回就会塌）。

## 一个必须说清楚的前提

规则层（`app/core/sensitive_keywords.py`）是**人工编写的静态短语表**，它照着
五大类的典型说法写。所以「规则层单独」的召回率是**偏乐观**的——它不是在一个
它没见过的数据集上测出来的。模型层没有这个问题：那些模型从没见过这份语料，
它们的数字是干净的开箱即用成绩。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.sensitive import MODEL_KEYS, SensitiveFilter  # noqa: E402
from app.core.sensitive_keywords import match_keywords  # noqa: E402
from scripts.sensitive_corpus import (  # noqa: E402
    FRAUD_POSITIVE,
    HARD_NEGATIVE,
    HATE_POSITIVE,
    POLITICAL_POSITIVE,
    PORN_POSITIVE,
    VIOLENCE_POSITIVE,
    build_dataset,
    embed_text,
)

# 划分用的随机种子。与历史训练脚本保持一致，划分结果才能互相比较。
SEED = 20241021

CATS = {
    "违法暴力": VIOLENCE_POSITIVE,
    "色情": PORN_POSITIVE,
    "政治敏感": POLITICAL_POSITIVE,
    "隐私诈骗": FRAUD_POSITIVE,
    "仇恨歧视": HATE_POSITIVE,
}


def _metrics(blocked: np.ndarray, y: np.ndarray) -> dict:
    """算混淆矩阵与三个关键比率。"""
    tp = int(np.sum(blocked & (y == 1)))
    fp = int(np.sum(blocked & (y == 0)))
    fn = int(np.sum(~blocked & (y == 1)))
    tn = int(np.sum(~blocked & (y == 0)))
    return {
        "precision": tp / (tp + fp) if tp + fp else 0.0,
        "recall": tp / (tp + fn) if tp + fn else 0.0,
        "fpr": fp / (fp + tn) if fp + tn else 0.0,
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
    }


def _calibrate(
    scores: dict[str, np.ndarray],
    y: np.ndarray,
    target_fpr: float,
    per_model_fpr: float,
    hard_scores: dict[str, np.ndarray] | None = None,
):
    """标定四个阈值。

    三条约束，缺一不可：

    1. **并集**误报率不超过 ``target_fpr``；
    2. **每个模型单独**的误报率不超过 ``per_model_fpr``；
    3. **任何模型都不许在难负例上响**。

    第 2 条不是多余的：只约束并集时，贪心标定会把预算花在最吵的那个模型身上
    ——并集看着达标，误报却全是它带来的。实测 porn 模型就贡献了全部误报
    （它在自己那一类的 AUC 只有 0.844，在别的类别上却乱响，连「写个密码哈希的
    实现」这种句子都会响）。

    第 3 条是**规格**，不是拟合：难负例是专门设计出来抓误报的对抗样本
    （「帮我写一个敏感词过滤器的实现思路」「写个密码哈希的实现」），不是随机
    抽样出来的统计样本。一个模型在它们上面响，就是这个模型在这个阈值下不可信。
    它们跨标定集与验证集，所以这条约束对**全部**难负例生效。

    做法是**误报预算分配**：每一步都**精确**评估并集（模型之间的重叠算得准），
    在仍然满足三条约束的前提下选收益最大的那一步——先动的那个模型不会把预算
    吃光，后面的模型就永远调不到位。

    Args:
        scores: ``{模型名: 分数数组}``（标定集上的分数）。
        y: 标签数组。
        target_fpr: 并集允许的误报率上限。
        per_model_fpr: 单个模型允许的误报率上限。
        hard_scores: ``{模型名: 该模型在全部难负例上的分数}``。

    Returns:
        ``(阈值字典, 标定集上的指标)``。
    """
    grid = [round(x, 2) for x in np.arange(0.02, 1.0, 0.02)]
    ths = {k: 1.01 for k in scores}  # 1.01 超过任何概率，等于「这个模型不参与」
    best = _metrics(np.zeros(len(y), dtype=bool), y)
    negatives = y == 0
    while True:
        candidate = None
        for key in scores:
            for t in grid:
                if t >= ths[key]:
                    continue
                if float(np.mean(scores[key][negatives] >= t)) > per_model_fpr:
                    continue
                if hard_scores is not None and bool(
                    np.any(hard_scores[key] >= t)
                ):
                    continue
                trial = dict(ths)
                trial[key] = t
                blocked = np.zeros(len(y), dtype=bool)
                for k in scores:
                    blocked |= scores[k] >= trial[k]
                m = _metrics(blocked, y)
                if m["fpr"] > target_fpr or m["recall"] <= best["recall"] + 1e-12:
                    continue
                if candidate is None or m["recall"] > candidate[1]["recall"]:
                    candidate = (trial, m)
        if candidate is None:
            return ths, best
        ths, best = candidate


def _report(title: str, m: dict) -> None:
    """打印一行指标。"""
    print(
        f"  {title:<22} 精确率 {m['precision']:.3f}  召回 {m['recall']:.3f}  "
        f"误报 {m['fpr']:.4f}  (TP {m['tp']} / FP {m['fp']} / FN {m['fn']} / TN {m['tn']})"
    )


def main(argv: list[str] | None = None) -> int:
    """命令行入口。

    Args:
        argv: 命令行参数（默认取 ``sys.argv[1:]``）。

    Returns:
        进程退出码。
    """
    ap = argparse.ArgumentParser(description="敏感词前置过滤评估")
    ap.add_argument("--model-dir", type=Path, default=None, help="模型目录（默认用配置里的）")
    ap.add_argument("--target-fpr", type=float, default=0.01, help="标定时的并集误报率上限")
    ap.add_argument(
        "--per-model-fpr",
        type=float,
        default=0.005,
        help="标定时的单模型误报率上限（防止最吵的模型吃掉整个预算）",
    )
    ap.add_argument("--rules-only", action="store_true", help="只评规则层，不加载模型")
    args = ap.parse_args(argv)

    texts, labels, stats = build_dataset()
    y = np.asarray(labels)

    rng = np.random.default_rng(SEED)
    order = rng.permutation(len(texts))
    n_val = max(1, int(len(texts) * 0.2))
    val_set = set(order[:n_val].tolist())
    tr = [i for i in range(len(texts)) if i not in val_set]
    va = [i for i in range(len(texts)) if i in val_set]

    # 两种输入形态：短句 + 包在正常上下文里的长句。
    tr_all = [texts[i] for i in tr] + [embed_text(texts[i], i) for i in tr]
    va_all = [texts[i] for i in va] + [embed_text(texts[i], i) for i in va]
    y_tr = np.concatenate([y[tr], y[tr]])
    y_va = np.concatenate([y[va], y[va]])

    print("=" * 78)
    print("敏感词前置过滤 —— 评估报告")
    print("=" * 78)
    print(f"语料：{len(texts)} 条（正 {sum(labels)} / 负 {len(labels) - sum(labels)}）")
    print(f"  各类别：{ {k: v for k, v in stats.items() if isinstance(v, int)} }")
    print(f"划分：标定集 {len(tr_all)} 条（短+长），验证集 {len(va_all)} 条（短+长）")

    # ---------------- 规则层 ----------------
    rule_tr = np.array([match_keywords(t) is not None for t in tr_all])
    rule_va = np.array([match_keywords(t) is not None for t in va_all])
    print(f"\n【规则层】（静态短语表，人工编写）")
    _report("标定集", _metrics(rule_tr, y_tr))
    _report("验证集", _metrics(rule_va, y_va))

    if args.rules_only:
        print("\n--rules-only：跳过模型层。")
        return 0

    # ---------------- 模型层 ----------------
    from app.config.settings import settings

    model_dir = args.model_dir or Path(settings.sensitive_model_dir)
    if not (model_dir / "manifest.json").exists():
        print(
            f"\n模型不在 {model_dir}，先跑 `python -m scripts.fetch_sensitive_model`。",
            file=sys.stderr,
        )
        return 1

    filt = SensitiveFilter(
        model_dir=model_dir,
        verify_checksum=False,
        # 阈值全开：**四个模型都要参与打分**，否则标定时根本无从比较——
        # 阈值 ≥ 1.0 的模型运行时是不加载的（省 103 MB），但评估时必须全在，
        # 否则「它有没有用」这个问题就永远问不出来。
        thresholds={key: 0.0 for key, _ in MODEL_KEYS},
    )
    filt.load()
    scored_tr = filt.score_batch(tr_all)
    scored_va = filt.score_batch(va_all)
    prob_tr = {k: np.array([s[k][0] for s in scored_tr]) for k, _ in MODEL_KEYS}
    prob_va = {k: np.array([s[k][0] for s in scored_va]) for k, _ in MODEL_KEYS}

    # 难负例在**两个划分里都要算**：它们是专门设计出来抓误报的对抗样本，
    # 是规格的一部分，不是随机抽样出来的统计量。
    text_index = {t: i for i, t in enumerate(texts)}
    hard_idx = {text_index[t] for t in HARD_NEGATIVE if t in text_index}
    hard_tr_pos = [k for k, i in enumerate(tr) if i in hard_idx] + [
        k + len(tr) for k, i in enumerate(tr) if i in hard_idx
    ]
    hard_va_pos = [k for k, i in enumerate(va) if i in hard_idx] + [
        k + len(va) for k, i in enumerate(va) if i in hard_idx
    ]
    hard_scores = {
        key: np.concatenate([prob_tr[key][hard_tr_pos], prob_va[key][hard_va_pos]])
        for key, _ in MODEL_KEYS
    }

    ths, cal = _calibrate(
        prob_tr, y_tr, args.target_fpr, args.per_model_fpr, hard_scores=hard_scores
    )
    print(f"\n【模型层】（四个现成中文审核模型，int8）")
    print(
        f"  标定出的阈值（并集误报 ≤ {args.target_fpr:.1%}，"
        f"单模型误报 ≤ {args.per_model_fpr:.1%}）："
    )
    for k, _ in MODEL_KEYS:
        print(f"    {k:<10} {ths[k]:.2f}")
    model_tr = np.zeros(len(y_tr), dtype=bool)
    model_va = np.zeros(len(y_va), dtype=bool)
    for k, _ in MODEL_KEYS:
        model_tr |= prob_tr[k] >= ths[k]
        model_va |= prob_va[k] >= ths[k]
    _report("标定集", cal)
    _report("验证集", _metrics(model_va, y_va))

    # ---------------- 并集 ----------------
    union_tr = model_tr | rule_tr
    union_va = model_va | rule_va
    print(f"\n【规则层 + 模型层】（实际生效的判定）")
    _report("标定集", _metrics(union_tr, y_tr))
    m_union = _metrics(union_va, y_va)
    _report("验证集", m_union)

    # ---------------- 分类别召回 ----------------
    pos_of = {t: i for i, t in enumerate(texts)}
    va_position = {idx: k for k, idx in enumerate(va)}
    print("\n【验证集·分类别召回】")
    for cat, items in CATS.items():
        idxs = [va_position[pos_of[s]] for s in items if s in pos_of and pos_of[s] in va_position]
        if not idxs:
            continue
        hit = int(np.sum(union_va[idxs]))
        print(f"  {cat:<10} {hit}/{len(idxs)} = {hit / len(idxs):.2f}")

    # ---------------- 建议 ----------------
    print("\n【建议】把上面的阈值写进 app/core/sensitive.py::DEFAULT_THRESHOLDS")
    print(f"  DEFAULT_THRESHOLDS = {{{', '.join(f'{k!r}: {ths[k]:.2f}' for k, _ in MODEL_KEYS)}}}")
    print(
        f"\n注意：规则层的召回率**偏乐观**——短语表是照着五大类的典型说法写的，"
        f"不是在一个它没见过的数据集上测的。\n模型层的数字是干净的：那些模型从没见过这份语料。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
