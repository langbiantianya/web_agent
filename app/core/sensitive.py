"""敏感词前置过滤（V3.4）。

**提示词进入 LLM 之前先过一遍**，命中就拦下，不让它到模型那里。

## 用的是现成的开源中文审核模型，不是自己训练的

模型来自 HuggingFace 的 ``jiangnanboy/content_audit``：基于
``thu-coai/roberta-base-cold`` 微调的四个中文单类别审核模型（政治敏感 / 暴力 /
色情 / 辱骂）。选型不是看模型卡，而是把候选全部拉下来、用**同一份中文语料**
实测过一轮——详见 docs/design.md §20.3。

模型文件不进仓库（四个 int8 模型合计约 411 MB，超过 GitHub 单文件 100 MB 的
上限），用 ``python -m scripts.fetch_sensitive_model`` 拉取并量化。

## 为什么是「滑窗取最大」而不是整句打分

一句话里提到敏感话题不等于在提敏感请求；反过来，「顺便问一下怎么做炸药」里
真正要命的是「怎么做炸药」那四个字，被整句的正常内容稀释之后分数就上不来。
所以打分在**窗口**上做，取最高分——窗口给出的是「哪一段有问题」，定位比整句准。

窗口同时也是资源上的关键：RoBERTa 的输入上限是 128 token，滑窗让每段证据
都能被模型**完整**地看一遍，而不是被截断在 128 字之外。

## 为什么快、为什么省

| 维度 | 做法 |
| --- | --- |
| 推理耗时 | 几个 int8 模型**并行**跑（ONNX Runtime 释放 GIL），几十字的提示词 20～50 ms；长文本按扫描字数线性增长（2000 字约 1.8 s） |
| 常驻内存 | 四个 int8 模型合计 413 MB（当前配置加载其中三个）——这是本方案最贵的地方，比字符 n-gram 方案重得多 |
| 外部依赖 | onnxruntime + tokenizers，**不加载任何深度学习框架**，不连任何外部服务 |
| 进程模型 | 纯 CPU 计算，不占 GPU |

**推理是同步的，绝不能直接堵在事件循环里**：本模块的异步入口一律用
``asyncio.to_thread`` 把同步计算挪到线程池。

## 为什么「不太严」

误杀一个正常请求比漏掉一条敏感词更伤——用户会直接不信任这个服务。所以：

1. 四个模型的阈值是**联合标定**出来的（整体误报率受控的前提下最大化召回），
   不是各自拍脑袋定的。各自标定再取并集会把误报率放大到 2.9%，联合标定是 0.6%。
2. 阈值只拦「明确命中」的请求；宁可漏也不误杀。
3. 只看**窗口级**分数，不看整句。

## 命中之后

默认动作是**拒绝**（API 层返回 422），不是遮蔽也不是悄悄放行。理由：过滤器是
「安全前置」，它的价值在于**确定性地**不把内容送出去；遮蔽会让模型拿到语义完整
的请求（只是词被替换了），那种「看起来处理过了其实没处理」的中间态最危险。

## 两道防线

1. **API 入口**：:func:`app.api.routes.conversation.send_message` 在**落库之前**
   就查一遍。命中直接 422 返回，用户消息不入库、不建 run、不消耗任何 token。
2. **LLM 边界**：:mod:`app.core.agent_runner` 在把消息交给模型之前再查一遍。

第二道不是多余的：第一道只能拦住「走 API 的新消息」，而历史消息、绕开 API 的
内部调用、别的服务复用本模块都逃得掉。有第二道，「进入 LLM 的内容一定被过滤过」
才是一条**对所有路径成立**的不变量，而不是一句约定。
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.sensitive_keywords import match_keywords

logger = logging.getLogger(__name__)

# 四个审核模型的登记表：本地文件名 → 类别名。
#
# 顺序是有意义的：它是「并行跑四个模型」时提交任务的顺序，也是分数并列时的
# 展示顺序。改动这里等于改判定顺序，要跟着改 manifest 的核对逻辑。
MODEL_KEYS: tuple[tuple[str, str], ...] = (
    ("politic", "政治敏感"),
    ("violence", "违法暴力"),
    ("porn", "色情"),
    ("insult", "仇恨歧视"),
)

# 标签极性：四个模型的输出标签顺序**不一致**。
#
# ``politic`` / ``violence`` / ``insult`` 的 logits[1] 是「命中该类别」的概率；
# ``porn`` 模型的 logits[1] 恰好是**反向**的（它是「不是色情」的置信度），
# 所以它的命中概率要取 ``1 - p``。
#
# 这个极性是**实测**出来的，不是照抄模型卡：用各模型自己的类别语料算 AUC，
# 取反之后 AUC 才 > 0.5（porn 模型取反前 0.16、取反后 0.84）。照抄文档会
# 把色情这一类彻底判反。
POLARITY: dict[str, int] = {"politic": 1, "violence": 1, "porn": -1, "insult": 1}

# 各模型的判定阈值（0～1）。
#
# 这些值是**联合标定**出来的（见 scripts/eval_sensitive.py 的 _calibrate），在三条
# 约束下最大化召回：
#
# 1. **并集**误报率不超过 1%；
# 2. **每个模型单独**的误报率不超过 0.5%；
# 3. **任何模型都不许在难负例上响**。
#
# 第 2 条不是多余的：只约束并集时，贪心标定会把预算花在**最吵的那个模型**身上
# ——并集看着达标，误报却全是它带来的。留一法实测就是这个情况：porn 模型贡献了
# 全部误报（它在自己那一类的 AUC 只有 0.844，在别的类别上却乱响，连「写个密码
# 哈希的实现」这种句子都会响）。
#
# 第 3 条是**规格**，不是拟合：难负例是专门设计出来抓误报的对抗样本（「帮我写一个
# 敏感词过滤器的实现思路」「写个密码哈希的实现」），不是随机抽样出来的统计样本。
# 一个模型在它们上面响，就是这个模型在这个阈值下不可信。
#
# ``insult`` 的阈值是 **1.01**——超过任何概率，等于「这个模型在当前预算下不参与
# 判定」。这不是随手填的：留一法实测显示拿掉 insult，联合召回一点不掉，而它要占
# 103 MB 内存和一份前向的时间。阈值 ≥ 1.0 的模型**根本不加载**（见
# :meth:`SensitiveFilter.load`），所以那 103 MB 不占；但它仍留在登记表里，
# 换一个更宽的阈值配置时它随时可以启用——仇恨歧视这一类的**模型覆盖只有它**。
#
# 阈值的实际含义是「这个模型对这一类别的告警置信度」。调高更严（漏得多），
# 调低更松（误杀多）。换模型或换语料后**必须重新标定**——
# ``python -m scripts.eval_sensitive`` 会把新阈值打印出来。
DEFAULT_THRESHOLDS: dict[str, float] = {
    "politic": 0.60,
    "violence": 0.74,
    "porn": 0.60,
    "insult": 1.01,
}

# 阈值 ≥ 这个值的模型视为**禁用**：不加载、不推理、不占内存。
DISABLED_THRESHOLD = 1.0

# 滑窗参数（字符）。
#
# 窗口 128 字，正好等于分词器的截断上限（128 token）——这样**每个窗口都能被模型
# 完整看一遍**，不会在窗口中间被截断。窗口太短，证据被切碎；太长，模型看不完。
#
# 步长取窗口的一半，保证重叠——漏掉半个窗口就是漏掉一条请求。
#
# 绝大多数提示词短于一个窗口，此时只有**一个**窗口，等价于整句打分；只有长文本
# 才会切多个窗口。长文本正是整句打分最容易出问题的地方：分词器截断到 128 token
# 之后的内容模型根本看不到，切窗才能把整段都盖到。
#
# 窗口长短的权衡是召回率与耗时的直接对冲：窗口越短，证据越集中、召回越高，
# 但窗口数变多、耗时线性上涨。实测（中文语料、验证集）见 docs/design.md §20.4。
DEFAULT_WINDOW = 128
DEFAULT_STRIDE = 64

# 一条文本最多切多少个窗口。
#
# 窗口数是耗时的**唯一线性因子**：超长提示词（粘贴了一整篇文档）如果按固定步长
# 切，会切出上百个窗口，四个模型各跑一遍就是几百次前向。超过这个上限时改为
# **拉大步长**把窗口摊开到整段文本上——分辨率下降，但整段仍然被盖到，不会漏掉
# 末尾的内容。
#
# 实测（三个模型并行，4 核 CPU）：
#
# | 窗口上限 | 21 字 | 44 字 | 180 字 | 2000 字 | 4000 字 |
# | --- | --- | --- | --- | --- | --- |
# | 24 | 20 ms | 53 ms | 489 ms | 5378 ms | 5723 ms |
# | 12 | 31 ms | 54 ms | 540 ms | 2958 ms | 2857 ms |
# | **8** | 19 ms | 50 ms | 468 ms | **1861 ms** | **1778 ms** |
# | 6 | 22 ms | 51 ms | 489 ms | 1421 ms | 1422 ms |
#
# 取 8：最坏情况 ~1.8 s，典型请求 20～50 ms。取 6 能再省 400 ms，但覆盖到的
# 内容少一大截——不划算。
DEFAULT_MAX_WINDOWS = 8

# 单条消息最多扫描多少字。超长的提示词（粘贴了一整篇文档）只扫前面这段——
# 敏感内容几乎总在开头，且这一步是为了**控住资源占用**。
#
# CPU 上的 int8 推理大约 **1.5 ms/token**（实测），所以扫描字数就是延迟的线性
# 因子：2000 字约 1.8 s，200 字约 0.5 s，几十字约 20～50 ms。
DEFAULT_MAX_CHARS = 2000

# 判定为命中后，错误信息里给出的片段最多留多少字。
EXCERPT_CHARS = 24

# 缓存条数上限。同一个提示词被重复提交（前端重试、测试反复用同一句）时，
# 第二次起直接命中缓存，零推理。
CACHE_SIZE = 1024


@dataclass(slots=True)
class SensitiveVerdict:
    """一次判定的结果。

    Attributes:
        hit: 是否命中（规则层命中，或任一模型越过它的阈值）。
        score: 判定分数。规则层命中时是 ``1.0``（精确短语命中就是确定）；
            模型层命中时是**触发命中的那个模型**的命中概率；两层都没命中时
            是所有模型中的最高概率（表示「最接近的一次」）。
        category: 命中的类别名（中文）；未命中时是空串。
        layer: 命中的来源——``"规则"`` 或 ``"模型"``；未命中时是空串。
            记下来是有用的：规则命中说明是已知黑产话术，模型命中说明是语义
            判断，两者的排查方向完全不同。
        position: 命中位置在原文中的起始下标；-1 表示没定位到。
        excerpt: 命中的原文片段，用于给用户看「大概哪里有问题」。
    """

    hit: bool
    score: float
    category: str
    layer: str
    position: int
    excerpt: str


class SensitiveFilter:
    """敏感词分类器（基于现成开源中文审核模型）。

    构造完必须调 :meth:`load`，否则 :meth:`check` 会一直返回「没命中」——
    这在服务里是不允许的，所以启动流程里调 :func:`init_filter`，模型读不出来
    就直接让服务起不来（见 :mod:`app.main`）。
    """

    def __init__(
        self,
        model_dir: str | Path,
        thresholds: dict[str, float] | None = None,
        window: int | None = None,
        stride: int | None = None,
        max_chars: int = DEFAULT_MAX_CHARS,
        max_windows: int = DEFAULT_MAX_WINDOWS,
        num_threads: int = 1,
        verify_checksum: bool = True,
    ) -> None:
        """构造过滤器。

        Args:
            model_dir: 模型目录（里面是四个 ``*.int8.onnx``、``tokenizer.json``
                和 ``manifest.json``）。由 ``scripts.fetch_sensitive_model`` 生成。
            thresholds: 各模型的阈值。``None`` 表示用 :data:`DEFAULT_THRESHOLDS`。
                键必须覆盖全部模型，缺键直接报错——阈值缺一个，那一类就是
                **静默放行**，比报错糟得多。
            window: 滑窗长度（字符）。``None`` 表示用 :data:`DEFAULT_WINDOW`。
            stride: 滑窗步长（字符）。``None`` 表示用 :data:`DEFAULT_STRIDE`。
            max_chars: 单条文本最多扫描多少字符。
            max_windows: 一条文本最多切多少个窗口。超过时拉大步长摊开，
                保证整段都被盖到。
            num_threads: 每个 ONNX 会话内部的线程数。四个模型已经并行跑了，
                这里再给每个会话多线程就是**超订**——默认 1，让并行度落在
                模型之间。
            verify_checksum: 是否按 manifest 里的 sha256 校验模型文件。
                模型是「安全前置」的一部分，被换掉了却没人知道，比启动慢
                一秒钟严重得多，所以默认校验。
        """
        self.model_dir = Path(model_dir)
        self.thresholds = dict(thresholds) if thresholds else dict(DEFAULT_THRESHOLDS)
        self.window = window or DEFAULT_WINDOW
        self.stride = stride or DEFAULT_STRIDE
        self.max_chars = max_chars
        self.max_windows = max(1, max_windows)
        self.num_threads = max(1, num_threads)
        self.verify_checksum = verify_checksum

        # 阈值 ≥ 1.0 的模型视为**禁用**：不加载、不推理、不占内存。
        # 留着它在登记表里，是为了「换个更宽的阈值配置它随时能启用」，而不是
        # 把它从产品能力里删掉——仇恨歧视这一类的模型覆盖只有它。
        #
        # 启用集合在 :meth:`load` 里按当时的阈值重算——构造之后再改
        # ``thresholds``，加载的模型集合也要跟着变，否则改阈值的人会拿到
        # 一个「阈值变了但模型没变」的怪状态。
        self._disabled: set[str] = set()
        self._enabled: tuple[tuple[str, str], ...] = MODEL_KEYS

        self._sessions: dict[str, Any] = {}
        self._tokenizer: Any = None
        self._manifest: dict = {}
        self._pool: ThreadPoolExecutor | None = None
        self._cache: OrderedDict[str, SensitiveVerdict] = OrderedDict()
        self._cache_lock = threading.Lock()
        self._load_lock = threading.Lock()

    # ------------------------------------------------------------ 加载

    def load(self) -> None:
        """从磁盘加载模型。

        加载四件事：manifest（来源与阈值校验）、分词器、四个 ONNX 会话、
        并行推理用的线程池。

        Raises:
            RuntimeError: 模型缺失、manifest 不合法或 sha256 对不上。
        """
        with self._load_lock:
            if self._sessions:
                return
            manifest_path = self.model_dir / "manifest.json"
            if not manifest_path.exists():
                raise RuntimeError(
                    f"敏感词过滤模型不存在：{self.model_dir}。"
                    "用 `python -m scripts.fetch_sensitive_model` 拉取，"
                    "或把 SENSITIVE_ENABLED 关掉（但那样就没有前置过滤了）"
                )
            self._manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

            missing = [k for k, _ in MODEL_KEYS if k not in self.thresholds]
            if missing:
                raise RuntimeError(f"敏感词过滤阈值缺失：{', '.join(missing)}")

            # 按**当前**阈值重算启用集合：构造之后改过阈值，这里才生效。
            self._disabled = {
                key
                for key, _ in MODEL_KEYS
                if self.thresholds.get(key, 1.0) >= DISABLED_THRESHOLD
            }
            self._enabled = tuple(
                (k, c) for k, c in MODEL_KEYS if k not in self._disabled
            )
            if not self._enabled:
                raise RuntimeError("敏感词过滤模型全部被禁用：没有任何模型参与判定")

            self._tokenizer = self._load_tokenizer()
            self._sessions = {
                key: self._load_session(key) for key, _ in self._enabled
            }
            self._pool = ThreadPoolExecutor(
                max_workers=max(1, len(self._enabled)), thread_name_prefix="sensitive"
            )
            logger.info(
                "敏感词过滤模型已加载：%s（%d 个 int8 模型%s，阈值 %s，窗口 %d/%d，"
                "最大扫描 %d 字）",
                self.model_dir,
                len(self._sessions),
                f"，已禁用 {', '.join(sorted(self._disabled))}" if self._disabled else "",
                {k: round(v, 3) for k, v in self.thresholds.items()},
                self.window,
                self.stride,
                self.max_chars,
            )

    def _load_tokenizer(self) -> Any:
        """加载分词器。

        Returns:
            ``tokenizers.Tokenizer`` 实例。

        Raises:
            RuntimeError: 分词器文件缺失。
        """
        from tokenizers import Tokenizer

        path = self.model_dir / "tokenizer.json"
        if not path.exists():
            raise RuntimeError(
                f"分词器不存在：{path}。用 `python -m scripts.fetch_sensitive_model` 重新拉取。"
            )
        return Tokenizer.from_file(str(path))

    def _load_session(self, key: str) -> Any:
        """加载一个模型的 ONNX 会话。

        Args:
            key: 模型键（``politic`` 等）。

        Returns:
            ``onnxruntime.InferenceSession``。

        Raises:
            RuntimeError: 模型文件缺失或 sha256 校验失败。
        """
        import onnxruntime as ort

        info = self._manifest.get("models", {}).get(key, {})
        filename = info.get("file", f"{key}.int8.onnx")
        path = self.model_dir / filename
        if not path.exists():
            raise RuntimeError(
                f"敏感词过滤模型文件不存在：{path}。"
                "用 `python -m scripts.fetch_sensitive_model` 拉取。"
            )
        if self.verify_checksum:
            self._verify_checksum(path, info.get("sha256", ""))

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = self.num_threads
        opts.inter_op_num_threads = 1
        # 图优化全开：量化后的算子融合能省掉不少内存搬运。
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        return ort.InferenceSession(
            str(path), opts, providers=["CPUExecutionProvider"]
        )

    def _verify_checksum(self, path: Path, expected: str) -> None:
        """按 manifest 里的 sha256 校验模型文件。

        Args:
            path: 模型文件路径。
            expected: 期望的 sha256。

        Raises:
            RuntimeError: 哈希对不上。
        """
        if not expected:
            return
        import hashlib

        h = hashlib.sha256()
        with path.open("rb") as fh:
            while chunk := fh.read(1 << 20):
                h.update(chunk)
        actual = h.hexdigest()
        if actual != expected:
            raise RuntimeError(
                f"敏感词过滤模型校验失败：{path.name} "
                f"（期望 {expected[:12]}…，实际 {actual[:12]}…）。"
                "文件可能下载不完整或被替换过，重新拉取。"
            )

    @property
    def loaded(self) -> bool:
        """模型是否已经加载。"""
        return bool(self._sessions)

    @property
    def meta(self) -> dict:
        """模型来源元信息（repo / revision / sha256 等），来自 manifest。"""
        return dict(self._manifest)

    # ------------------------------------------------------------ 判定

    def check(self, text: str) -> SensitiveVerdict:
        """判定一段文本是否敏感。

        **这是同步函数，会占住一个线程。** 事件循环里请用 :meth:`acheck`。

        判定走两层，两层是**或**的关系：

        1. **规则层**：静态短语表 + 目标型正则，负责模型没有类别覆盖的部分
           （隐私诈骗）以及模型明显偏弱的类别；
        2. **模型层**：四个现成中文审核模型，负责语义层面的判断。

        模型层在文本上滑窗，逐窗打分，取每个模型的**最高**分。窗口级而不是
        整句级——一句话里提到敏感话题不等于在提敏感请求，整句打分会把这种情况
        一起误杀。

        Args:
            text: 待判定的文本（通常是用户提示词）。

        Returns:
            判定结果。未加载模型时一律返回「没命中」并记一条警告。
        """
        if not text or not text.strip():
            return SensitiveVerdict(
                hit=False, score=0.0, category="", layer="", position=-1, excerpt=""
            )
        return self.judge_batch([text])[0]

    def judge_batch(self, texts: list[str]) -> list[SensitiveVerdict]:
        """批量判定（一批文本一次模型前向，比逐条判定快得多）。

        模型没加载时一律返回「没命中」——调用方据此放行。这是**唯一**的静默
        放行路径，启动流程里 :func:`init_filter` 保证模型读不出来就启动失败，
        所以正常运行时不会走到这里。

        Args:
            texts: 待判定的文本列表。

        Returns:
            每条文本一个判定结果，顺序与输入一致。
        """
        if not texts:
            return []
        if not self.loaded:
            logger.warning("敏感词过滤模型未加载，本次判定直接放行")
            return [
                SensitiveVerdict(
                    hit=False, score=0.0, category="", layer="", position=-1, excerpt=""
                )
                for _ in texts
            ]
        clipped = [t[: self.max_chars] for t in texts]
        rules = [match_keywords(t) for t in clipped]
        scored = self.score_batch(clipped)
        return [
            self._verdict(s, t, r)
            for s, t, r in zip(scored, clipped, rules, strict=True)
        ]

    async def acheck(self, text: str) -> SensitiveVerdict:
        """异步版 :meth:`check`。

        ONNX 推理是**同步**的，直接调会堵住事件循环——所有请求都得等它跑完。
        这里把它挪到线程池，事件循环继续跑别的协程。

        Args:
            text: 待判定的文本。

        Returns:
            判定结果。
        """
        return await asyncio.to_thread(self.check, text)

    def check_cached(self, text: str) -> SensitiveVerdict:
        """带缓存的判定。

        同一个提示词被重复提交（前端重试、测试反复用同一句）时，第二次开始
        直接命中缓存，零推理。缓存按**文本内容**做键。

        Args:
            text: 待判定的文本。

        Returns:
            判定结果。
        """
        with self._cache_lock:
            hit = self._cache.get(text)
            if hit is not None:
                self._cache.move_to_end(text)
                return hit
        verdict = self.check(text)
        with self._cache_lock:
            self._cache[text] = verdict
            self._cache.move_to_end(text)
            while len(self._cache) > CACHE_SIZE:
                self._cache.popitem(last=False)
        return verdict

    async def acheck_cached(self, text: str) -> SensitiveVerdict:
        """异步版 :meth:`check_cached`。

        Args:
            text: 待判定的文本。

        Returns:
            判定结果。
        """
        with self._cache_lock:
            hit = self._cache.get(text)
            if hit is not None:
                self._cache.move_to_end(text)
                return hit
        verdict = await self.acheck(text)
        with self._cache_lock:
            self._cache[text] = verdict
            self._cache.move_to_end(text)
            while len(self._cache) > CACHE_SIZE:
                self._cache.popitem(last=False)
        return verdict

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
        candidates: list[tuple[int, str]] = []
        for i, msg in enumerate(messages):
            if not isinstance(msg, dict) or msg.get("role") != "user":
                continue
            content = msg.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            candidates.append((i, content))
        if not candidates:
            return None

        # 一次批量打分：所有用户消息拼一个 batch，四个模型各跑一次，
        # 比逐条判定快得多（每条都是一次完整的模型前向）。
        verdicts = self.judge_batch([c for _, c in candidates])
        for (index, content), verdict in zip(candidates, verdicts, strict=True):
            if verdict.hit:
                return index, verdict
        return None

    async def afind_sensitive_message(
        self, messages: list[dict]
    ) -> tuple[int, SensitiveVerdict] | None:
        """异步版 :meth:`find_sensitive_message`。

        Args:
            messages: OpenAI 协议的消息列表。

        Returns:
            ``(消息下标, 判定结果)``；没有命中时返回 ``None``。
        """
        candidates: list[tuple[int, str]] = []
        for i, msg in enumerate(messages):
            if not isinstance(msg, dict) or msg.get("role") != "user":
                continue
            content = msg.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            candidates.append((i, content))
        if not candidates:
            return None
        verdicts = await asyncio.to_thread(
            self.judge_batch, [c for _, c in candidates]
        )
        for (index, content), verdict in zip(candidates, verdicts, strict=True):
            if verdict.hit:
                return index, verdict
        return None

    # ------------------------------------------------------------ 打分

    def score_batch(self, texts: list[str]) -> list[dict[str, tuple[float, int]]]:
        """给一批文本打分。

        每条文本先切成重叠滑窗，所有窗口拼成**一个 batch**，四个模型各跑一次
        前向，然后按模型取窗口最大值回到文本级。四个模型之间用线程池并行
        （ONNX Runtime 释放 GIL，是真并行）。

        Args:
            texts: 待打分的文本列表。

        Returns:
            每条文本一个 ``{模型键: (最高概率, 命中窗口起点)}`` 字典。
        """
        if not texts:
            return []

        import numpy as np

        clipped = [t[: self.max_chars] for t in texts]
        windows: list[str] = []
        owner: list[int] = []
        starts: list[int] = []
        for ti, text in enumerate(clipped):
            for win_text, start in self._make_windows(text):
                windows.append(win_text)
                owner.append(ti)
                starts.append(start)

        if not windows:
            return [{} for _ in texts]

        encodings = self._tokenizer.encode_batch(windows)
        width = min(128, max(len(e.ids) for e in encodings))
        ids = np.zeros((len(encodings), width), dtype=np.int64)
        mask = np.zeros((len(encodings), width), dtype=np.int64)
        for i, enc in enumerate(encodings):
            k = min(len(enc.ids), width)
            ids[i, :k] = enc.ids[:k]
            mask[i, :k] = 1

        owner_arr = np.asarray(owner)
        starts_arr = np.asarray(starts)

        def run_model(key: str) -> tuple[str, Any]:
            """跑一个模型，返回 (键, 每条文本的 (概率, 窗口起点))。"""
            session = self._sessions[key]
            feed: dict[str, Any] = {"input_ids": ids, "attention_mask": mask}
            names = {i.name for i in session.get_inputs()}
            if "token_type_ids" in names:
                feed["token_type_ids"] = np.zeros_like(ids)
            logits = session.run(None, feed)[0]
            if logits.ndim == 3:
                logits = logits[:, 0, :]
            raw = 1.0 / (1.0 + np.exp(-logits[:, 1]))
            prob = raw if POLARITY[key] > 0 else 1.0 - raw
            return key, prob

        results: dict[str, Any] = {}
        if self._pool is not None and self._enabled:
            futures = [self._pool.submit(run_model, key) for key, _ in self._enabled]
            for fut in futures:
                key, prob = fut.result()
                results[key] = prob
        else:  # pragma: no cover - 线程池只可能缺失于未加载状态
            for key, _ in self._enabled:
                results[key] = run_model(key)[1]

        out: list[dict[str, tuple[float, int]]] = []
        for ti in range(len(texts)):
            row: dict[str, tuple[float, int]] = {}
            sel = owner_arr == ti
            for key, _ in self._enabled:
                probs = results[key][sel]
                if probs.size == 0:
                    row[key] = (0.0, -1)
                    continue
                best = int(np.argmax(probs))
                row[key] = (float(probs[best]), int(starts_arr[sel][best]))
            out.append(row)
        return out

    def _make_windows(self, text: str) -> list[tuple[str, int]]:
        """把文本切成重叠滑窗。

        窗口数超过 :data:`DEFAULT_MAX_WINDOWS` 时，**拉大步长**把窗口摊开到整段
        文本上，而不是截断——截断会漏掉末尾的内容，而末尾同样可能有敏感请求。
        代价是长文本的分辨率下降：窗口之间的重叠变小甚至没有重叠。

        Args:
            text: 已截断的文本。

        Returns:
            ``[(窗口文本, 窗口起点)]``；短于一个窗口时就是整段。
        """
        if len(text) <= self.window:
            return [(text, 0)]
        stride = self.stride
        n_windows = 1 + (len(text) - self.window + stride - 1) // stride
        if n_windows > self.max_windows:
            # 摊开：让首尾都被盖到，窗口之间尽量均匀。
            stride = max(
                self.window // 2,
                (len(text) - self.window + self.max_windows - 1) // (self.max_windows - 1),
            )
        out: list[tuple[str, int]] = []
        i = 0
        while True:
            out.append((text[i : i + self.window], i))
            if i + self.window >= len(text):
                break
            i += stride
        return out

    def _verdict(
        self,
        scored: dict[str, tuple[float, int]],
        text: str,
        rule: tuple[str, int] | None = None,
    ) -> SensitiveVerdict:
        """把分数变成判定结果。

        规则层与模型层是**或**的关系：任一层命中就算命中。

        模型层的判定依据是**余量**（概率 − 阈值）而不是概率本身：四个模型的
        阈值不同，概率不可直接比较。余量最大的模型就是证据最强的那个。

        两层都命中时取**位置更靠前**的那个——用户看到的「大概哪里有问题」应该是
        最先出问题的地方。

        Args:
            scored: :meth:`score_batch` 的单条结果。
            text: 原始文本（取片段用）。
            rule: 规则层的命中结果（类别、位置），没命中时是 ``None``。

        Returns:
            判定结果。
        """
        best_key: str | None = None
        best_margin = -1.0
        best_prob = 0.0
        best_start = -1
        for key, _ in self._enabled:
            prob, start = scored.get(key, (0.0, -1))
            margin = prob - self.thresholds[key]
            if margin > best_margin:
                best_margin = margin
                best_prob = prob
                best_start = start
                best_key = key

        model_hit = best_key is not None and best_margin >= 0

        if rule is not None:
            rule_pos = rule[1]
            model_pos = best_start if model_hit else -1
            # 规则命中一定算数；模型也命中时，取位置更靠前的那个。
            if not model_hit or rule_pos <= model_pos:
                return SensitiveVerdict(
                    hit=True,
                    score=1.0,
                    category=rule[0],
                    layer="规则",
                    position=rule_pos,
                    excerpt=text[rule_pos : rule_pos + EXCERPT_CHARS].strip(),
                )

        if model_hit:
            return SensitiveVerdict(
                hit=True,
                score=round(float(best_prob), 4),
                category=dict(MODEL_KEYS)[best_key],
                layer="模型",
                position=best_start,
                excerpt=text[best_start : best_start + EXCERPT_CHARS].strip(),
            )

        # 都没越过阈值：取最高概率作为「最接近的一次」报出去，便于排查。
        return SensitiveVerdict(
            hit=False,
            score=round(float(best_prob), 4),
            category="",
            layer="",
            position=-1,
            excerpt="",
        )


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
            model_dir=settings.sensitive_model_dir,
            thresholds=settings.sensitive_thresholds_parsed(),
            window=settings.sensitive_window,
            stride=settings.sensitive_stride,
            max_chars=settings.sensitive_max_chars,
            max_windows=settings.sensitive_max_windows,
            num_threads=settings.sensitive_num_threads,
            verify_checksum=settings.sensitive_verify_checksum,
        )
    return _filter


async def screen_text(text: str) -> SensitiveVerdict | None:
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
        await asyncio.to_thread(filt.load)
    return await filt.acheck_cached(text)


async def screen_messages(messages: list[dict]) -> tuple[int, SensitiveVerdict] | None:
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
        await asyncio.to_thread(filt.load)
    return await filt.afind_sensitive_message(messages)


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
    filt.load()
    logger.info(
        "敏感词前置过滤已启用：%d 个模型，阈值 %s，窗口 %d/%d",
        len(MODEL_KEYS),
        {k: round(v, 3) for k, v in filt.thresholds.items()},
        filt.window,
        filt.stride,
    )
    return filt
