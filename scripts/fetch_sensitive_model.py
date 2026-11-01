"""拉取并量化敏感词过滤用的**现成开源中文审核模型**（V3.4）。

模型文件**不进仓库**：四个 int8 模型合计约 411 MB，超过 GitHub 单文件 100 MB
的上限；而且模型是第三方产物，应该由上游维护、在部署时显式获取。

## 为什么要有这个脚本

过滤器是「安全前置」，模型缺失必须**启动失败**而不是静默放行
（见 docs/design.md §20）。把「获取模型」做成一个显式步骤，而不是让服务在启动时
偷偷联网下载，是为了让「模型从哪来、什么版本」这两件事在部署记录里清清楚楚——
模型换了行为，排查时至少知道是模型变了。

## 用的是哪几个模型

``jiangnanboy/content_audit``：基于 ``thu-coai/roberta-base-cold`` 微调的四个
中文单类别审核模型（政治敏感 / 暴力 / 色情 / 辱骂）。这是 HuggingFace 上**唯一**
成套的中文内容审核模型——实测下来，其余候选要么只覆盖英文，要么是英文多语言模型
不含中文（详见 docs/design.md §20.3）。

词表取自基座模型 ``thu-coai/roberta-base-cold`` 的 ``vocab.txt``。

## 版本固定

每个仓库都钉死一个 commit（revision）。上游改了文件也不会悄悄改变本项目的行为；
想升级就显式改这里的 revision，并在 manifest 里留下新的哈希。

用法：

```bash
python -m scripts.fetch_sensitive_model                    # 拉取 + 量化
python -m scripts.fetch_sensitive_model --keep-fp32        # 保留原始 fp32 文件
python -m scripts.fetch_sensitive_model --dir D:\\models\\sensitive
python -m scripts.fetch_sensitive_model --force            # 已存在也重新下载
```
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DIR = ROOT / "app" / "core" / "sensitive_models"

# 审核模型登记表：repo + revision（commit）+ 源文件 + 本地文件名 + 类别说明。
#
# 选型依据见 docs/design.md §20.3——所有候选都用同一份中文语料实测过，
# 按「误报率受控时的召回」和「推理耗时」选，不是看模型卡上的宣传。
MODELS: list[dict] = [
    {
        "key": "politic",
        "repo": "jiangnanboy/content_audit",
        "revision": "4a2c7b7baba2bb8100369d43d9d392bbc0bbbb63",
        "source": "roberta_wwm_politic_model.onnx",
        "category": "政治敏感",
    },
    {
        "key": "violence",
        "repo": "jiangnanboy/content_audit",
        "revision": "4a2c7b7baba2bb8100369d43d9d392bbc0bbbb63",
        "source": "roberta_wwm_violence_model.onnx",
        "category": "违法暴力",
    },
    {
        "key": "porn",
        "repo": "jiangnanboy/content_audit",
        "revision": "4a2c7b7baba2bb8100369d43d9d392bbc0bbbb63",
        "source": "roberta_wwm_porn_model.onnx",
        "category": "色情",
    },
    {
        "key": "insult",
        "repo": "jiangnanboy/content_audit",
        "revision": "4a2c7b7baba2bb8100369d43d9d392bbc0bbbb63",
        "source": "roberta_wwm_insult_model.onnx",
        "category": "仇恨歧视 / 辱骂",
    },
]

# 词表来源：基座模型。审核模型仓库里没有 tokenizer，词表得另外取。
TOKENIZER_SOURCE = {
    "repo": "thu-coai/roberta-base-cold",
    "revision": "614c097a485910d28beb75f60d90c61e3082b518",
    "source": "vocab.txt",
}

# 量化方式：动态 int8（QInt8）。权重按 int8 存、计算仍用 float。
#
# 选它是因为这是**唯一**能把体积压下来的手段：fp32 每个模型 409 MB，四个就是
# 1.64 GB；量化后 103 MB，合计 413 MB。
#
# **两个参数必须成对设置**，这是实测出来的，不是抄文档：
#
# | 量化参数 | 与 fp32 的平均相关 | 验证集召回 |
# | --- | --- | --- |
# | fp32（不量化） | 1.000 | 0.237 |
# | 默认（per_channel=False, reduce_range=False） | 0.702 | 0.145 |
# | **per_channel=True 单独** | **0.038** | **0.013** |
# | reduce_range=True 单独 | 0.426 | 0.105 |
# | **per_channel=True + reduce_range=True** | **0.971** | **0.303** |
#
# 也就是说：**per_channel=True 单独用是灾难**——分数与 fp32 几乎不相关，模型
# 等于废了；必须**搭配 reduce_range=True** 才是对的（相关 0.97，与 fp32 几乎
# 一致）。`reduce_range` 把激活值限制在 int8 的安全范围内，避免某些算子的
# 动态范围溢出。少设一个参数，四个模型一起废掉，而且症状很隐蔽——模型能加载、
# 能出分数，只是分数不再可信。
QUANTIZE_ARGS = {
    "weight_type": "QInt8",
    "per_channel": True,
    "reduce_range": True,
}


def _download(url: str, dest: Path) -> None:
    """下载一个文件（先写临时文件再改名，避免半截文件被当成完整模型）。"""
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "web-agent-fetch/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=180) as resp, tmp.open("wb") as fh:
            while chunk := resp.read(1 << 20):
                fh.write(chunk)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(dest)


def _sha256(path: Path) -> str:
    """算文件的 sha256（分块读，别把 400 MB 整个读进内存）。"""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _fetch_file(
    repo: str,
    revision: str,
    source: str,
    dest: Path,
    force: bool,
    local_src: Path | None = None,
) -> Path:
    """下载一个上游文件（已存在且没要求强制时跳过）。

    Args:
        repo: 仓库名。
        revision: 固定的 commit。
        source: 仓库内相对路径。
        dest: 本地落盘路径。
        force: 已存在也重新下载。
        local_src: 本地源目录。给了它就从本地拷贝，**不联网**——离线部署用。

    Returns:
        落盘路径。

    Raises:
        SystemExit: 下载失败，或本地源目录里缺文件。
    """
    if dest.exists() and not force:
        print(f"  已有 {dest.name}（{dest.stat().st_size / 1e6:.1f} MB），跳过下载")
        return dest
    if local_src is not None:
        src = local_src / source
        if not src.exists():
            raise SystemExit(f"本地源目录里没有 {source}：{src}")
        print(f"  从本地取 {source} …")
        shutil.copyfile(src, dest)
        print(f"    完成（{dest.stat().st_size / 1e6:.1f} MB）")
        return dest
    url = f"https://huggingface.co/{repo}/resolve/{revision}/{source}"
    print(f"  下载 {source} …")
    try:
        _download(url, dest)
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"下载失败 {url}: {exc}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"网络不可达 {url}: {exc}") from exc
    print(f"    完成（{dest.stat().st_size / 1e6:.1f} MB）")
    return dest


def _quantize(src: Path, dst: Path) -> None:
    """把 fp32 ONNX 动态量化成 int8。

    Args:
        src: fp32 模型路径。
        dst: int8 模型输出路径。

    Raises:
        SystemExit: 量化失败。
    """
    from onnxruntime.quantization import QuantType, quantize_dynamic

    print(f"  量化 {src.name} → {dst.name} …")
    tmp = dst.with_suffix(dst.suffix + ".part")
    try:
        quantize_dynamic(
            str(src),
            str(tmp),
            weight_type=QuantType.QInt8,
            per_channel=QUANTIZE_ARGS["per_channel"],
            reduce_range=QUANTIZE_ARGS["reduce_range"],
        )
    except Exception as exc:  # noqa: BLE001 - 量化器抛什么异常都不确定，统一转成可读错误
        tmp.unlink(missing_ok=True)
        raise SystemExit(f"量化失败 {src.name}: {exc}") from exc
    tmp.replace(dst)
    print(f"    完成（{dst.stat().st_size / 1e6:.1f} MB）")


def _build_tokenizer(vocab_path: Path, out_path: Path) -> None:
    """用词表拼一个 BERT WordPiece 分词器，落成 tokenizer.json。

    为什么不在运行时拼：``Tokenizer.from_file`` 只认 ``tokenizer.json``，不认
    ``vocab.txt``——想让运行时能直接加载一个文件，就在这里把分词器生成好。
    好处是分词规则（lowercase / handle_chinese_chars / [CLS] [SEP] 模板）跟着
    模型一起固化下来，不会因为手改常量而漂移。

    Args:
        vocab_path: 词表路径。
        out_path: tokenizer.json 输出路径。

    Raises:
        SystemExit: 分词器构建失败。
    """
    from tokenizers import Tokenizer, models, normalizers, pre_tokenizers, processors

    vocab: dict[str, int] = {}
    with vocab_path.open(encoding="utf-8") as fh:
        for i, line in enumerate(fh):
            vocab[line.rstrip("\n")] = i

    tok = Tokenizer(models.WordPiece(vocab, unk_token="[UNK]", max_input_chars_per_word=100))
    tok.normalizer = normalizers.BertNormalizer(
        lowercase=True, strip_accents=None, clean_text=True, handle_chinese_chars=True
    )
    tok.pre_tokenizer = pre_tokenizers.BertPreTokenizer()
    tok.post_processor = processors.TemplateProcessing(
        single="[CLS] $A [SEP]",
        pair="[CLS] $A [SEP] $B:1 [SEP]:1",
        special_tokens=[("[CLS]", tok.token_to_id("[CLS]")), ("[SEP]", tok.token_to_id("[SEP]"))],
    )
    tok.enable_truncation(max_length=128)
    out_path.write_text(tok.to_str(), encoding="utf-8")
    print(f"  分词器已生成：{out_path.name}（{out_path.stat().st_size / 1e3:.0f} KB）")


def fetch(
    out_dir: Path,
    force: bool = False,
    keep_fp32: bool = False,
    local_src: Path | None = None,
) -> Path:
    """拉取全部模型、量化、写 manifest。

    Args:
        out_dir: 落盘目录。
        force: 已存在也重新下载。
        keep_fp32: 量化后保留原始 fp32 文件（默认删掉，省 1.6 GB 磁盘）。
        local_src: 本地源目录。给了它就从本地取源文件，**完全不联网**。

    Returns:
        落盘目录。

    Raises:
        SystemExit: 下载或量化失败。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    work = out_dir / "_download"
    work.mkdir(exist_ok=True)

    manifest: dict = {
        "format": "sensitive-models/1",
        "models": {},
        "tokenizer": {},
    }

    for spec in MODELS:
        key = spec["key"]
        print(f"[{key}] {spec['category']}（{spec['repo']}）")
        src = _fetch_file(
            spec["repo"],
            spec["revision"],
            spec["source"],
            work / spec["source"],
            force,
            local_src,
        )
        dst = out_dir / f"{key}.int8.onnx"
        if dst.exists() and not force:
            print(f"  已有 {dst.name}（{dst.stat().st_size / 1e6:.1f} MB），跳过量化")
        else:
            _quantize(src, dst)
        if not keep_fp32:
            src.unlink(missing_ok=True)

        manifest["models"][key] = {
            "category": spec["category"],
            "repo": spec["repo"],
            "revision": spec["revision"],
            "source_file": spec["source"],
            "file": dst.name,
            "size": dst.stat().st_size,
            "sha256": _sha256(dst),
            "quantization": QUANTIZE_ARGS,
            "label_index": 1,
            "polarity": -1 if key == "porn" else 1,
        }

    print("[tokenizer] 词表 + 分词器")
    vocab_path = _fetch_file(
        TOKENIZER_SOURCE["repo"],
        TOKENIZER_SOURCE["revision"],
        TOKENIZER_SOURCE["source"],
        work / TOKENIZER_SOURCE["source"],
        force,
        local_src,
    )
    tok_path = out_dir / "tokenizer.json"
    _build_tokenizer(vocab_path, tok_path)
    manifest["tokenizer"] = {
        "repo": TOKENIZER_SOURCE["repo"],
        "revision": TOKENIZER_SOURCE["revision"],
        "source_file": TOKENIZER_SOURCE["source"],
        "file": tok_path.name,
        "size": tok_path.stat().st_size,
        "sha256": _sha256(tok_path),
        "vocab_size": sum(1 for _ in vocab_path.open(encoding="utf-8")),
        "lowercase": True,
        "handle_chinese_chars": True,
        "max_length": 128,
    }

    if not keep_fp32:
        shutil.rmtree(work, ignore_errors=True)

    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    total = sum(m["size"] for m in manifest["models"].values())
    print(f"\n完成：{out_dir}")
    print(f"  四个 int8 模型合计 {total / 1e6:.0f} MB，manifest：{manifest_path.name}")
    return out_dir


def main(argv: list[str] | None = None) -> int:
    """命令行入口。

    Args:
        argv: 命令行参数（默认取 ``sys.argv[1:]``）。

    Returns:
        进程退出码。
    """
    ap = argparse.ArgumentParser(description="拉取并量化敏感词过滤用的中文审核模型")
    ap.add_argument("--dir", type=Path, default=DEFAULT_DIR, help="落盘目录")
    ap.add_argument("--force", action="store_true", help="已存在也重新下载")
    ap.add_argument("--keep-fp32", action="store_true", help="量化后保留原始 fp32 文件")
    ap.add_argument(
        "--local-src",
        type=Path,
        default=None,
        help="从本地目录取源文件（离线部署，完全不联网）。目录里要按上游的"
        "原始文件名放好各 .onnx 与 vocab.txt。",
    )
    args = ap.parse_args(argv)
    try:
        fetch(
            args.dir,
            force=args.force,
            keep_fp32=args.keep_fp32,
            local_src=args.local_src,
        )
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
