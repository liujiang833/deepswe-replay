#!/usr/bin/env python3
"""把 data/ 里的 113 条轨迹装配成 replay 可直接消费的 trial 目录。

背景：data/ 下的布局是「下载态」——trajectories/<task_name>/ 放轨迹与补丁，
tasks/<task_name>.json 放整包 task 定义，语言与镜像信息又在 build_env.json 里。
replay.py / run_batch.py 要的是另一种布局：每条 trial 一个目录，里面四个文件齐全。
这个脚本做的就是这层转换。

三个不显然的点：

1. **n_commands 直接复用 replay.py 的 load_trace()**，不另写解析。
   自己数一遍的话口径极易和实际重放不一致（哨兵条目、无 tool_calls 的 step
   都要按同样规则处理），而这个数会进 meta.json 被 run_batch 打印出来当预期值。

2. **task.json 裁剪到只剩两个文件**：replay.py 只读 task.toml（取 docker_image /
   base_commit_hash / cpus / memory_mb / allow_internet），build_arm.sh 只读
   environment/Dockerfile。完整包 30.4MB → 裁剪后 0.4MB，而且顺带把参考解
   solution/solution.patch 挡在包外——它对重放毫无用处，不该进到服务器上。

3. **语言来自 build_env.json 的静态分析**，不靠猜 Dockerfile 里的包管理器。
   typescript 与 javascript 都是 node，`pnpm install` / `npm ci` 区分不可靠。

用法：
    python3 make_full_trials.py                        # 全部 113 条 → ./full_trials/
    python3 make_full_trials.py --only go,python,javascript
    python3 make_full_trials.py -o /tmp/t --only rust
"""

import argparse
import importlib.util
import json
import pathlib
import shutil
import sys

HERE = pathlib.Path(__file__).resolve().parent
DEEPSWE = HERE.parent
DATA = DEEPSWE / "data"

# task.json 里重放/构建真正会读到的文件，其余一律不带
KEEP_FILES = {"task.toml", "environment/Dockerfile"}
LANG_ORDER = ["python", "go", "rust", "typescript", "javascript"]


