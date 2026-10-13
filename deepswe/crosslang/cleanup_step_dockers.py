#!/usr/bin/env python3
"""Remove only images labeled for DeepSWE per-step replay.

Pass the generated *_docker.csv to remove that batch, or --all for every locally labeled
per-step replay image. The original task images are never removal candidates.
"""

import argparse
import csv
import json
from pathlib import Path
import re
import subprocess
import sys

from prepare_step_dockers import REPLAY_LABEL, SOURCE_LABEL


TAG_RE = re.compile(r"-([0-9a-f]{12})-replay-step\d+$")


def repo_tags(image):
    return [tag for tag in (image.get("RepoTags") or []) if tag and tag != "<none>:<none>"]


def docker(*args):
    return subprocess.run(["docker", *args], capture_output=True, text=True)


def inspect_image(ref):
    p = docker("image", "inspect", ref)
    if p.returncode:
        raise RuntimeError(f"找不到镜像 {ref}: {p.stderr.strip()[:250]}")
    images = json.loads(p.stdout)
    if len(images) != 1:
        raise RuntimeError(f"镜像 inspect 返回异常条数: {ref}")
    return images[0]


def marked_image(image, tag=None):
    labels = (image.get("Config") or {}).get("Labels") or {}
    if labels.get(REPLAY_LABEL) != "true":
        return False
    source = labels.get(SOURCE_LABEL, "")
    if not re.fullmatch(r"[0-9a-f]{20}", source):
        return False
    if tag is not None:
        match = TAG_RE.search(tag)
        if not match or not source.startswith(match.group(1)):
            return False
        if tag not in repo_tags(image):
            return False
    return True


def manifest_candidates(path):
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or not {"image_tag", "image_id", "status"}.issubset(reader.fieldnames):
            raise ValueError("steps_docker.csv 缺少 image_tag、image_id 或 status 列")
        return list(dict.fromkeys((row["image_tag"].strip(), row["image_id"].strip())
                                  for row in reader if row.get("status") == "ready"))


def all_candidates():
    p = docker("image", "ls", "-a", "--no-trunc", "--filter",
               f"label={REPLAY_LABEL}=true", "--format", "{{.ID}}")
    if p.returncode:
        raise RuntimeError(f"列出 replay 镜像失败: {p.stderr.strip()[:300]}")
    candidates = []
    for image_id in dict.fromkeys(p.stdout.splitlines()):
        image = inspect_image(image_id)
        if not marked_image(image):
            continue
        tags = [tag for tag in repo_tags(image) if marked_image(image, tag)]
        if tags:
            candidates.extend((tag, image_id) for tag in tags)
        elif not repo_tags(image):
            # A previously replaced tag can leave a dangling labeled image.
            candidates.append((image_id, image_id))
    return list(dict.fromkeys(candidates))


def remove_candidates(candidates, dry_run=False):
    removed = failed = 0
    for ref, expected_id in candidates:
        try:
            image = inspect_image(ref)
            if image.get("Id") != expected_id:
                raise RuntimeError("镜像 ID 与清单不符；tag 可能已被重新指向")
            if not marked_image(image, None if ref == expected_id else ref):
                raise RuntimeError("缺少 per-step replay 标签，或 tag/来源哈希不符")
            if ref == expected_id and repo_tags(image):
                raise RuntimeError("不能按 ID 删除仍有 tag 的镜像")
            if dry_run:
                print(f"将删除 {ref} ({expected_id[:19]})")
                continue
            p = docker("image", "rm", ref)
            if p.returncode:
                raise RuntimeError(p.stderr.strip()[:300] or "docker image rm 失败")
            removed += 1
            print(f"已删除 {ref}")
        except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
            failed += 1
            print(f"跳过 {ref}: {exc}", file=sys.stderr)
    return removed, failed


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("steps_docker_csv", type=Path, nargs="?",
                    help="只删除此清单中的检查点镜像")
    ap.add_argument("--all", action="store_true", help="删除本机所有带 per-step replay 标签的镜像")
    ap.add_argument("--dry-run", action="store_true", help="只列出将删除的镜像")
    args = ap.parse_args()
    if (args.steps_docker_csv is None) == (not args.all):
        ap.error("指定 steps_docker.csv 或 --all，二者选其一")
    try:
        candidates = all_candidates() if args.all else manifest_candidates(args.steps_docker_csv)
        removed, failed = remove_candidates(candidates, args.dry_run)
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"清理失败: {exc}", file=sys.stderr)
        return 1
    print(f"候选 {len(candidates)}；{'可删除' if args.dry_run else '已删除'} {len(candidates)-failed if args.dry_run else removed}；失败 {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
