#!/usr/bin/env python3
"""拉取 DeepSWE (deepswe.datacurve.ai) 某个 trial 的 agent trace 及相关产物。

链路：
  1. GET https://deepswe.datacurve.ai/artifacts/{release}/release.json
     -> 拿到 artifact_base_url + artifact_patterns（各产物的 key 模板）
  2. GET {artifact_base_url}/{pattern.format(trial_name=...)}
     -> 直接是原始文件，公开可读，无需 cookie / token

可选：GET /artifacts/{release}/trials.json 拿到全部 trial 的元数据索引
（29357 行，含 model / harness / reward / cost / has_trajectory 等），用于批量枚举。

用法:
    python fetch_trial_artifacts.py abs-module-cache-flags__4kU2tLe -o out/
    python fetch_trial_artifacts.py --index -o out/          # 只下索引
"""

import argparse
import json
import pathlib
import sys
import urllib.request

SITE = "https://deepswe.datacurve.ai"
DEFAULT_RELEASE = "v1.1"
UA = {"User-Agent": "Mozilla/5.0 (deepswe-trace-fetch)"}


def get(url: str) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=300) as resp:
        return resp.read()


def load_release(release: str) -> dict:
    return json.loads(get(f"{SITE}/artifacts/{release}/release.json"))


def load_index(release: str) -> dict:
    """全量 trial 元数据索引（约 47MB 未压缩）。"""
    return json.loads(get(f"{SITE}/artifacts/{release}/trials.json"))


def artifact_url(rel: dict, kind: str, trial_name: str, file: str = "") -> str:
    key = rel["artifact_patterns"][kind]
    key = key.replace("{trial_name}", trial_name).replace("{file}", file)
    return f'{rel["artifact_base_url"].rstrip("/")}/{key}'


# verifier_file 需要具体文件名，单独处理；其余是固定路径
SIMPLE_KINDS = ["trajectory", "model_patch", "agent_log", "verifier_output"]


def fetch_trial(rel: dict, trial_name: str, outdir: pathlib.Path,
                verifier_files=()) -> None:
    dest = outdir / trial_name
    dest.mkdir(parents=True, exist_ok=True)
    targets = [(k, artifact_url(rel, k, trial_name),
                dest / pathlib.PurePosixPath(rel["artifact_patterns"][k]).name)
               for k in SIMPLE_KINDS]
    for f in verifier_files:
        targets.append((f"verifier/{f}",
                        artifact_url(rel, "verifier_file", trial_name, f),
                        dest / "verifier" / f))
    for kind, url, path in targets:
        try:
            body = get(url)
        except Exception as exc:  # noqa: BLE001 - 缺产物是常态，跳过即可
            print(f"  {kind:<18} SKIP  {exc}", file=sys.stderr)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        print(f"  {kind:<18} {len(body):>9,}B  -> {path}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("trials", nargs="*", help="trial_name，可给多个")
    ap.add_argument("-o", "--outdir", default="deepswe_artifacts")
    ap.add_argument("-r", "--release", default=DEFAULT_RELEASE)
    ap.add_argument("--index", action="store_true", help="同时下载全量 trials.json 索引")
    args = ap.parse_args()

    outdir = pathlib.Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    rel = load_release(args.release)
    (outdir / "release.json").write_text(json.dumps(rel, indent=2))
    print(f"release {rel['release_id']} base={rel['artifact_base_url']}")

    index_rows = {}
    if args.index:
        idx = load_index(args.release)
        (outdir / "trials.json").write_text(json.dumps(idx))
        rows = idx["rows"] if isinstance(idx, dict) else idx
        index_rows = {r["trial_name"]: r for r in rows}
        print(f"index: {len(rows)} trials -> {outdir / 'trials.json'}")

    for name in args.trials:
        print(name)
        meta = index_rows.get(name, {})
        fetch_trial(rel, name, outdir, meta.get("verifier_files", []))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
