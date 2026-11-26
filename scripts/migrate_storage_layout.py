import argparse
import hashlib
import os
import re
from pathlib import Path

BUCKET_PREFIX = {
    "recordings": "interview-recording",
    "verifications": "verification",
    "resumes": "resume",
    "backups": "backup",
    "employee_test_recordings": "test-recording",
    "monitor": "monitor",
    "video_upload_chunks": "video-upload-chunks",
    "docs/BR": "doc-br",
    "docs/JD": "doc-jd",
    "docs/Resumes": "doc-resume",
    "docs/Corp Pool": "doc-corp-pool",
    "docs/Portal Mapping": "doc-portal-mapping",
}
CLIENT_ROOT = "_clients"
DOCS_BUCKETS = {"BR": "docs/BR", "JD": "docs/JD", "Resumes": "docs/Resumes", "Corp Pool": "docs/Corp Pool", "Portal Mapping": "docs/Portal Mapping"}
UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
BACKUP_RE = re.compile(rf"^({UUID})-(?:(latest)|(\d{{8}}T\d+)-(submit|proctor))\.json$")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Plan:
    def __init__(self, apply: bool):
        self.apply = apply
        self.moved = self.deleted = self.conflicts = 0
        self.freed = 0

    def move(self, source: Path, target: Path) -> None:
        if target.exists():
            if digest(target) == digest(source):
                return self.delete(source, "duplicate of " + target.name)
            self.conflicts += 1
            print(f"CONFLICT  {source} -> {target.name} (different content, left in place)")
            return
        print(f"MOVE      {source.name} -> {target.name}")
        self.moved += 1
        if self.apply:
            os.replace(source, target)

    def delete(self, source: Path, reason: str) -> None:
        print(f"DELETE    {source.name} ({reason})")
        self.deleted += 1
        self.freed += source.stat().st_size
        if self.apply:
            source.unlink()


def files_in(folder: Path) -> list[Path]:
    return sorted(p for p in folder.iterdir() if p.is_file() and p.name != ".gitkeep") if folder.is_dir() else []


def migrate_backups(plan: Plan, folder: Path, root: Path, head: str) -> None:
    grouped: dict[str, dict] = {}
    for path in files_in(folder):
        match = BACKUP_RE.match(path.name)
        if not match:
            plan.move(path, root / f"{head}backup-{path.name}")
            continue
        entry = grouped.setdefault(match.group(1), {"latest": None, "submit": [], "proctor": []})
        if match.group(2):
            entry["latest"] = path
        else:
            entry[match.group(4)].append((match.group(3), path))
    for test_id, entry in grouped.items():
        submits = sorted(entry["submit"])
        proctors = sorted(entry["proctor"])
        latest = entry["latest"] or (max(submits + proctors)[1] if submits or proctors else None)
        keep = {id(latest)} if latest else set()
        if submits:
            keep.add(id(submits[-1][1]))
        if latest:
            plan.move(latest, root / f"{head}backup-{test_id}-latest.json")
        if submits and submits[-1][1] is not latest:
            plan.move(submits[-1][1], root / f"{head}backup-{test_id}-submit.json")
        for _, path in submits + proctors:
            if id(path) not in keep and path.exists():
                plan.delete(path, "superseded snapshot")


def migrate_client(plan: Plan, source: Path, root: Path, head: str, resume_hashes: set[str]) -> None:
    for bucket, base in BUCKET_PREFIX.items():
        if bucket.startswith("docs/") or bucket in {"monitor", "video_upload_chunks"}:
            continue
        folder = source / bucket
        if bucket == "backups":
            migrate_backups(plan, folder, root, head)
            continue
        for path in files_in(folder):
            plan.move(path, root / f"{head}{base}-{path.name}")
    legacy = source / "recordings" / "employee-tests"
    for path in files_in(legacy):
        plan.move(path, root / f"{head}{BUCKET_PREFIX['employee_test_recordings']}-{path.name}")
    for category, bucket in DOCS_BUCKETS.items():
        for path in files_in(source / "docs" / category):
            if category == "Resumes" and digest(path) in resume_hashes:
                plan.delete(path, "already stored with the candidate resumes")
            else:
                plan.move(path, root / f"{head}{BUCKET_PREFIX[bucket]}-{path.name}")
    history = source / "monitor" / "db-history.jsonl"
    if history.is_file() and not head:
        plan.move(history, root / "monitor-db-history.jsonl")


def prune_empty(root: Path) -> None:
    for folder in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        if folder.name != "logs" and not any(folder.iterdir()):
            folder.rmdir()


def resume_hashes(source: Path) -> set[str]:
    return {digest(p) for p in files_in(source / "resumes")}


def migrate_recordings(plan: Plan, root: Path) -> None:
    migrate_client(plan, root, root, "", resume_hashes(root))
    for client_dir in sorted(p for p in (root / CLIENT_ROOT).iterdir() if p.is_dir()) if (root / CLIENT_ROOT).is_dir() else []:
        migrate_client(plan, client_dir, root, f"{client_dir.name}-", resume_hashes(client_dir))
    if plan.apply:
        prune_empty(root)


def migrate_logs(plan: Plan, logs: Path) -> None:
    for environment in ("dev", "prod"):
        folder = logs / environment
        nested = folder / environment
        if environment == "dev":
            for path in nested.rglob("*") if nested.is_dir() else []:
                if path.is_file():
                    plan.delete(path, "nested dev log folder is not needed")
        else:
            for path in files_in(nested):
                plan.move(path, folder / path.name)
        for archive in files_in(folder):
            if re.fullmatch(r"\d{4}-\d{2}\.zip", archive.name):
                (folder / "archive").mkdir(exist_ok=True) if plan.apply else None
                plan.move(archive, folder / "archive" / archive.name)
    if plan.apply:
        prune_empty(logs)


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert nested recordings and log folders to the flat layout.")
    parser.add_argument("--recordings", type=Path, default=None, help="uploads root of one environment, e.g. /opt/recordings/prod")
    parser.add_argument("--logs", type=Path, default=None, help="logs root that contains dev/ and prod/")
    parser.add_argument("--apply", action="store_true", help="perform the changes; without it nothing is modified")
    args = parser.parse_args()
    plan = Plan(args.apply)
    if args.recordings and args.recordings.is_dir():
        migrate_recordings(plan, args.recordings)
    if args.logs and args.logs.is_dir():
        migrate_logs(plan, args.logs)
    mode = "applied" if args.apply else "dry run, nothing changed"
    print(f"\n{mode}: {plan.moved} moved, {plan.deleted} deleted ({plan.freed / 1048576:.2f} MB freed), {plan.conflicts} conflicts")


if __name__ == "__main__":
    main()
