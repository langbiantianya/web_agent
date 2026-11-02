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

## 阈值是怎么定的

阈值是**产品决策**（误报率与召回率的取舍点），但它必须由**没参与训练的
数据**来定，否则报出来的数字就是自己调出来的。两条规矩：

1. 阈值用 **K 折袋外（OOF）预测**选——每条文本都有一个「模型没见过它」的
   分数，样本量是全量语料，比单次 20% 切分稳得多（实测单次切分选出的
   阈值会把召回压掉近一半）。
2. :mod:`scripts.sensitive_eval_corpus` 那份**留出语料只用于事后报告**，
   绝不参与选阈值。

训完拿 :mod:`scripts.eval_sensitive` 从三个独立角度复核：留出集、训练集
（已拟合，只作对照）、仓库自身的中文文档段落。
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
    _normalize,
    _reference_grams,
    _view_windows,
    _window_grams,
)
from scripts.sensitive_corpus import embed_text

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "app" / "core" / "sensitive_model.npz"

# 阈值选择的目标：验证集上的**误报率**上限。
# 宁可漏检也不误杀——一个老是拦错正常请求的过滤器，用户会直接绕开它。
TARGET_FPR = 0.02


def _view_features(
    text: str, window: int, stride: int, dim: int
) -> tuple[list[int], list[int], np.ndarray]:
    """把一段文本展开成「窗口 → gram 哈希」的样本。

    切窗与 gram 展开**直接复用** :mod:`app.core.sensitive` 里的
    :func:`~app.core.sensitive._window_grams`——训练与推理走同一条路径，
    是这类模型不出错的前提（两边口径不一致，训出来的权重在推理时全是噪声，
    而这种错在测试里很难看出来）。

    Args:
        text: 原始文本。
        window: 窗口长度。
        stride: 步长。
        dim: 哈希空间维度。

    Returns:
        ``(gram_idx, window_id, stop_mask)``——每个 gram 的哈希槽位、它属于
        哪个窗口，以及它是不是「停用字的单字特征」（是则不计入证据）。
    """
    gram_idx: list[int] = []
    window_id: list[int] = []
    stop_mask: list[bool] = []
    wid = 0
    for view, seed in ((_normalize(text), 0), (_compact(text), 1)):
        for grams, mask in _window_grams(view, seed, window, stride, dim):
            gram_idx.extend(grams.tolist())
            window_id.extend([wid] * len(grams))
            stop_mask.extend(mask.tolist())
            wid += 1
    return gram_idx, window_id, np.asarray(stop_mask, dtype=bool)


