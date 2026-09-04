#!/usr/bin/env python3
"""按 TRAJECTORY_SELECTION.json 下载每个 task 选中 trial 的产物。

产物落盘：
    data/trajectories/<task_name>/trajectory.json
    data/trajectories/<task_name>/model.patch
    data/trajectories/<task_name>/verifier/<file>     # 清单里列的全部 verifier_files
    data/trajectories/<task_name>/meta.json           # trial 元信息 + 各文件 bytes/sha256

特性：
  * base url 不硬编码，每次从 {SITE}/artifacts/{release}/release.json 现取
  * 失败重试 3 次，指数退避
  * 断点续传：文件已存在且非空则跳过（--force 强制重下）
  * 并发上限 8
  * 任何最终失败都显式汇总打印并以非零码退出，绝不静默当成功

用法：
    python download_trajectories.py
    python download_trajectories.py --force -j 8
    python download_trajectories.py --only abs-stepped-slices --only anko-default-function-arguments
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime
import hashlib
import json
import pathlib
import sys
import threading
import time
import urllib.error
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
MANIFEST = HERE / "TRAJECTORY_SELECTION.json"
OUTROOT = HERE / "data" / "trajectories"

SITE = "https://deepswe.datacurve.ai"
DEFAULT_RELEASE = "v1.1"
UA = {"User-Agent": "Mozilla/5.0 (deepswe-trace-fetch)"}

MAX_JOBS = 8
RETRIES = 3
BACKOFF_BASE = 2.0  # 1s, 2s, 4s

_print_lock = threading.Lock()


def log(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


def http_get(url: str, timeout: int = 300) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def http_get_retry(url: str, retries: int = RETRIES) -> bytes:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            return http_get(url)
        except Exception as exc:  # noqa: BLE001 - 网络异常种类多，统一重试
            last = exc
            if attempt + 1 < retries:
                time.sleep(BACKOFF_BASE**attempt)
    assert last is not None
    raise last


def load_release(release: str) -> dict:
    return json.loads(http_get_retry(f"{SITE}/artifacts/{release}/release.json"))


def artifact_url(rel: dict, kind: str, trial_name: str, file: str = "") -> str:
    key = rel["artifact_patterns"][kind]
    key = key.replace("{trial_name}", trial_name).replace("{file}", file)
    return f'{rel["artifact_base_url"].rstrip("/")}/{key}'


def safe_relpath(name: str) -> str:
    """verifier_files 里是相对路径（可能带 reports/ 前缀），拒绝逃逸出目标目录的写法。"""
    p = pathlib.PurePosixPath(name)
    if p.is_absolute() or any(part in ("..", "") for part in p.parts):
        raise ValueError(f"unsafe verifier file path: {name!r}")
    return str(p)


def sha256_of(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build_targets(rel: dict, sel: dict) -> list[tuple[str, str, pathlib.Path]]:
    """-> [(label, url, dest_path)]"""
    task_dir = OUTROOT / sel["task_name"]
    trial = sel["trial_name"]
    targets = [
        ("trajectory.json", artifact_url(rel, "trajectory", trial),
         task_dir / "trajectory.json"),
        ("model.patch", artifact_url(rel, "model_patch", trial),
         task_dir / "model.patch"),
    ]
    for f in sel.get("verifier_files") or []:
        relp = safe_relpath(f)
        targets.append(
            (f"verifier/{relp}",
             artifact_url(rel, "verifier_file", trial, relp),
             task_dir / "verifier" / relp)
        )
    return targets


def download_one(url: str, dest: pathlib.Path, force: bool,
                 known_empty: bool = False) -> tuple[bool, int]:
    """-> (downloaded?, bytes)。异常直接抛给调用方。

    断点续传：文件已存在且非空则跳过。0 字节文件本身无法区分"下载完成的空产物"和
    "写了一半的空壳"，所以只有上一轮 meta.json 明确记过 bytes==0 时才认为它已完成
    （known_empty），否则重新拉一次——代价只是一个空响应。

    上游确实存在 HTTP 200 + content-length: 0 的产物（大量 verifier/run.log 即是），
    那是真实产物内容，不是失败，照常落盘。
    """
    if not force and dest.is_file():
        size = dest.stat().st_size
        if size > 0 or known_empty:
            return False, size
    body = http_get_retry(url)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / (dest.name + ".part")
    tmp.write_bytes(body)
    tmp.replace(dest)
    return True, len(body)


def load_prev_meta(task_dir: pathlib.Path) -> dict:
    """上一轮的 meta.json（用于识别已完成的 0 字节产物）；读不到就当空。"""
    p = task_dir / "meta.json"
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text()).get("files") or {}
    except Exception:  # noqa: BLE001 - 坏掉的 meta 不该阻断下载
        return {}


def do_task(rel: dict, sel: dict, force: bool) -> dict:
    """下载一个 task 的全部产物，全部成功才写 meta.json。"""
    task = sel["task_name"]
    task_dir = OUTROOT / task
    prev_files = {} if force else load_prev_meta(task_dir)
    failures: list[dict] = []
    files_meta: dict[str, dict] = {}
    n_new = n_skip = n_bytes = 0

    for label, url, dest in build_targets(rel, sel):
        try:
            known_empty = prev_files.get(label, {}).get("bytes") == 0
            downloaded, size = download_one(url, dest, force, known_empty)
        except Exception as exc:  # noqa: BLE001
            detail = str(exc)
            if isinstance(exc, urllib.error.HTTPError):
                detail = f"HTTP {exc.code} {exc.reason}"
            failures.append({"task_name": task, "file": label, "url": url,
                             "error": f"{type(exc).__name__}: {detail}"})
            continue
        n_bytes += size
        if downloaded:
            n_new += 1
        else:
            n_skip += 1
        files_meta[label] = {"bytes": size, "sha256": sha256_of(dest)}

    if not failures:
        meta = {
            "task_name": task,
            "trial_name": sel["trial_name"],
            "model": sel["model"],
            "tier": sel["tier"],
            "config": sel.get("config"),
            "reasoning_effort": sel.get("reasoning_effort"),
            "n_agent_steps": sel.get("n_agent_steps"),
            "cost_usd": sel.get("cost_usd"),
            "reward": sel.get("reward"),
            "release_id": rel.get("release_id"),
            "downloaded_at": datetime.datetime.now(datetime.timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"),
            "n_files": len(files_meta),
            "total_bytes": n_bytes,
            "empty_files": sorted(k for k, v in files_meta.items() if v["bytes"] == 0),
            "files": dict(sorted(files_meta.items())),
        }
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")

    empties = [k for k, v in files_meta.items() if v["bytes"] == 0]
    status = "OK  " if not failures else "FAIL"
    log(f"  {status} {task:<50} new={n_new:<3} skip={n_skip:<3} "
        f"{n_bytes:>10,}B  empty={len(empties)}  fail={len(failures)}")
    return {"task_name": task, "failures": failures, "n_new": n_new,
            "n_skip": n_skip, "n_bytes": n_bytes, "n_files": len(files_meta),
            "empty": [f"{task}::{k}" for k in empties]}


def main() -> int:
    global OUTROOT  # noqa: PLW0603 - 允许 CLI 覆盖输出根目录
    ap = argparse.ArgumentParser()
    ap.add_argument("-m", "--manifest", default=str(MANIFEST))
    ap.add_argument("-o", "--outroot", default=str(OUTROOT))
    ap.add_argument("-r", "--release", default=DEFAULT_RELEASE)
    ap.add_argument("-j", "--jobs", type=int, default=MAX_JOBS,
                    help=f"并发任务数，上限 {MAX_JOBS}")
    ap.add_argument("--force", action="store_true", help="忽略已存在文件，全部重下")
    ap.add_argument("--only", action="append", default=[],
                    help="只处理指定 task_name，可重复")
    args = ap.parse_args()

    OUTROOT = pathlib.Path(args.outroot)

    jobs = max(1, min(args.jobs, MAX_JOBS))
    manifest = json.loads(pathlib.Path(args.manifest).read_text())
    selections = manifest["selections"]
    if args.only:
        wanted = set(args.only)
        selections = [s for s in selections if s["task_name"] in wanted]
        missing = wanted - {s["task_name"] for s in selections}
        if missing:
            print(f"ERROR: --only 指定的 task 不在清单里: {sorted(missing)}",
                  file=sys.stderr)
            return 2

    if manifest.get("unresolved"):
        print(f"NOTE: 清单含 {len(manifest['unresolved'])} 个 unresolved task "
              f"（无产物可下）: {manifest['unresolved']}")

    rel = load_release(args.release)
    print(f"release {rel['release_id']}  base={rel['artifact_base_url']}")
    expected_files = sum(2 + len(s.get("verifier_files") or []) for s in selections)
    print(f"tasks {len(selections)}  expected files {expected_files}  jobs {jobs}"
          f"{'  [force]' if args.force else ''}")

    t0 = time.time()
    results: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        futs = {pool.submit(do_task, rel, s, args.force): s for s in selections}
        for fut in concurrent.futures.as_completed(futs):
            sel = futs[fut]
            try:
                results.append(fut.result())
            except Exception as exc:  # noqa: BLE001 - 兜底，绝不让异常静默
                results.append({
                    "task_name": sel["task_name"],
                    "failures": [{"task_name": sel["task_name"], "file": "<task>",
                                  "url": "", "error": f"{type(exc).__name__}: {exc}"}],
                    "n_new": 0, "n_skip": 0, "n_bytes": 0, "n_files": 0,
                    "empty": [],
                })

    dt = time.time() - t0
    all_failures = [f for r in results for f in r["failures"]]
    total_files = sum(r["n_files"] for r in results)
    total_bytes = sum(r["n_bytes"] for r in results)
    total_new = sum(r["n_new"] for r in results)
    total_skip = sum(r["n_skip"] for r in results)
    all_empty = sorted(e for r in results for e in r.get("empty", []))

    print("-" * 78)
    print(f"tasks         : {len(results)}")
    print(f"files ok      : {total_files} / {expected_files} "
          f"(new={total_new} resumed={total_skip})")
    print(f"bytes         : {total_bytes:,}")
    print(f"elapsed       : {dt:.1f}s")
    print(f"failures      : {len(all_failures)}")
    # 上游真实存在 0 字节产物（HTTP 200 + content-length: 0），不算失败，但必须可见
    print(f"empty (0B) ok : {len(all_empty)}")
    for e in all_empty:
        print(f"  - {e}")

    if all_failures:
        print("-" * 78, file=sys.stderr)
        print("FAILED DOWNLOADS:", file=sys.stderr)
        for f in all_failures:
            print(f"  {f['task_name']} :: {f['file']}\n"
                  f"    url  : {f['url']}\n"
                  f"    error: {f['error']}", file=sys.stderr)
        bad = sorted({f["task_name"] for f in all_failures})
        print(f"incomplete tasks ({len(bad)}): {bad}", file=sys.stderr)
        return 1

    if total_files != expected_files:
        print(f"ERROR: 文件数 {total_files} != 预期 {expected_files}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