def load_replay_module():
    """从 replay.py 里借 load_trace()，保证命令计数与实际重放同一口径。"""
    spec = importlib.util.spec_from_file_location("_replay", DEEPSWE / "replay.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--outdir", default=str(HERE / "full_trials"))
    ap.add_argument("--only", default="",
                    help="只装配这些语言，逗号分隔（python,go,rust,typescript,javascript）")
    ap.add_argument("--baseline-from", default=str(HERE),
                    help="若该目录下同名 trial 有 replay/verdict.json，复制过来当基线")
    ap.add_argument("--no-verified", action="store_true",
                    help="不带上 crosslang/ 里那 5 条已验证 trial（默认带）")
    args = ap.parse_args()

    for p in (DATA / "build_env.json", DEEPSWE / "TRAJECTORY_SELECTION.json"):
        if not p.exists():
            print(f"❌ 缺少 {p}")
            return 1

    replay = load_replay_module()
    envs = json.loads((DATA / "build_env.json").read_text())["tasks"]
    sels = json.loads((DEEPSWE / "TRAJECTORY_SELECTION.json").read_text())["selections"]
    want = {s.strip() for s in args.only.split(",") if s.strip()}

    out = pathlib.Path(args.outdir)
    out.mkdir(parents=True, exist_ok=True)
    base_src = pathlib.Path(args.baseline_from)

    made, skipped, problems = [], [], []
    for sel in sels:
        task_name, trial_name = sel["task_name"], sel["trial_name"]
        env = envs.get(task_name)
        if env is None:
            problems.append(f"{task_name}: build_env.json 里没有")
            continue
        lang = env.get("language", "?")
        if want and lang not in want:
            skipped.append((lang, trial_name))
            continue

        src = DATA / "trajectories" / task_name
        task_json = DATA / "tasks" / f"{task_name}.json"
        missing = [str(p) for p in (src / "trajectory.json", src / "model.patch", task_json)
                   if not p.exists()]
        if missing:
            problems.append(f"{task_name}: 缺 {', '.join(missing)}")
            continue

        d = out / trial_name
        (d / "replay").mkdir(parents=True, exist_ok=True)
        shutil.copy2(src / "trajectory.json", d / "trajectory.json")
        shutil.copy2(src / "model.patch", d / "model.patch")

        # ── 裁剪 task.json ───────────────────────────────────────────
        full = json.loads(task_json.read_text())
        kept = [f for f in full["files"] if f["path"] in KEEP_FILES]
        got = {f["path"] for f in kept}
        if got != KEEP_FILES:
            problems.append(f"{task_name}: task.json 里缺 {sorted(KEEP_FILES - got)}")
            continue
        (d / "task.json").write_text(json.dumps(
            {"task_id": full.get("task_id", task_name), "n_files": len(kept), "files": kept},
            ensure_ascii=False, indent=1))

        # ── meta.json：run_batch.py 只读 language/task_id/image/n_commands/agent ──
        traj = json.loads((d / "trajectory.json").read_text())
        items = replay.load_trace(traj)
        environment = env.get("environment") or {}
        meta = {
            "language": lang,
            "trial_name": trial_name,
            "task_id": task_name,
            "repo": env.get("repository_url"),
            "agent": {"model_name": sel.get("model"), "config": sel.get("config")},
            "steps_total": sel.get("n_agent_steps"),
            "n_commands": len(items),
            "n_sentinel": sum(1 for it in items if it["sentinel"]),
            "model_patch_bytes": (src / "model.patch").stat().st_size,
            "image": {
                "docker_image": environment.get("docker_image"),
                "base_commit_hash": env.get("base_commit_hash"),
                "cpus": str(environment.get("cpus", 2)),
                "memory_mb": str(environment.get("memory_mb", 8192)),
                "allow_internet": str(environment.get("allow_internet", False)).lower(),
            },
            "reward": sel.get("reward"),
            "source": "make_full_trials.py（data/trajectories + data/tasks + build_env.json）",
        }
        (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1))

        # 已跑过的那几条带上基线，run_batch 会自动做跨机对比
        b = base_src / trial_name / "replay" / "verdict.json"
        if b.exists():
            shutil.copy2(b, d / "replay" / "verdict.json")

        made.append((lang, trial_name, len(items), meta["image"]["docker_image"]))

    # ── 并入已验证的对照组 ──────────────────────────────────────────
    # TRAJECTORY_SELECTION.json 选的是「每个 task 里 claude-fable-5 优先的那次 pass」，
    # 而 crosslang/ 下那 5 条是更早一轮按**跨模型**取样挑的，两批的 trial_name 不同、
    # 模型不同、model.patch 大小也不同 —— 也就是说 113 条里一条已验证基线都没有。
    #
    # 但同一个 task 用同一个镜像，所以把这 5 条一并带上**不增加任何构建成本**，
    # 却提供了一组「本地已跑通、patch 逐字节核对过」的回归对照：它们要是在服务器上
    # 也过，就说明这套流程本身是好的，别的条目失败就该往那个 task 自己身上找原因。
    n_ctrl = 0
    if not args.no_verified:
        need = ("meta.json", "trajectory.json", "model.patch", "task.json")
        for d0 in sorted(HERE.iterdir()):
            if not d0.is_dir() or not all((d0 / f).exists() for f in need):
                continue
            m0 = json.loads((d0 / "meta.json").read_text())
            if want and m0.get("language") not in want:
                continue
            if (out / d0.name).exists():
                continue
            d = out / d0.name
            (d / "replay").mkdir(parents=True, exist_ok=True)
            for f in ("meta.json", "trajectory.json", "model.patch"):
                shutil.copy2(d0 / f, d / f)
            full0 = json.loads((d0 / "task.json").read_text())
            kept0 = [f for f in full0["files"] if f["path"] in KEEP_FILES]
            (d / "task.json").write_text(json.dumps(
                {"task_id": full0.get("task_id"), "n_files": len(kept0), "files": kept0},
                ensure_ascii=False, indent=1))
            v0 = d0 / "replay" / "verdict.json"
            if v0.exists():
                shutil.copy2(v0, d / "replay" / "verdict.json")
            made.append((m0.get("language", "?"), d0.name, m0.get("n_commands") or 0,
                         (m0.get("image") or {}).get("docker_image")))
            n_ctrl += 1

    # ── 报告 ────────────────────────────────────────────────────────
    by_lang = {}
    for lang, _, n, _ in made:
        e = by_lang.setdefault(lang, [0, 0])
        e[0] += 1
        e[1] += n
    print("=" * 66)
    print(f" 装配完成  {len(made)} 条 → {out}")
    print("=" * 66)
    print(f"  {'语言':<12}{'条数':>6}{'命令总数':>10}")
    for lang in LANG_ORDER:
        if lang in by_lang:
            print(f"  {lang:<12}{by_lang[lang][0]:>6}{by_lang[lang][1]:>10}")
    print(f"  {'合计':<12}{len(made):>6}{sum(n for _, _, n, _ in made):>10}")
    n_base = sum(1 for _, t, _, _ in made if (out / t / "replay" / "verdict.json").exists())
    print(f"\n  其中已验证对照组   {n_ctrl} 条（带基线 verdict {n_base} 条）")
    if skipped:
        print(f"  --only 过滤掉      {len(skipped)} 条")
    if problems:
        print(f"\n❌ {len(problems)} 条有问题：")
        for p in problems[:10]:
            print("   ", p)
        return 1

    imgs = {i for _, _, _, i in made}
    print(f"\n  不同镜像  {len(imgs)} 个 —— 每个都要单独 docker build")
    print(f"\n下一步：")
    print(f"  bash make_bundle.sh --trials-dir {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
