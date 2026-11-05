"""studio 的指标导出（skill-service §12.1）。

## 指标清单（§12.1 的表，逐条对齐）

| 指标 | 类型 | 标签 |
| --- | --- | --- |
| ``studio_exec_total`` | counter | ``status`` / ``language`` / ``profile`` |
| ``studio_exec_duration_seconds`` | histogram | ``language`` |
| ``studio_exec_active`` | gauge | —— |
| ``studio_exec_queue_wait_seconds`` | histogram | —— |
| ``studio_exec_timeout_total`` | counter | ``language`` |
| ``studio_exec_output_truncated_total`` | counter | ``language`` |
| ``studio_exec_failed_total`` | counter | ``reason`` |
| ``studio_orphan_process_total`` | counter | —— |

## 为什么自己写，不引 prometheus_client

studio 的镜像**只装两个包**（grpcio + protobuf），这是有意的：容器里跑的是**模型
指定的代码**，镜像越小、依赖越少，被供应链投毒后能拿到的东西就越少
（``services/studio/requirements.txt`` 的开头就写着这条）。为了导指标再引一个
第三方包，等于把这条原则让出去一半。

Prometheus 的文本 exposition 格式本身就很简单——counter / gauge / histogram 三种
类型，一百来行就够。自己写换来的是「依赖还是那两个」，格式细节见
<https://prometheus.io/docs/instrumenting/exposition_formats/>。

## 为什么自己写 HTTP 服务

`/metrics` 端点只需要一个 GET、返回一段文本。``asyncio.start_server`` 加三十行
就够，犯不着为了一个端点引一个 Web 框架——那又是一份依赖、又是又是一批 CVE。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Prometheus 默认的耗时分桶（秒）。
DEFAULT_BUCKETS: tuple[float, ...] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
)

# exposition 格式的 Content-Type（Prometheus 0.0.4）。
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def _escape_label_value(value: str) -> str:
    """按 exposition 格式转义标签值。

    Args:
        value: 原始值。

    Returns:
        转义后的值（``\\`` → ``\\\\``、``"`` → ``\\"``、换行 → ``\\n``）。
    """
    return (
        value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    )


def _format_float(value: float) -> str:
    """把浮点数渲染成 exposition 格式。

    Args:
        value: 数值。

    Returns:
        文本（整数就渲染成整数，``+Inf`` 单独处理）。
    """
    if value == float("inf"):
        return "+Inf"
    if value == float("-inf"):
        return "-Inf"
    if value.is_integer() and abs(value) < 1e15:
        return str(int(value))
    return repr(value)


@dataclass
class _Sample:
    """一个指标族里的一组标签取值。"""

    labels: tuple[str, ...]
    value: float = 0.0
    # 直方图专用：各桶的累计计数、观测值之和、观测次数。
    buckets: list[int] = field(default_factory=list)
    total: float = 0.0
    count: int = 0


class MetricFamily:
    """一个指标族：同名样本的集合。

    Args:
        name: 指标名。
        help_text: 一句话说明（渲染成 ``# HELP``）。
        metric_type: ``counter`` / ``gauge`` / ``histogram``。
        label_names: 标签名（顺序固定）。
        buckets: 直方图的分桶边界（升序）。
    """

    def __init__(
        self,
        name: str,
        help_text: str,
        metric_type: str,
        label_names: tuple[str, ...] = (),
        buckets: tuple[float, ...] = DEFAULT_BUCKETS,
    ) -> None:
        """建一个空的指标族。

        Args:
            name: 指标名。
            help_text: 说明文字。
            metric_type: 指标类型。
            label_names: 标签名。
            buckets: 直方图分桶边界。
        """
        self.name = name
        self.help_text = help_text
        self.metric_type = metric_type
        self.label_names = tuple(label_names)
        self.buckets = tuple(buckets)
        self._samples: dict[tuple[str, ...], _Sample] = {}

    def _key(self, labels: dict[str, str]) -> tuple[str, ...]:
        """把标签字典转成可哈希的键。

        Args:
            labels: 标签字典。

        Returns:
            按 ``label_names`` 顺序排好的元组。
        """
        return tuple(str(labels.get(name, "")) for name in self.label_names)

    def _sample(self, labels: dict[str, str]) -> _Sample:
        """拿到（没有就建）某个标签组合的样本。

        Args:
            labels: 标签字典。

        Returns:
            样本对象。
        """
        key = self._key(labels)
        sample = self._samples.get(key)
        if sample is None:
            sample = _Sample(labels=key, buckets=[0] * len(self.buckets))
            self._samples[key] = sample
        return sample

    def _render_labels(self, labels: tuple[str, ...], extra: str = "") -> str:
        """渲染标签部分。

        Args:
            labels: 标签值元组。
            extra: 额外追加的标签片段（直方图的 ``le``）。

        Returns:
            花括号里的标签串；没有标签时是空串。
        """
        parts = [
            f'{name}="{_escape_label_value(value)}"'
            for name, value in zip(self.label_names, labels)
        ]
        if extra:
            parts.append(extra)
        if not parts:
            return ""
        return "{" + ",".join(parts) + "}"

    def render(self) -> list[str]:
        """渲染成 exposition 格式的行。

        Returns:
            文本行列表。
        """
        lines = [f"# HELP {self.name} {self.help_text}",
                 f"# TYPE {self.name} {self.metric_type}"]
        for sample in self._samples.values():
            if self.metric_type == "histogram":
                for edge, count in zip(self.buckets, sample.buckets):
                    # 桶边界用 repr：``1.0`` 就是 ``1.0``，不能被压成 ``1``。
                    label = self._render_labels(
                        sample.labels, f'le="{edge!r}"'
                    )
                    lines.append(f"{self.name}_bucket{label} {count}")
                label = self._render_labels(sample.labels, 'le="+Inf"')
                lines.append(f"{self.name}_bucket{label} {sample.count}")
                label = self._render_labels(sample.labels)
                lines.append(f"{self.name}_sum{label} {_format_float(sample.total)}")
                lines.append(f"{self.name}_count{label} {sample.count}")
            else:
                label = self._render_labels(sample.labels)
                lines.append(f"{self.name}{label} {_format_float(sample.value)}")
        return lines


class Counter(MetricFamily):
    """只增不减的计数器。"""

    def __init__(
        self, name: str, help_text: str, label_names: tuple[str, ...] = ()
    ) -> None:
        """建一个计数器。

        Args:
            name: 指标名。
            help_text: 说明文字。
            label_names: 标签名。
        """
        super().__init__(name, help_text, "counter", label_names)

    def inc(self, amount: float = 1, **labels: str) -> None:
        """加一。

        Args:
            amount: 增量。
            **labels: 标签取值。
        """
        self._sample(labels).value += amount


class Gauge(MetricFamily):
    """可增可减的瞬时值。"""

    def __init__(
        self, name: str, help_text: str, label_names: tuple[str, ...] = ()
    ) -> None:
        """建一个仪表。

        Args:
            name: 指标名。
            help_text: 说明文字。
            label_names: 标签名。
        """
        super().__init__(name, help_text, "gauge", label_names)

    def set(self, value: float, **labels: str) -> None:
        """设值。

        Args:
            value: 新值。
            **labels: 标签取值。
        """
        self._sample(labels).value = value

    def inc(self, amount: float = 1, **labels: str) -> None:
        """加。

        Args:
            amount: 增量。
            **labels: 标签取值。
        """
        self._sample(labels).value += amount

    def dec(self, amount: float = 1, **labels: str) -> None:
        """减。

        Args:
            amount: 减量。
            **labels: 标签取值。
        """
        self._sample(labels).value -= amount


class Histogram(MetricFamily):
    """分桶统计的分布。"""

    def __init__(
        self,
        name: str,
        help_text: str,
        label_names: tuple[str, ...] = (),
        buckets: tuple[float, ...] = DEFAULT_BUCKETS,
    ) -> None:
        """建一个直方图。

        Args:
            name: 指标名。
            help_text: 说明文字。
            label_names: 标签名。
            buckets: 分桶边界（升序）。
        """
        super().__init__(name, help_text, "histogram", label_names, buckets)

    def observe(self, value: float, **labels: str) -> None:
        """记一次观测。

        Args:
            value: 观测值。
            **labels: 标签取值。
        """
        sample = self._sample(labels)
        sample.count += 1
        sample.total += value
        for index, edge in enumerate(self.buckets):
            if value <= edge:
                sample.buckets[index] += 1


class MetricsRegistry:
    """指标注册表。"""

    def __init__(self) -> None:
        """建一个空注册表。"""
        self._families: dict[str, MetricFamily] = {}

    def counter(
        self, name: str, help_text: str, label_names: tuple[str, ...] = ()
    ) -> Counter:
        """登记一个计数器。

        Args:
            name: 指标名。
            help_text: 说明文字。
            label_names: 标签名。

        Returns:
            计数器对象（重复登记同名会返回同一个）。
        """
        return self._as(Counter, name, help_text, label_names)

    def gauge(
        self, name: str, help_text: str, label_names: tuple[str, ...] = ()
    ) -> Gauge:
        """登记一个仪表。

        Args:
            name: 指标名。
            help_text: 说明文字。
            label_names: 标签名。

        Returns:
            仪表对象。
        """
        return self._as(Gauge, name, help_text, label_names)

    def histogram(
        self,
        name: str,
        help_text: str,
        label_names: tuple[str, ...] = (),
        buckets: tuple[float, ...] = DEFAULT_BUCKETS,
    ) -> Histogram:
        """登记一个直方图。

        Args:
            name: 指标名。
            help_text: 说明文字。
            label_names: 标签名。
            buckets: 分桶边界。

        Returns:
            直方图对象。
        """
        existing = self._families.get(name)
        if existing is not None:
            return existing  # type: ignore[return-value]
        family = Histogram(name, help_text, label_names, buckets)
        self._families[name] = family
        return family

    def _as(
        self,
        kind: type[Counter] | type[Gauge],
        name: str,
        help_text: str,
        label_names: tuple[str, ...],
    ):
        """登记一个 counter / gauge。

        Args:
            kind: 类型。
            name: 指标名。
            help_text: 说明文字。
            label_names: 标签名。

        Returns:
            指标对象。
        """
        existing = self._families.get(name)
        if existing is not None:
            return existing
        family = kind(name, help_text, label_names)
        self._families[name] = family
        return family

    def render(self) -> str:
        """渲染整个注册表。

        Returns:
            Prometheus exposition 格式的文本。
        """
        lines: list[str] = []
        for family in self._families.values():
            lines.extend(family.render())
        return "\n".join(lines) + ("\n" if lines else "")


@dataclass(frozen=True, slots=True)
class StudioMetrics:
    """§12.1 那一整套指标。

    Attributes:
        exec_total: 执行次数（``status`` / ``language`` / ``profile``）。
        exec_duration: 端到端耗时（``language``）。
        exec_active: 当前在跑的脚本数。
        queue_wait: 排队时长。
        timeout_total: 超时次数（``language``）。
        output_truncated_total: 输出被截断次数（``language``）。
        failed_total: 启动失败的原因分布（``reason``）。
        orphan_process_total: 启动巡检发现的残留进程数。
        sandbox_start: 沙箱启动耗时（``profile`` / ``image``）。
        sandbox_total: 沙箱阶段计数（``phase`` / ``profile``）。
        sandbox_duration: 沙箱执行耗时（``image`` / ``status``）。
        registry: 底层的注册表（渲染用）。
    """

    exec_total: Counter
    exec_duration: Histogram
    exec_active: Gauge
    queue_wait: Histogram
    timeout_total: Counter
    output_truncated_total: Counter
    failed_total: Counter
    orphan_process_total: Counter
    sandbox_start: Histogram
    sandbox_total: Counter
    sandbox_duration: Histogram
    registry: MetricsRegistry


def build_metrics() -> StudioMetrics:
    """建一个**装好了 §12.1 全部指标**的度量集合。

    Returns:
        度量集合。
    """
    registry = MetricsRegistry()
    metrics = StudioMetrics(
        exec_total=registry.counter(
            "studio_exec_total",
            "执行次数",
            ("status", "language", "profile"),
        ),
        exec_duration=registry.histogram(
            "studio_exec_duration_seconds",
            "端到端耗时（秒）",
            ("language", "profile"),
        ),
        exec_active=registry.gauge("studio_exec_active", "当前在跑的脚本数"),
        queue_wait=registry.histogram(
            "studio_exec_queue_wait_seconds", "排队时长（秒）", ("profile",)
        ),
        timeout_total=registry.counter(
            "studio_exec_timeout_total", "超时次数", ("language",)
        ),
        output_truncated_total=registry.counter(
            "studio_exec_output_truncated_total",
            "输出被截断次数",
            ("language",),
        ),
        failed_total=registry.counter(
            "studio_exec_failed_total", "启动失败的原因分布", ("reason",)
        ),
        orphan_process_total=registry.counter(
            "studio_orphan_process_total", "启动巡检发现的残留进程数"
        ),
        sandbox_start=registry.histogram(
            "sandbox_start_seconds",
            "沙箱从创建到启动完成的耗时（秒）",
            ("profile", "image"),
        ),
        sandbox_total=registry.counter(
            "sandbox_total",
            "沙箱阶段计数",
            ("phase", "profile"),
        ),
        sandbox_duration=registry.histogram(
            "sandbox_duration_seconds",
            "沙箱执行耗时（秒）",
            ("image", "status"),
        ),
        registry=registry,
    )
    return metrics


async def _handle_metrics_request(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    registry: MetricsRegistry,
) -> None:
    """处理一次 HTTP 请求。

    Args:
        reader: 连接读端。
        writer: 连接写端。
        registry: 指标注册表。
    """
    try:
        request_line = await asyncio.wait_for(reader.readline(), timeout=5)
        # 把请求头读完——不读的话对端的连接会悬着。
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=5)
            if line in (b"\r\n", b"\n", b""):
                break
        parts = request_line.decode("latin-1").split()
        path = parts[1] if len(parts) >= 2 else "/"
        if path.split("?", 1)[0] == "/metrics":
            body = registry.render().encode("utf-8")
            status = "200 OK"
            content_type = CONTENT_TYPE
        else:
            body = b"not found\n"
            status = "404 Not Found"
            content_type = "text/plain; charset=utf-8"
        head = (
            f"HTTP/1.1 {status}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n"
            "\r\n"
        )
        writer.write(head.encode("latin-1") + body)
        await writer.drain()
    except (asyncio.TimeoutError, ConnectionError, OSError):
        logger.debug("指标请求处理失败", exc_info=True)
    finally:
        try:
            writer.close()
        except OSError:  # pragma: no cover - 连接已经断了
            pass


async def serve_metrics(
    registry: MetricsRegistry,
    host: str = "0.0.0.0",
    port: int = 9090,
) -> asyncio.AbstractServer:
    """起一个只提供 ``/metrics`` 的极简 HTTP 服务。

    Args:
        registry: 指标注册表。
        host: 监听地址。
        port: 监听端口。

    Returns:
        ``asyncio`` 服务器对象（调用方负责关掉它）。
    """

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """转调请求处理。"""
        await _handle_metrics_request(reader, writer, registry)

    server = await asyncio.start_server(handler, host, port)
    logger.info("指标端点已启动：http://%s:%s/metrics", host, port)
    return server
