"""敏感词前置过滤（V3.4）。

**提示词进入 LLM 之前先过一遍**，命中就拦下，不让它到模型那里。

## 为什么是「机器学习」而不是关键词表

关键词表（AC 自动机那套）只能挡住**字面完全一致**的词：写「怎么制作炸药」能挡，
写「怎么搞点炸的」挡不住；「杀 人」中间插个空格就漏。规则表永远追不上变形。

这里用的是**字符 n-gram + 逻辑回归**（纯 numpy，没有 sklearn 依赖）：

- 特征：文本的字符 1/2/3-gram，哈希到固定维度的稀疏空间；
- 模型：逻辑回归，权重是一维数组，推理就是一次**稀疏点积**；
- 打分：在文本上**滑窗**取最大窗口分数——窗口给出的是「哪一段有问题」，
  定位比整句打分准得多。

## 为什么快、为什么省

| 维度 | 做法 |
| --- | --- |
| 推理耗时 | 一次 gather + 一次 bincount，**百微秒级**（比一次 LLM 请求快四个数量级） |
| 常驻内存 | 稠密权重 1 MB（2^18 个 float32），模型文件本身只有几十 KB |
| 外部依赖 | **无**：只用 numpy，不加载任何模型框架，不起任何外部服务 |
| 进程模型 | 纯 CPU 计算，不占 GPU，不阻塞事件循环（同步计算，单条消息 < 1ms） |

## 为什么「不太严」

误杀一个正常请求比漏掉一条敏感词更伤——用户会直接不信任这个服务。所以：

1. 训练语料里塞了大量**难负例**：含敏感词但无害的请求（「帮我写一个检测敏感词的
   过滤器」「我要举报一个诈骗网站，怎么举报」）。它们让模型学会区分**词**和**意图**。
2. 阈值默认偏保守，宁可漏也不误杀。
3. 只看**窗口级**分数，不看整句——一句话里提到敏感话题不等于在提敏感请求。

## 命中之后

默认动作是**拒绝**（API 层返回 422），不是遮蔽也不是悄悄放行。理由：过滤器是
「安全前置」，它的价值在于**确定性地**不把内容送出去；遮蔽会让模型拿到语义完整
的请求（只是词被替换了），那种「看起来处理过了其实没处理」的中间态最危险。

命中后只有一条路径（拒绝），不做「遮蔽放行」的第二种行为——多一种行为就多一套
要测的分支，而遮蔽那一支的价值又恰恰在这里最可疑。

## 两道防线

1. **API 入口**：:func:`app.api.routes.conversation.send_message` 在**落库之前**
   就查一遍。命中直接 422 返回，用户消息不入库、不建 run、不消耗任何 token。
2. **LLM 边界**：:mod:`app.core.agent_runner` 在把消息交给模型之前再查一遍。

第二道不是多余的：第一道只能拦住「走 API 的新消息」，而历史消息、绕开 API 的
内部调用、别的服务复用本模块都逃得掉。有第二道，「进入 LLM 的内容一定被过滤过」
才是一条**对所有路径成立**的不变量，而不是一句约定。

## 模型的来源与重训

仓库里带了一份训练好的模型（``app/core/sensitive_model.npz``），开箱即用。
它是用 :mod:`scripts.train_sensitive` 里的**种子语料**训的——那份语料是几百条
短句，覆盖五大类（违法暴力 / 色情 / 政治敏感 / 隐私诈骗 / 仇恨歧视）。

**种子语料的规模决定了它的能力上限**：它能挡住常见形态，挡不住所有变形。
真要上生产，用自己的真实数据重训：

```bash
python -m scripts.train_sensitive --data my_corpus.jsonl --out app/core/sensitive_model.npz
```

语料格式是 JSONL，每行 ``{"text": "...", "label": 1|0}``，label=1 表示敏感。
重训只要几十秒（几百到几千条语料，纯 numpy 训练）。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import zlib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

# 哈希空间的维度：2^16 = 65536 个槽位，稠密权重 256 KB。
#
# 维度不是越大越好：种子语料只有几百条、几万个不同的 n-gram，维度给到 2^18
# 反而让模型有足够容量把训练样本背下来（实测验证集召回 0.69 → 0.75、全量
# 0.88 → 0.91）。**容量过剩是这个模型最容易踩的坑**，不是不够。
#
# 训练语料的 n-gram 词表通常只有几千到几万个，这个量级下哈希冲突可以忽略。
DEFAULT_DIM = 1 << 16

# n-gram 的阶数。1-gram 抓单字敏感词（「毒」），2-3 gram 抓词组。
#
# 这个常量是**对外的声明**：模型 meta 里记着它，换阶数等于换特征空间，旧模型
# 就不能用了。实现上 :func:`_ngram_hash_table` 按 (1, 2, 3) 三段返回哈希表——
# 两处必须同步改，改一处要记得改另一处。
NGRAM_SIZES = (1, 2, 3)

# 滑窗参数。窗口 20 字能装下「怎么做炸药」这类短语；步长 4 保证不漏。
#
# 窗口长度是**召回率的主要杠杆**，不是随手取的数：窗口太短，短正例（「哪里能
# 买到冰毒」只有 7 个字）只落到一个窗口里、证据被摊薄成很低的分，系统性漏检。
# 实测（种子语料、验证集、alpha=0.7）：W16 召回 0.76 / W20 0.76 / W24 0.80，
# 差别只有一两个样本；W20 是计算量与召回率的平衡点。
DEFAULT_WINDOW = 20
DEFAULT_STRIDE = 4

# 单条消息最多扫描多少字。超长的提示词（粘贴了一整篇文档）只扫前面这段——
# 敏感内容几乎总在开头，且这一步是为了**控住资源占用**。
DEFAULT_MAX_CHARS = 4000

# 判定为命中后，错误信息里给出的片段最多留多少字。
EXCERPT_CHARS = 24

# 窗口聚合的**长度归一化指数**。
#
# 直接把窗口里所有 gram 的权重相加，会有一个系统性的**长度偏置**：gram 总数
# 越多，和越大——一句话越长越容易被判敏感；而短文本（「哪里能买到冰毒」7 个字）
# 只有几十个 gram，同样的证据被摊薄成很低的分，于是系统性地漏检。
#
# 归一化做法是给每个窗口乘一个系数 ``(参考长度 / 实际长度) ** alpha``：
# alpha=0 等于不做归一化（原始的纯求和）；alpha 越大越接近「按平均证据量判」。
# 参考长度取**满窗**的 gram 总数，于是满窗系数恰好是 1，只有短窗被抬起来。
#
# 取 0.7 而不是 1.0：实测种子语料上，alpha=0.7 在验证集上召回率 0.76、误报 0；
# alpha=1.0 召回率反而回落（把「窗口里恰好有一段敏感」这个稀疏证据也一起稀释
# 了），而 alpha=0 漏检明显（短文本系统性低分）。
DEFAULT_NORM_ALPHA = 0.7

# 归一化系数的上下限。系数过大时短文本的分数会冲过头，误报抬头。
NORM_CLIP_LOW = 0.5
NORM_CLIP_HIGH = 4.0


def _reference_grams(window: int) -> int:
    """一个**满窗**（正好 ``window`` 个字符）里有多少个 gram。

    长度 L 的文本：1-gram 有 L 个、2-gram 有 L-1 个、3-gram 有 L-2 个，
    合计 ``3L - 3``。这是长度归一化的参考长度——必须是**真实值**，否则训练
    与推理会用不同的参考系，训出来的权重到推理时全是噪声。

    Args:
        window: 窗口长度（字符）。

    Returns:
        满窗的 gram 总数。
    """
    return max(1, 3 * window - 3)


@dataclass(slots=True)
class SensitiveVerdict:
    """一次判定的结果。

    Attributes:
        hit: 是否命中（分数 ≥ 阈值）。
        score: 最高窗口的命中概率（0～1）。
        position: 命中窗口在原文中的起始下标；-1 表示没定位到。
        excerpt: 命中窗口的原文片段，用于给用户看「大概哪里有问题」。
    """

    hit: bool
    score: float
    position: int
    excerpt: str


def _normalize(text: str) -> str:
    """第一视图：保留结构的原文视图。

    小写 + 空白折叠。保留空格是有意的——英文单词的边界靠它，
    ``bomb`` 和 ``b omb`` 必须是两个特征。

    Args:
        text: 原始文本。

    Returns:
        归一化后的文本。
    """
    return re.sub(r"\s+", " ", text.lower())


def _compact(text: str) -> str:
    """第二视图：去掉所有非字母数字字符的「压紧」视图。

    这一视图专门对付**插空混淆**：``杀 人`` / ``s-e-x`` / ``炸 药`` 里的空格和
    标点被抹掉之后，敏感词又变回连续的字。代价是英文的词边界也没了——所以它
    只作为**补充视图**，原始视图仍然在。

    Args:
        text: 原始文本。

    Returns:
        只保留字母、数字与汉字的紧凑文本。
    """
    return "".join(ch for ch in text.lower() if ch.isalnum())


def _ngram_hashes(text: str, seed: int) -> list[int]:
    """算出文本所有 n-gram 的哈希（按阶数依次排列）。

    用 ``zlib.crc32`` 而不是 Python 内置 ``hash()``：内置 hash 对 str 是**按进程
    随机加盐**的（PYTHONHASHSEED），同一个词在不同进程里哈希值不同——那样模型
    文件存下来就没意义了。crc32 是确定性的，且是 C 实现，快。

    Args:
        text: 已归一化的文本。
        seed: 哈希种子。两个视图用不同种子，避免同一段字在两个视图里撞到同一
            个槽位、权重互相污染。

    Returns:
        n-gram 哈希值列表。
    """
    tables = _ngram_hash_table(text, seed)
    return [int(v) for arr in tables for v in arr]


def _ngram_hash_table(
    text: str, seed: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """算出整段文本的 n-gram 哈希表（按阶数分成三段）。

    **一次算完整段，窗口之间再切片。** 窗口互相重叠（W 20 / 步长 4 → 每个 gram
    被算 5 遍），逐窗算等于把同样的哈希重复做 5 次——实测单视图打分 0.27ms 里
    有 0.22ms 花在这上面。改成整段算一次之后，判定耗时降到三分之一左右。

    Args:
        text: 已归一化的文本。
        seed: 哈希种子。

    Returns:
        ``(h1, h2, h3)``——分别是 1-gram / 2-gram / 3-gram 的哈希值数组，长度
        分别是 ``len(text)`` / ``len(text)-1`` / ``len(text)-2``（不足时为空）。
    """
    n = len(text)
    h1 = np.empty(n, dtype=np.int64)
    h2 = np.empty(max(0, n - 1), dtype=np.int64)
    h3 = np.empty(max(0, n - 2), dtype=np.int64)
    for i in range(n):
        h1[i] = zlib.crc32(text[i : i + 1].encode("utf-8"), seed)
    for i in range(max(0, n - 1)):
        h2[i] = zlib.crc32(text[i : i + 2].encode("utf-8"), seed)
    for i in range(max(0, n - 2)):
        h3[i] = zlib.crc32(text[i : i + 3].encode("utf-8"), seed)
    return h1, h2, h3


class SensitiveFilter:
    """敏感词分类器。

    构造完必须调 :meth:`load`，否则 :meth:`check` 会一直返回「没命中」——
    这在服务里是不允许的，所以启动流程里调 :func:`init_filter`，模型读不出来
    就直接让服务起不来（见 :mod:`app.main`）。
    """

    def __init__(
        self,
        path: str | Path,
        threshold: float | None = None,
        window: int | None = None,
        stride: int | None = None,
        max_chars: int = DEFAULT_MAX_CHARS,
        norm_alpha: float | None = None,
    ) -> None:
        """构造过滤器。

        Args:
            path: 模型文件路径（.npz）。
            threshold: 判定阈值，越高越严（误杀越少、漏检越多）。``None`` 或
                ``<= 0`` 表示**沿用模型自带的推荐阈值**——那个值是训练时按
                「误报率不超过目标」挑出来的，比手填一个数靠谱。
            window: 滑窗长度（字符）。``None`` 或 ``<= 0`` 表示用模型 meta 里
                记录的值——重训换了窗口，推理必须跟着换，否则训出来的权重在
                推理时全是噪声。
            stride: 滑窗步长（字符）。同 ``window``。
            max_chars: 单条文本最多扫描多少字符。
            norm_alpha: 窗口聚合的长度归一化指数。``None`` 表示用模型 meta 里
                记录的值，同 ``window`` 的理由。
        """
        self.path = Path(path)
        self.threshold = threshold
        self.window = window
        self.stride = stride
        self.max_chars = max_chars
        self.norm_alpha = norm_alpha
        self._weights: np.ndarray | None = None
        self._dim = DEFAULT_DIM
        self._bias: float = 0.0
        self._meta: dict = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------ 加载

    def load(self) -> None:
        """从磁盘加载模型。

        Raises:
            RuntimeError: 模型文件不存在或内容不合法。
        """
        if not self.path.exists():
            raise RuntimeError(
                f"敏感词过滤模型不存在：{self.path}。"
                "用 `python -m scripts.train_sensitive` 生成，"
                "或把 SENSITIVE_ENABLED 关掉（但那样就没有前置过滤了）"
            )
        with np.load(self.path, allow_pickle=False) as data:
            idx = data["w_idx"].astype(np.int64)
            val = data["w_val"].astype(np.float32)
            self._bias = float(data["bias"])
            dim = int(data["dim"])
            meta = json.loads(str(data["meta"]))
        weights = np.zeros(dim, dtype=np.float32)
        weights[idx] = val
        self._weights = weights
        self._dim = dim
        self._meta = meta
        # 阈值：配置没显式给就用模型自带的推荐值。
        if self.threshold is None or self.threshold <= 0:
            self.threshold = float(meta.get("threshold") or 0.5)
        # 窗口/步长同理：重训换了参数，推理必须跟着换。
        if self.window is None or self.window <= 0:
            self.window = int(meta.get("window") or DEFAULT_WINDOW)
        if self.stride is None or self.stride <= 0:
            self.stride = int(meta.get("stride") or DEFAULT_STRIDE)
        if self.norm_alpha is None:
            self.norm_alpha = float(meta.get("norm_alpha", DEFAULT_NORM_ALPHA))
        logger.info(
            "敏感词过滤模型已加载：%s（维度 %s，阈值 %.3f，窗口 %d/%d，"
            "归一化 %.2f，训练样本 %s）",
            self.path,
            dim,
            self.threshold,
            self.window,
            self.stride,
            self.norm_alpha,
            meta.get("n_samples", "?"),
        )

    @property
    def loaded(self) -> bool:
        """模型是否已经加载。"""
        return self._weights is not None

    @property
    def meta(self) -> dict:
        """模型附带的训练元信息（阈值建议、样本数、评估结果等）。"""
        return dict(self._meta)

    # ------------------------------------------------------------ 判定

    def check(self, text: str) -> SensitiveVerdict:
        """判定一段文本是否敏感。

        判定逻辑：在文本上滑窗，逐窗打分，取**最高**分。窗口级而不是整句级——
        一句话里提到敏感话题不等于在提敏感请求，整句打分会把这种情况一起误杀。

        Args:
            text: 待判定的文本（通常是用户提示词）。

        Returns:
            判定结果。未加载模型时一律返回「没命中」并记一条警告。
        """
        if self._weights is None:
            logger.warning("敏感词过滤模型未加载，本次判定直接放行")
            return SensitiveVerdict(hit=False, score=0.0, position=-1, excerpt="")
        if not text or not text.strip():
            return SensitiveVerdict(hit=False, score=0.0, position=-1, excerpt="")

        raw = _normalize(text[: self.max_chars])
        best_score, best_pos = self._score_view(raw, seed=0)
        compact_score, _ = self._score_view(_compact(text[: self.max_chars]), seed=1)
        score = max(best_score, compact_score)
        excerpt = raw[best_pos : best_pos + EXCERPT_CHARS].strip() if best_pos >= 0 else ""
        return SensitiveVerdict(
            hit=score >= self.threshold,
            score=round(float(score), 4),
            position=best_pos,
            excerpt=excerpt,
        )

    def check_cached(self, text: str) -> SensitiveVerdict:
        """带缓存的判定。

        同一个提示词被重复提交（前端重试、测试反复用同一句）时，第二次开始
        直接命中缓存，零计算。缓存是**按文本内容**的，键是文本本身。

        Args:
            text: 待判定的文本。

        Returns:
            判定结果。
        """
        return self._check_cached(text)

    @lru_cache(maxsize=1024)  # noqa: B019 - 实例级缓存，进程内复用
    def _check_cached(self, text: str) -> SensitiveVerdict:
        """缓存包装（真正的计算在 :meth:`check`）。"""
        return self.check(text)

    def find_sensitive_message(
        self, messages: list[dict]
    ) -> tuple[int, SensitiveVerdict] | None:
        """在一组聊天消息里找出第一个命中的**用户**消息。

        只看 ``role == "user"``：system 提示词是我们自己写的，tool 消息是工具
        的返回值——把它们也判一遍，等于让模型自己的输出把自己绊倒。

        这个方法服务于**第二道防线**：API 层已经拦过一遍了，这里再查一次是
        为了让「进入 LLM 的内容一定被过滤过」这条不变量对**所有路径**成立——
        历史消息、绕开 API 的内部调用、别的服务复用本模块，都逃不掉。

        Args:
            messages: OpenAI 协议的消息列表。

        Returns:
            ``(消息下标, 判定结果)``；没有命中时返回 ``None``。
        """
        for i, msg in enumerate(messages):
            if not isinstance(msg, dict) or msg.get("role") != "user":
                continue
            content = msg.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            verdict = self.check_cached(content)
            if verdict.hit:
                return i, verdict
        return None

    # ------------------------------------------------------------ 内部

    def _score_view(self, text: str, seed: int) -> tuple[float, int]:
        """给一个视图（原文或压紧版）做滑窗打分。

        Args:
            text: 已归一化的视图文本。
            seed: n-gram 哈希种子。

        Returns:
            (最高窗口概率, 该窗口在视图里的起始下标)。
        """
        weights = self._weights
        assert weights is not None  # noqa: S101 - check() 里已保证
        if not text:
            return 0.0, -1

        starts = list(range(0, max(1, len(text) - self.window + 1), self.stride))
        if not starts:
            starts = [0]
        # 末尾补一个窗口，保证最后几个字也被扫到
        last_end = min(len(text), starts[-1] + self.window)
        if starts[-1] + self.window < len(text):
            starts.append(max(0, len(text) - self.window))

        # 一次把整段文本的 gram 哈希算齐，窗口之间再切片（见 _ngram_hash_table）。
        h1, h2, h3 = _ngram_hash_table(text, seed)
        dim = self._dim
        h1 = h1 % dim
        h2 = h2 % dim
        h3 = h3 % dim

        gram_parts: list[np.ndarray] = []
        counts: list[int] = []
        for start in starts:
            end = min(len(text), start + self.window)
            if end - start < 1:
                counts.append(0)
                continue
            parts = [h1[start:end]]
            if end - start >= 2:
                parts.append(h2[start : end - 1])
            if end - start >= 3:
                parts.append(h3[start : end - 2])
            grams = np.concatenate(parts)
            gram_parts.append(grams)
            counts.append(len(grams))

        if not gram_parts:
            return 0.0, -1

        all_grams = np.concatenate(gram_parts)
        wid = np.repeat(np.arange(len(counts)), np.asarray(counts, dtype=np.int64))
        # 长度归一化：短窗的 gram 少，直接求和会系统性低分（漏检）。
        # 系数按「满窗 / 实际」开 alpha 次方，满窗恰好是 1。
        counts_arr = np.asarray(counts, dtype=np.float64)
        ref = _reference_grams(self.window)
        scale = np.clip(
            (ref / np.maximum(counts_arr, 1.0)) ** self.norm_alpha,
            NORM_CLIP_LOW,
            NORM_CLIP_HIGH,
        )
        contrib = weights[all_grams] * scale[wid]
        scores = np.bincount(
            wid,
            weights=contrib.astype(np.float64),
            minlength=len(counts),
        )
        logits = scores + self._bias
        probs = 1.0 / (1.0 + np.exp(-logits))
        best = int(np.argmax(probs))
        return float(probs[best]), starts[best] if starts else -1


_filter: SensitiveFilter | None = None


def get_filter() -> SensitiveFilter:
    """取全局过滤器单例（不保证已加载）。

    Returns:
        过滤器实例。
    """
    global _filter
    if _filter is None:
        from app.config.settings import settings

        _filter = SensitiveFilter(
            path=settings.sensitive_model_path,
            threshold=settings.sensitive_threshold,
            window=settings.sensitive_window,
            stride=settings.sensitive_stride,
            max_chars=settings.sensitive_max_chars,
        )
    return _filter


def screen_text(text: str) -> SensitiveVerdict | None:
    """按当前配置筛一段文本——给路由层与运行器用的统一入口。

    功能被显式关掉（``SENSITIVE_ENABLED=false``）时返回 ``None``，调用方据此
    跳过检查。这是**唯一**的「不过滤」路径，且必须显式配置，有记录、可审计。

    模型没加载时这里会补一次惰性加载。补不上就抛——模型缺失是部署事故，
    藏在一次请求里报 500 比在启动时炸掉更难查。

    Args:
        text: 待筛的文本。

    Returns:
        判定结果；功能关闭时返回 ``None``。

    Raises:
        RuntimeError: 模型文件缺失或损坏。
    """
    from app.config.settings import settings

    if not settings.sensitive_enabled:
        return None
    filt = get_filter()
    if not filt.loaded:
        filt.load()
    return filt.check_cached(text)


def screen_messages(messages: list[dict]) -> tuple[int, SensitiveVerdict] | None:
    """筛一组聊天消息——给 LLM 边界兜底用的统一入口。

    只看 ``role == "user"`` 的消息（system 是我们自己写的，tool 是模型自己的
    产出，把它们也判一遍等于让模型自己把自己绊倒）。

    Args:
        messages: OpenAI 协议的消息列表。

    Returns:
        ``(消息下标, 判定结果)``；功能关闭或没有命中时返回 ``None``。

    Raises:
        RuntimeError: 模型文件缺失或损坏。
    """
    from app.config.settings import settings

    if not settings.sensitive_enabled:
        return None
    filt = get_filter()
    if not filt.loaded:
        filt.load()
    return filt.find_sensitive_message(messages)


def init_filter() -> SensitiveFilter | None:
    """启动时初始化过滤器。

    **模型读不出来就让服务起不来**：静默放行等于把「安全前置」变成一句空话，
    而这类失败恰恰是最难在事后发现的（没有报错、没有告警，只是过滤没生效）。
    关掉整个功能请显式设 ``SENSITIVE_ENABLED=false``，那是有记录的、可审计的。

    Returns:
        已加载的过滤器；功能被关掉时返回 None。

    Raises:
        RuntimeError: 功能开启但模型加载失败。
    """
    from app.config.settings import settings

    if not settings.sensitive_enabled:
        logger.info("敏感词前置过滤已关闭（SENSITIVE_ENABLED=false）")
        return None
    filt = get_filter()
    with filt._lock:  # noqa: SLF001 - 同模块内的初始化锁
        if not filt.loaded:
            filt.load()
    logger.info(
        "敏感词前置过滤已启用：阈值 %.3f，窗口 %d/%d",
        filt.threshold,
        filt.window,
        filt.stride,
    )
    return filt