def build_matrix(
    texts: list[str],
    labels: list[int],
    window: int,
    stride: int,
    dim: int,
    max_chars: int,
    pos_weight: float = 1.0,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """把数据集展开成可训练的元组。

    Args:
        texts: 训练文本。
        labels: 每条文本的标签（1=敏感）。
        window: 滑窗长度。
        stride: 滑窗步长。
        dim: 哈希空间维度。
        max_chars: 单条文本最多扫描多少字。
        pos_weight: 正例的样本权重（负例恒为 1）。

    Returns:
        ``(gram_idx, window_id, y, stop_mask, sample_weight)``——gram 槽位、窗口
        编号、窗口标签、「停用字单字特征」的掩码（被掩的 gram 不计入证据，
        详见 :data:`app.core.sensitive.STOP_CHARS`），以及每个窗口的样本权重。

    样本权重在这里算而不是在 :func:`train` 里：窗口是按文本切出来的，「这个
    窗口属于哪条文本」这个信息只有切窗口的时候才知道。
    """
    all_idx: list[int] = []
    all_wid: list[int] = []
    all_stop: list[bool] = []
    ys: list[int] = []
    sws: list[float] = []
    wid_base = 0
    for text, label in zip(texts, labels, strict=True):
        idx, wid, stop = _view_features(text[:max_chars], window, stride, dim)
        if not idx:
            continue
        all_idx.extend(idx)
        all_wid.extend(w + wid_base for w in wid)
        all_stop.extend(stop.tolist())
        n_win = (max(wid) + 1) if wid else 0
        ys.extend([label] * n_win)
        sws.extend([pos_weight if label == 1 else 1.0] * n_win)
        wid_base += n_win
    return (
        np.asarray(all_idx, dtype=np.int64),
        np.asarray(all_wid, dtype=np.int64),
        np.asarray(ys, dtype=np.float64),
        np.asarray(all_stop, dtype=bool),
        np.asarray(sws, dtype=np.float64),
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
    stop_mask: np.ndarray | None = None,
    sample_weight: np.ndarray | None = None,
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
        stop_mask: 停用字单字特征的掩码；这些 gram 既不贡献分数，也不接受
            梯度——它们的槽位最终只会被 L2 压向 0。
        sample_weight: 每个窗口的样本权重（默认全 1）。正例加权能把偏置项
            拉向「敏感」一侧：难负例一多，语料的正负比会变成 1:3，模型学到的
            先验跟着偏负，正例的分数被整体压低，召回跟着掉。

    Returns:
        ``(weights, bias)``。
    """
    w = np.zeros(dim, dtype=np.float64)
    b = 0.0
    n = len(y)
    if n == 0:
        return w, b
    masked = (
        np.zeros(len(gram_idx), dtype=bool)
        if stop_mask is None
        else np.asarray(stop_mask, dtype=bool)
    )
    sw = np.ones(n, dtype=np.float64) if sample_weight is None else np.asarray(
        sample_weight, dtype=np.float64
    )
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
        contrib[masked] = 0.0
        logits = np.bincount(window_id, weights=contrib, minlength=n) + b
        p = 1.0 / (1.0 + np.exp(-logits))
        g = (p - y) * sw / n
        grad_samples = g[window_id] * scale[window_id]
        # 停用字的单字特征不接受梯度：它们的权重不该被语料的词频牵着走。
        grad_samples[masked] = 0.0
        grad_w = np.bincount(gram_idx, weights=grad_samples, minlength=dim)
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

    与推理侧（:meth:`app.core.sensitive.SensitiveFilter.check`）走同一条特征
    展开路径，包括停用字掩码——否则训练时的分数和推理时的分数对不上，
    挑出来的阈值就是错的。

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
        idx, wid, stop = _view_features(text[:max_chars], window, stride, dim)
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
        contrib[stop] = 0.0
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


def kfold_oof_scores(
    texts: list[str],
    labels: list[int],
    emb_texts: list[str],
    *,
    folds: int,
    window: int,
    stride: int,
    dim: int,
    max_chars: int,
    epochs: int,
    lr: float,
    l2: float,
    norm_alpha: float,
    pos_weight: float,
    seed: int,
    verbose: bool = True,
) -> tuple[np.ndarray, np.ndarray]:
    """K 折交叉验证，产出每条文本的**袋外（OOF）分数**。

    ## 为什么不用单次切分

    语料 800 多条，按 20% 切一次，验证集只剩 160 条——误报率每 1% 只对应
    两条样本，选出来的阈值极不稳定：实测同一份语料上切出 0.870，把召回压到
    0.46，而真正合适的阈值在 0.75 附近。

    ## 为什么不用留出集选阈值

    :mod:`scripts.sensitive_eval_corpus` 那份留出语料**只用于事后报告**。
    拿它挑阈值，报告出来的数字就是自己调出来的，不算独立评测。

    OOF 的每个分数都来自「没见过这条文本」的模型，样本量又是全量语料，
    两头都占。

    Args:
        texts: 全部训练文本。
        labels: 全部标签（1=敏感）。
        emb_texts: 与 ``texts`` 一一对应的「包在上下文里」的版本。
        folds: 折数（≥2）。
        window: 滑窗长度。
        stride: 滑窗步长。
        dim: 哈希空间维度。
        max_chars: 单条文本最多扫描多少字。
        epochs: 每折的训练轮数。
        lr: 学习率。
        l2: L2 正则系数。
        norm_alpha: 长度归一化指数。
        pos_weight: 正例的样本权重。
        seed: 划分用的随机种子。
        verbose: 是否打印每折进度。

    Returns:
        ``(短句分数, 嵌入上下文后的分数)``——两条数组与 ``texts`` 等长。
    """
    n = len(texts)
    rng = np.random.default_rng(seed)
    order = rng.permutation(n)
    parts = np.array_split(order, folds)
    oof_short = np.zeros(n, dtype=np.float64)
    oof_emb = np.zeros(n, dtype=np.float64)

    for k, part in enumerate(parts):
        hold = set(part.tolist())
        tr_idx = [j for j in range(n) if j not in hold]
        gram_idx, window_id, y, stop_mask, sample_weight = build_matrix(
            [texts[j] for j in tr_idx],
            [labels[j] for j in tr_idx],
            window,
            stride,
            dim,
            max_chars,
            pos_weight=pos_weight,
        )
        w, b = train(
            gram_idx,
            window_id,
            y,
            dim,
            epochs=epochs,
            lr=lr,
            l2=l2,
            norm_alpha=norm_alpha,
            reference_grams=_reference_grams(window),
            stop_mask=stop_mask,
            sample_weight=sample_weight,
        )
        held = part.tolist()
        s1 = score_texts(
            [texts[j] for j in held],
            w,
            b,
            window,
            stride,
            dim,
            max_chars,
            norm_alpha=norm_alpha,
        )
        s2 = score_texts(
            [emb_texts[j] for j in held],
            w,
            b,
            window,
            stride,
            dim,
            max_chars,
            norm_alpha=norm_alpha,
        )
        for pos, j in enumerate(held):
            oof_short[j] = s1[pos]
            oof_emb[j] = s2[pos]
        if verbose:
            print(f"  折 {k + 1}/{folds} 完成（留出 {len(held)} 条）")
    return oof_short, oof_emb


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
    ap.add_argument(
        "--pos-weight",
        type=float,
        default=1.0,
        help="正例的样本权重（负例恒为 1）。难负例一多，正负比就偏负，"
        "模型先验跟着偏负、正例分数被整体压低，召回跟着掉；"
        "给正例加权把偏置拉回「敏感」一侧",
    )
    ap.add_argument("--lr", type=float, default=0.5, help="学习率（每轮按 0.98 衰减）")
    ap.add_argument("--l2", type=float, default=1e-6, help="L2 正则系数")
    ap.add_argument(
        "--cv-folds",
        type=int,
        default=5,
        help="选阈值用的交叉验证折数；1 表示退回单次 20%% 切分（快但阈值不稳）",
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

    # 训练集**不做**上下文增强。想过「给每条样本补一个包在正常上下文里的
    # 版本」，但那条路是错的：窗口级监督会把长文本里**每一段正常上下文**都标成
    # 敏感，模型于是学成「上下文本身也敏感」，实测长句召回从 0.88 掉到 0.52。
    # 要真正修好「敏感短语被正常句子稀释」，得给窗口打**逐窗口**的标签
    # （只有盖住敏感短语的那几个窗口算正例），那是另一套标注口径的事。
    # 这里保持语料原样，把问题如实记在文档里。

    # 阈值在**两种形态的合集**上选——真实输入是短句还是长句都有，只按一种挑
    # 阈值，另一半的召回就会塌。
    emb_texts = [embed_text(t, i) for i, t in enumerate(texts)]

    t0 = time.perf_counter()
    if args.cv_folds >= 2:
        print(f"K 折交叉验证（{args.cv_folds} 折）选阈值…")
        short_scores, emb_scores = kfold_oof_scores(
            texts,
            labels,
            emb_texts,
            folds=args.cv_folds,
            window=args.window,
            stride=args.stride,
            dim=args.dim,
            max_chars=args.max_chars,
            epochs=args.epochs,
            lr=args.lr,
            l2=args.l2,
            norm_alpha=args.norm_alpha,
            pos_weight=args.pos_weight,
            seed=args.seed,
        )
        sel_scores = np.concatenate([short_scores, emb_scores])
        sel_labels = np.concatenate(
            [np.asarray(labels, dtype=np.float64), np.asarray(labels, dtype=np.float64)]
        )
        metric_labels = np.asarray(labels, dtype=np.float64)
    else:
        # 单次切分（--cv-folds 1）：快，但阈值不稳。
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
        va_emb_texts = [embed_text(t, i) for i, t in enumerate(va_texts)]
        fold_gram, fold_wid, fold_y, fold_stop, fold_sw = build_matrix(
            tr_texts,
            tr_labels,
            args.window,
            args.stride,
            args.dim,
            args.max_chars,
            pos_weight=args.pos_weight,
        )
        fold_w, fold_b = train(
            fold_gram,
            fold_wid,
            fold_y,
            args.dim,
            epochs=args.epochs,
            lr=args.lr,
            l2=args.l2,
            norm_alpha=args.norm_alpha,
            reference_grams=_reference_grams(args.window),
            stop_mask=fold_stop,
            sample_weight=fold_sw,
        )
        short_scores = score_texts(
            va_texts, fold_w, fold_b, args.window, args.stride,
            args.dim, args.max_chars, norm_alpha=args.norm_alpha,
        )
        emb_scores = score_texts(
            va_emb_texts, fold_w, fold_b, args.window, args.stride,
            args.dim, args.max_chars, norm_alpha=args.norm_alpha,
        )
        sel_scores = np.concatenate([short_scores, emb_scores])
        sel_labels = np.concatenate(
            [np.asarray(va_labels, dtype=np.float64), np.asarray(va_labels, dtype=np.float64)]
        )
        metric_labels = np.asarray(va_labels, dtype=np.float64)

    threshold, metrics = pick_threshold(sel_scores, sel_labels)
    short_metrics = evaluate(short_scores, metric_labels, threshold)
    embedded_metrics = evaluate(emb_scores, metric_labels, threshold)

    # 最终模型用**全部**语料训练：阈值已经由袋外分数定好了，没有理由再留
    # 一份数据不用。
    gram_idx, window_id, y, stop_mask, sample_weight = build_matrix(
        texts,
        labels,
        args.window,
        args.stride,
        args.dim,
        args.max_chars,
        pos_weight=args.pos_weight,
    )
    print(f"训练样本：{len(texts)} 条文本 → {len(y)} 个窗口，{len(gram_idx)} 个 gram")
    w, b = train(
        gram_idx,
        window_id,
        y,
        args.dim,
        epochs=args.epochs,
        lr=args.lr,
        l2=args.l2,
        norm_alpha=args.norm_alpha,
        reference_grams=_reference_grams(args.window),
        stop_mask=stop_mask,
        sample_weight=sample_weight,
    )
    train_s = time.perf_counter() - t0

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
        "选阈值用的袋外集（短句+长句）：精确率 {precision} 召回率 {recall} F1 {f1} 误报率 {fpr}".format(
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
        "全量集（已拟合，只作对照）：精确率 {precision} 召回率 {recall} F1 {f1} 误报率 {fpr}".format(
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
        "oof": metrics,
        "oof_short": short_metrics,
        "oof_embedded": embedded_metrics,
        "cv_folds": args.cv_folds,
        "full": all_metrics,
        "ngram_sizes": list(NGRAM_SIZES),
        "window": args.window,
        "stride": args.stride,
        "norm_alpha": args.norm_alpha,
        "pos_weight": args.pos_weight,
        "lr": args.lr,
        "l2": args.l2,
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
