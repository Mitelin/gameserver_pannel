import argparse
import os
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect current layered backup retention for a backup directory.")
    parser.add_argument("--slug", required=True, help="Server slug used in backup filenames.")
    parser.add_argument("--backup-dir", required=True, help="Directory containing backup archives.")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

    import django

    django.setup()

    from apps.servers.backup_engine import list_backups

    server = SimpleNamespace(slug=args.slug, backup_directory=args.backup_dir)
    backups = list_backups(server)
    kept = [item for item in backups if item["protected_by_rotation"]]
    deleted = [item for item in backups if not item["protected_by_rotation"]]
    bucket_counts = Counter(item["retention_bucket"] for item in kept)

    print(f"slug={args.slug}")
    print(f"backup_dir={args.backup_dir}")
    print(f"total={len(backups)} keep={len(kept)} delete={len(deleted)}")
    print(
        "buckets="
        f"user:{bucket_counts.get('user', 0)} "
        f"hourly:{bucket_counts.get('hourly', 0)} "
        f"daily:{bucket_counts.get('daily', 0)} "
        f"weekly:{bucket_counts.get('weekly', 0)} "
        f"monthly:{bucket_counts.get('monthly', 0)} "
        f"legacy:{bucket_counts.get('legacy', 0)}"
    )

    print("KEEP")
    for item in kept:
        print(f"  {item['created_at']}  {item['name']}  [{item['retention_bucket']}]")

    print("DELETE")
    for item in deleted:
        print(f"  {item['created_at']}  {item['name']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())