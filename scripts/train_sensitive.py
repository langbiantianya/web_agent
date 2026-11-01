"""训练敏感词过滤器（V3.4）。

模型是**字符 n-gram + 逻辑回归**，纯 numpy 实现，几十秒就能训完、几 MB 内存、
推理百微秒级。特征提取直接复用 :mod:`app.core.sensitive` 里的函数——训练与推理
用同一套代码，是这类模型不出错的前提（两边口径不一致，训出来的权重在推理时
全是噪声，而这种错在测试里很难看出来）。

用法：

```bash
# 用内置种子语料训练（默认）
python -m scripts.train_sensitive

# 用自己的数据重训
python -m scripts.train_sensitive --data my_corpus.jsonl --out app/core/sensitive_model.npz

# 只看评估，不写文件
python -m scripts.train_sensitive --dry-run
```

自定义语料是 JSONL，每行 ``{"text": "...", "label": 1|0}``，label=1 表示敏感。
**难负例很重要**：含敏感词但无害的样本（label=0）教模型区分「词」和「意图」，
没有它们，误杀率会高得没法用。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

from app.core.sensitive import (
    DEFAULT_DIM,
    DEFAULT_MAX_CHARS,
    DEFAULT_NORM_ALPHA,
    DEFAULT_STRIDE,
    DEFAULT_WINDOW,
    NGRAM_SIZES,
    NORM_CLIP_HIGH,
    NORM_CLIP_LOW,
    _compact,
    _ngram_hash_table,
    _normalize,
    _reference_grams,
)
from scripts.sensitive_corpus import embed_text

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "app" / "core" / "sensitive_model.npz"

# 阈值选择的目标：验证集上的**误报率**上限。
# 宁可漏检也不误杀——一个老是拦错正常请求的过滤器，用户会直接绕开它。
TARGET_FPR = 0.02


def _windows_of(text: str, window: int, stride: int) -> list[tuple[int, int]]:
    """切出文本的所有滑窗。

    与 :meth:`app.core.sensitive.SensitiveFilter._score_view` 的切法**完全一致**
    ——这是训练/推理口径统一的前提。

    Args:
        text: 已归一化的文本。
        window: 窗口长度。
        stride: 步长。

    Returns:
        ``[(起, 止), ...]``。
    """
    n = len(text)
    if n == 0:
        return []
    if n <= window:
        return [(0, n)]
    starts = list(range(0, n - window + 1, stride))
    if starts[-1] + window < n:
        starts.append(n - window)
    return [(s, min(n, s + window)) for s in starts]


def _view_features(
    text: str, window: int, stride: int, dim: int
) -> tuple[list[int], list[int]]:
    """把一段文本展开成「窗口 → gram 哈希」的样本。

    两个视图都算：原文视图（保结构）与压紧视图（去空格标点，抗插空混淆），
    两者的 gram 用不同哈希种子，避免权重互相污染。

    gram 的哈希**整段算一次、窗口间再切片**——窗口互相重叠，逐窗算等于把同样
    的哈希重复做 5 遍。这里的切法必须与 :meth:`app.core.sensitive.
    SensitiveFilter._score_view` **完全一致**，否则训出来的权重在推理时全是噪声。

    Args:
        text: 原始文本。
        window: 窗口长度。
        stride: 步长。
        dim: 哈希空间维度。

    Returns:
        ``(gram_idx, window_id)``——每个 gram 的哈希槽位，以及它属于哪个窗口。
    """
    gram_idx: list[int] = []
    window_id: list[int] = []
    wid = 0
    for view, seed in ((_normalize(text), 0), (_compact(text), 1)):
        h1, h2, h3 = _ngram_hash_table(view, seed)
        h1 = h1 % dim
        h2 = h2 % dim
        h3 = h3 % dim
        for start, end in _windows_of(view, window, stride):
            parts = [h1[start:end]]
            if end - start >= 2:
                parts.append(h2[start : end - 1])
            if end - start >= 3:
                parts.append(h3[start : end - 2])
            grams = np.concatenate(parts)
            gram_idx.extend(grams.tolist())
            window_id.extend([wid] * len(grams))
            wid += 1
    return gram_idx, window_id


def build_matrix(
    texts: list[str],
    labels: list[int],
    window: int,
    stride: int,
    dim: int,
    max_chars: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """把数据集展开成可训练的三元组。

    Returns:
        ``(gram_idx, window_id, y)``——gram 槽位、窗口编号、窗口标签。
    """
    all_idx: list[int] = []
    all_wid: list[int] = []
    ys: list[int] = []
    wid_base = 0
    for text, label in zip(texts, labels, strict=True):
        idx, wid = _view_features(text[:max_chars], window, stride, dim)
        if not idx:
            continue
        all_idx.extend(idx)
        all_wid.extend(w + wid_base for w in wid)
        ys.extend([label] * (max(wid) + 1 if wid else 0))
        wid_base += (max(wid) + 1) if wid else 0
    return (
        np.asarray(all_idx, dtype=np.int64),
        np.asarray(all_wid, dtype=np.int64),
        np.asarray(ys, dtype=np.float64),
    )


def train(
    gram_idx: np.ndarray,
    window_id: np.ndarray,
    y: np.ndarray,
    dim: int,
    epochs: int = 700,
    lr: float = 0.5,
    l2: float = 1e-6,
    norm_alpha: float = DEFAULT_NORM_ALPHA,
    reference_grams: int | None = None,
) -> tuple[np.ndarray, float]:
    """全批量梯度下降训练逻辑回归。

    梯度用 ``np.bincount`` 做散射加——不需要 scipy 的稀疏矩阵，纯 numpy 就够，
    而且对这种「几百万 gram、几十万个槽位」的规模，bincount 反而比转成稀疏
    矩阵更省事也更快。

    Args:
        gram_idx: 每个 gram 的哈希槽位。
        window_id: 每个 gram 所属的窗口编号。
        y: 每个窗口的标签。
        dim: 哈希空间维度。
        epochs: 训练轮数。
        lr: 学习率（每轮按 0.98 衰减）。
        l2: L2 正则系数。
        norm_alpha: 长度归一化指数（0 表示纯求和）。
        reference_grams: 归一化的参考长度（满窗的 gram 数）。

    Returns:
        ``(weights, bias)``。
    """
    w = np.zeros(dim, dtype=np.float64)
    b = 0.0
    n = len(y)
    if n == 0:
        return w, b
    counts = np.bincount(window_id, minlength=n).astype(np.float64)
    if reference_grams is None:
        reference_grams = int(counts.max())
    scale = np.clip(
        (reference_grams / np.maximum(counts, 1.0)) ** norm_alpha,
        NORM_CLIP_LOW,
        NORM_CLIP_HIGH,
    )
    for epoch in range(epochs):
        contrib = w[gram_idx] * scale[window_id]
        logits = np.bincount(window_id, weights=contrib, minlength=n) + b
        p = 1.0 / (1.0 + np.exp(-logits))
        g = (p - y) / n
        grad_w = np.bincount(
            gram_idx, weights=g[window_id] * scale[window_id], minlength=dim
        )
        grad_b = float(g.sum())
        cur_lr = lr * (0.98**epoch)
        w -= cur_lr * grad_w + l2 * cur_lr * w
        b -= cur_lr * grad_b
    return w, b


def score_texts(
    texts: list[str],
    w: np.ndarray,
    b: float,
    window: int,
    stride: int,
    dim: int,
    max_chars: int,
    norm_alpha: float = DEFAULT_NORM_ALPHA,
) -> np.ndarray:
    """给每条文本打一个分（取它所有窗口里的最高分）。

    Args:
        texts: 待打分的文本。
        w: 权重。
        b: 偏置。
        window: 窗口长度。
        stride: 步长。
        dim: 哈希空间维度。
        max_chars: 单条文本最多扫描多少字。
        norm_alpha: 长度归一化指数（必须与训练时一致）。

    Returns:
        每条文本的最高窗口概率。
    """
    reference = _reference_grams(window)
    out = np.zeros(len(texts), dtype=np.float64)
    for i, text in enumerate(texts):
        idx, wid = _view_features(text[:max_chars], window, stride, dim)
        if not idx:
            continue
        idx_arr = np.asarray(idx, dtype=np.int64)
        wid_arr = np.asarray(wid, dtype=np.int64)
        counts = np.bincount(wid_arr).astype(np.float64)
        scale = np.clip(
            (reference / np.maximum(counts, 1.0)) ** norm_alpha,
            NORM_CLIP_LOW,
            NORM_CLIP_HIGH,
        )
        contrib = w[idx_arr] * scale[wid_arr]
        logits = np.bincount(wid_arr, weights=contrib) + b
        p = 1.0 / (1.0 + np.exp(-logits))
        out[i] = float(p.max())
    return out


def evaluate(
    scores: np.ndarray, labels: np.ndarray, threshold: float
) -> dict[str, float]:
    """在给定阈值下算混淆矩阵。

    Args:
        scores: 每条文本的分数。
        labels: 每条文本的真值（1=敏感）。
        threshold: 判定阈值。

    Returns:
        精确率 / 召回率 / F1 / 误报率 / 准确率。
    """
    pred = scores >= threshold
    truth = labels == 1
    tp = int(np.sum(pred & truth))
    fp = int(np.sum(pred & ~truth))
    fn = int(np.sum(~pred & truth))
    tn = int(np.sum(~pred & ~truth))
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    fpr = fp / (fp + tn) if (fp + tn) else 0.0
    acc = (tp + tn) / len(labels) if len(labels) else 0.0
    return {
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "fpr": round(fpr, 4),
        "accuracy": round(acc, 4),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
    }


def pick_threshold(scores: np.ndarray, labels: np.ndarray) -> tuple[float, dict]:
    """选阈值：在误报率不超过目标的前提下，召回率最高。

    Args:
        scores: 验证集上的文本分数。
        labels: 验证集真值。

    Returns:
        ``(阈值, 该阈值下的评估结果)``。
    """
    best = (0.0, evaluate(scores, labels, 1.0))
    for t in np.arange(0.05, 0.999, 0.005):
        m = evaluate(scores, labels, float(t))
        if m["fpr"] <= TARGET_FPR and m["recall"] > best[1]["recall"]:
            best = (float(t), m)
    # 目标 FPR 下一个正例都拦不住的话，退而求其次挑 F1 最高的
    if best[1]["recall"] == 0.0:
        best_f1 = (0.5, evaluate(scores, labels, 0.5))
        for t in np.arange(0.05, 0.999, 0.005):
            m = evaluate(scores, labels, float(t))
            if m["f1"] > best_f1[1]["f1"]:
                best_f1 = (float(t), m)
        best = best_f1
    return best


def load_jsonl(path: Path) -> tuple[list[str], list[int]]:
    """读自定义语料（JSONL）。

    Args:
        path: 语料文件路径。

    Returns:
        ``(texts, labels)``。

    Raises:
        ValueError: 文件里有格式不对的行。
    """
    texts: list[str] = []
    labels: list[int] = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            texts.append(str(obj["text"]))
            labels.append(int(obj["label"]))
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"语料第 {i} 行格式不对：{exc}") from exc
    return texts, labels


def main(argv: list[str] | None = None) -> int:
    """训练入口。

    Args:
        argv: 命令行参数（默认取 ``sys.argv[1:]``）。

    Returns:
        进程退出码。
    """
    ap = argparse.ArgumentParser(description="训练敏感词过滤器")
    ap.add_argument("--data", type=Path, help="自定义语料 JSONL（不给就用内置种子语料）")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="模型输出路径")
    ap.add_argument("--epochs", type=int, default=700, help="训练轮数")
    ap.add_argument("--dim", type=int, default=DEFAULT_DIM, help="哈希空间维度")
    ap.add_argument("--window", type=int, default=DEFAULT_WINDOW, help="滑窗长度")
    ap.add_argument("--stride", type=int, default=DEFAULT_STRIDE, help="滑窗步长")
    ap.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS, help="单条最多扫描字符数")
    ap.add_argument(
        "--norm-alpha",
        type=float,
        default=DEFAULT_NORM_ALPHA,
        help="窗口聚合的长度归一化指数（0=纯求和，1=按平均证据量）",
    )
    ap.add_argument("--seed", type=int, default=20241021, help="随机种子（划分用）")
    ap.add_argument("--dry-run", action="store_true", help="只评估不写文件")
    args = ap.parse_args(argv)

    if args.data:
        texts, labels = load_jsonl(args.data)
        stats = {"source": str(args.data), "n": len(texts)}
    else:
        from scripts.sensitive_corpus import build_dataset

        texts, labels, stats = build_dataset()
        stats["source"] = "内置种子语料"
    if len(texts) < 20:
        print("语料太少，训不出可用的模型", file=sys.stderr)
        return 1

    # 按**文本**划分训练/验证集——按窗口划分会让同一句话的窗口同时出现在两边，
    # 验证分数虚高，等于自欺欺人。
    rng = np.random.default_rng(args.seed)
    order = rng.permutation(len(texts))
    n_val = max(1, int(len(texts) * 0.2))
    val_idx = set(order[:n_val].tolist())
    tr_texts = [t for i, t in enumerate(texts) if i not in val_idx]
    tr_labels = [l for i, l in enumerate(labels) if i not in val_idx]
    va_texts = [t for i, t in enumerate(texts) if i in val_idx]
    va_labels = [l for i, l in enumerate(labels) if i in val_idx]

    # 训练集**不做**上下文增强。想过「给每条样本补一个包在正常上下文里的
    # 版本」，但那条路是错的：窗口级监督会把长文本里**每一段正常上下文**都标成
    # 敏感，模型于是学成「上下文本身也敏感」，实测长句召回从 0.88 掉到 0.52。
    # 要真正修好「敏感短语被正常句子稀释」，得给窗口打**逐窗口**的标签
    # （只有盖住敏感短语的那几个窗口算正例），那是另一套标注口径的事。
    # 这里保持语料原样，把问题如实记在文档里。

    # 验证集同时评估两种形态：短句（语料原样）与长句（包在上下文里）。
    # 阈值在**两种形态的合集**上选——真实输入是哪种形态都有，只按一种挑
    # 阈值，另一半的召回就会塌。
    va_emb_texts = [embed_text(t, i) for i, t in enumerate(va_texts)]
    sel_texts = va_texts + va_emb_texts
    sel_labels = list(va_labels) + list(va_labels)

    t0 = time.perf_counter()
    gram_idx, window_id, y = build_matrix(
        tr_texts, tr_labels, args.window, args.stride, args.dim, args.max_chars
    )
    print(f"训练样本：{len(tr_texts)} 条文本 → {len(y)} 个窗口，{len(gram_idx)} 个 gram")
    w, b = train(
        gram_idx,
        window_id,
        y,
        args.dim,
        epochs=args.epochs,
        norm_alpha=args.norm_alpha,
        reference_grams=_reference_grams(args.window),
    )
    train_s = time.perf_counter() - t0

    va_scores = score_texts(
        va_texts,
        w,
        b,
        args.window,
        args.stride,
        args.dim,
        args.max_chars,
        norm_alpha=args.norm_alpha,
    )
    va_emb_scores = score_texts(
        va_emb_texts,
        w,
        b,
        args.window,
        args.stride,
        args.dim,
        args.max_chars,
        norm_alpha=args.norm_alpha,
    )
    sel_scores = np.concatenate([va_scores, va_emb_scores])
    threshold, metrics = pick_threshold(sel_scores, np.asarray(sel_labels))
    short_metrics = evaluate(va_scores, np.asarray(va_labels), threshold)
    embedded_metrics = evaluate(va_emb_scores, np.asarray(va_labels), threshold)

    all_scores = score_texts(
        texts,
        w,
        b,
        args.window,
        args.stride,
        args.dim,
        args.max_chars,
        norm_alpha=args.norm_alpha,
    )
    all_metrics = evaluate(all_scores, np.asarray(labels), threshold)

    print(f"\n训练耗时 {train_s:.1f}s")
    print(f"建议阈值 {threshold:.3f}")
    print(
        "验证集（短句+长句）：精确率 {precision} 召回率 {recall} F1 {f1} 误报率 {fpr}".format(
            **metrics
        )
    )
    print(
        "  其中短句：精确率 {precision} 召回率 {recall} 误报率 {fpr}".format(
            **short_metrics
        )
    )
    print(
        "  其中长句：精确率 {precision} 召回率 {recall} 误报率 {fpr}".format(
            **embedded_metrics
        )
    )
    print(
        "全量集：精确率 {precision} 召回率 {recall} F1 {f1} 误报率 {fpr}".format(
            **all_metrics
        )
    )

    # 把误杀样本和漏检样本原样打出来：阈值是给人看的数字，光看精确率/召回率
    # 不知道「到底哪条被误杀了」，调参就只能靠猜。
    fp_idx = [
        i
        for i, (s, l) in enumerate(zip(all_scores, labels, strict=True))
        if s >= threshold and l != 1
    ]
    fn_idx = [
        i
        for i, (s, l) in enumerate(zip(all_scores, labels, strict=True))
        if s < threshold and l == 1
    ]
    if fp_idx:
        print("\n被误杀的正常请求：")
        for i in fp_idx[:20]:
            print(f"  [{all_scores[i]:.3f}] {texts[i]}")
    if fn_idx:
        print("\n没拦住的敏感请求：")
        for i in fn_idx[:20]:
            print(f"  [{all_scores[i]:.3f}] {texts[i]}")

    if args.dry_run:
        return 0

    nz = np.nonzero(w)[0]
    meta = {
        "threshold": round(threshold, 3),
        "n_samples": len(texts),
        "n_windows": int(len(y)),
        "stats": stats,
        "validation": metrics,
        "validation_short": short_metrics,
        "validation_embedded": embedded_metrics,
        "full": all_metrics,
        "ngram_sizes": list(NGRAM_SIZES),
        "window": args.window,
        "stride": args.stride,
        "norm_alpha": args.norm_alpha,
        "seed": args.seed,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        args.out,
        w_idx=nz.astype(np.int32),
        w_val=w[nz].astype(np.float32),
        bias=np.float32(b),
        dim=np.int32(args.dim),
        meta=np.array(json.dumps(meta, ensure_ascii=False)),
    )
    size_kb = args.out.stat().st_size / 1024
    print(f"\n模型已写入 {args.out}（{size_kb:.1f} KB，非零权重 {len(nz)} 个）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
