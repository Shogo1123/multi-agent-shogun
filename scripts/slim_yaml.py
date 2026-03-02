#!/usr/bin/env python3
"""
YAML Slimming Utility

Removes completed/archived items from YAML queue files to maintain performance.
- For Karo: Archives done/cancelled commands from shogun_to_karo.yaml
- For all agents: Archives read: true messages from inbox files
"""

import sys
import yaml
import os
import time
from pathlib import Path
from datetime import datetime


CANONICAL_TASKS = {f'ashigaru{i}' for i in range(1, 9)} | {'gunshi'}
CANONICAL_REPORTS = {f'ashigaru{i}_report' for i in range(1, 9)} | {'gunshi_report'}
IDLE_STUB = {'task': {'status': 'idle'}}


def load_yaml(filepath):
    """Safely load YAML file."""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}
    except yaml.YAMLError as e:
        print(f"Error parsing {filepath}: {e}", file=sys.stderr)
        return {}


def save_yaml(filepath, data):
    """Safely save YAML file."""
    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            yaml.dump(data, f, allow_unicode=True, sort_keys=False, default_flow_style=False)
        return True
    except Exception as e:
        print(f"Error writing {filepath}: {e}", file=sys.stderr)
        return False


def get_timestamp():
    """Generate archive filename timestamp."""
    return datetime.now().strftime('%Y%m%d%H%M%S')


def slim_shogun_to_karo(dry_run=False):
    """Archive done/cancelled commands from shogun_to_karo.yaml."""
    queue_dir = Path(__file__).resolve().parent.parent / 'queue'
    archive_dir = queue_dir / 'archive'
    shogun_file = queue_dir / 'shogun_to_karo.yaml'

    if not shogun_file.exists():
        print(f"Warning: {shogun_file} not found", file=sys.stderr)
        return True

    data = load_yaml(shogun_file)
    # Support both 'commands' and 'queue' keys for backwards compatibility
    key = 'commands' if 'commands' in data else 'queue'
    if not data or key not in data:
        return True

    queue = data.get(key, [])
    if not isinstance(queue, list):
        print("Error: queue is not a list", file=sys.stderr)
        return False

    # Separate active and archived commands
    active = []
    archived = []

    for cmd in queue:
        status = cmd.get('status', 'unknown')
        if status in ['done', 'cancelled']:
            archived.append(cmd)
        else:
            active.append(cmd)

    # If nothing to archive, return success without writing
    if not archived:
        return True

    # Write archived commands to timestamped file
    archive_timestamp = get_timestamp()
    archive_file = archive_dir / f'shogun_to_karo_{archive_timestamp}.yaml'

    if dry_run:
        print(f"[DRY-RUN] would archive: {len(archived)} commands to {archive_file}")
        return True

    archive_data = {key: archived}
    if not save_yaml(archive_file, archive_data):
        return False

    # Update main file with active commands only
    data[key] = active
    if not save_yaml(shogun_file, data):
        print(f"Error: Failed to update {shogun_file}, but archive was created", file=sys.stderr)
        return False

    print(f"Archived {len(archived)} commands to {archive_file.name}", file=sys.stderr)
    return True


def slim_inbox(agent_id, dry_run=False):
    """Archive read: true messages from inbox file."""
    queue_dir = Path(__file__).resolve().parent.parent / 'queue'
    archive_dir = queue_dir / 'archive'
    inbox_file = queue_dir / 'inbox' / f'{agent_id}.yaml'

    if not inbox_file.exists():
        # Inbox doesn't exist yet - that's fine
        return True

    data = load_yaml(inbox_file)
    if not data or 'messages' not in data:
        return True

    messages = data.get('messages', [])
    if not isinstance(messages, list):
        print("Error: messages is not a list", file=sys.stderr)
        return False

    # Separate unread and archived messages
    unread = []
    archived = []

    for msg in messages:
        is_read = msg.get('read', False)
        if is_read:
            archived.append(msg)
        else:
            unread.append(msg)

    # If nothing to archive, return success without writing
    if not archived:
        return True

    # Write archived messages to timestamped file
    archive_timestamp = get_timestamp()
    archive_file = archive_dir / f'inbox_{agent_id}_{archive_timestamp}.yaml'

    archive_data = {'messages': archived}
    if dry_run:
        print(f"[DRY-RUN] would archive: {len(archived)} messages from {agent_id} to {archive_file.name}")
        return True

    if not save_yaml(archive_file, archive_data):
        return False

    # Update main file with unread messages only
    data['messages'] = unread
    if not save_yaml(inbox_file, data):
        print(f"Error: Failed to update {inbox_file}, but archive was created", file=sys.stderr)
        return False

    if archived:
        print(f"Archived {len(archived)} messages from {agent_id} to {archive_file.name}", file=sys.stderr)
    return True


def slim_tasks(dry_run=False):
    queue_dir = Path(__file__).resolve().parent.parent / 'queue'
    tasks_dir = queue_dir / 'tasks'
    archive_dir = queue_dir / 'archive' / 'tasks'

    if not tasks_dir.exists():
        return True

    for filepath in tasks_dir.glob('*.yaml'):
        data = load_yaml(filepath)
        if not isinstance(data, dict) or 'task' not in data or not isinstance(data.get('task'), dict):
            continue

        task_body = data['task']
        status = str(task_body.get('status', '')).lower()
        is_canonical = filepath.stem in CANONICAL_TASKS

        if is_canonical and status not in ['done', 'completed', 'cancelled']:
            continue
        if not is_canonical and status not in ['done', 'cancelled']:
            continue

        archive_timestamp = get_timestamp()
        archive_file = archive_dir / f'{filepath.stem}_{archive_timestamp}.yaml'

        if not is_canonical:
            archive_file = archive_dir / filepath.name
            if archive_file.exists():
                archive_file = archive_dir / f'{filepath.stem}_{archive_timestamp}.yaml'

        print(f"[DRY-RUN] would archive: {filepath} -> {archive_file}") if dry_run else None
        if dry_run:
            continue

        if not save_yaml(archive_file, data):
            return False

        if is_canonical:
            if not save_yaml(filepath, IDLE_STUB):
                return False
        else:
            try:
                filepath.unlink()
            except OSError as e:
                print(f"Error: failed to remove {filepath}: {e}", file=sys.stderr)
                return False

    return True


def is_active_command(parent_cmd, queue_file):
    data = load_yaml(queue_file)
    key = 'commands' if 'commands' in data else 'queue'
    queue = data.get(key, []) if isinstance(data, dict) else []

    for cmd in queue:
        if not isinstance(cmd, dict):
            continue
        if str(cmd.get('id')) == str(parent_cmd) and str(cmd.get('status')) != 'done':
            return True
    return False


def slim_reports(dry_run=False):
    queue_dir = Path(__file__).resolve().parent.parent / 'queue'
    reports_dir = queue_dir / 'reports'
    archive_dir = queue_dir / 'archive' / 'reports'
    command_file = queue_dir / 'shogun_to_karo.yaml'

    if not reports_dir.exists():
        return True

    for filepath in reports_dir.glob('*.yaml'):
        stem = filepath.stem
        if stem in CANONICAL_REPORTS:
            continue

        data = load_yaml(filepath)
        if not isinstance(data, dict):
            continue
        parent_cmd = data.get('parent_cmd')

        age_ok = time.time() - filepath.stat().st_mtime >= 86400
        if not age_ok:
            continue

        if parent_cmd and is_active_command(parent_cmd, command_file):
            continue

        archive_file = archive_dir / filepath.name
        print(f"[DRY-RUN] would archive: {filepath} -> {archive_file}") if dry_run else None
        if dry_run:
            continue

        if not save_yaml(archive_file, data):
            return False
        try:
            filepath.unlink()
        except OSError as e:
            print(f"Error: failed to remove {filepath}: {e}", file=sys.stderr)
            return False

    return True


def migration(dry_run=False):
    queue_dir = Path(__file__).resolve().parent.parent / 'queue'
    source_dir = queue_dir / 'reports' / 'archive'
    dest_dir = queue_dir / 'archive' / 'reports'

    if not source_dir.exists():
        return True

    files = [f for f in source_dir.glob('*.yaml') if f.is_file()]
    if not files:
        return True

    if dry_run:
        print(f"[DRY-RUN] would migrate: {len(files)} files")
        return True

    dest_dir.mkdir(parents=True, exist_ok=True)
    for filepath in files:
        try:
            filepath.rename(dest_dir / filepath.name)
        except OSError as e:
            print(f"Error: failed to migrate {filepath}: {e}", file=sys.stderr)
            return False

    try:
        source_dir.rmdir()
    except OSError:
        pass
    return True


def slim_all_inboxes(dry_run=False):
    queue_dir = Path(__file__).resolve().parent.parent / 'queue'
    inbox_dir = queue_dir / 'inbox'

    if not inbox_dir.exists():
        return True

    for filepath in inbox_dir.glob('*.yaml'):
        stem = filepath.stem
        if dry_run:
            print(f"[DRY-RUN] processing inbox: {filepath}")
        if not slim_inbox(stem, dry_run):
            return False
        if dry_run:
            print(f"[DRY-RUN] finished inbox: {filepath}")

    return True


def main():
    """Main entry point."""
    if len(sys.argv) < 2:
        print("Usage: slim_yaml.py <agent_id>", file=sys.stderr)
        sys.exit(1)

    dry_run = '--dry-run' in sys.argv
    agent_id = next((arg for arg in sys.argv[1:] if arg != '--dry-run'), None)
    if not agent_id:
        print("Usage: slim_yaml.py <agent_id>", file=sys.stderr)
        sys.exit(1)

    # Ensure archive directory exists
    archive_dir = Path(__file__).resolve().parent.parent / 'queue' / 'archive'
    archive_dir.mkdir(parents=True, exist_ok=True)
    (archive_dir / 'tasks').mkdir(parents=True, exist_ok=True)
    (archive_dir / 'reports').mkdir(parents=True, exist_ok=True)

    # Process shogun_to_karo if this is Karo
    if agent_id == 'karo':
        if not slim_shogun_to_karo(dry_run):
            sys.exit(1)
        if not migration(dry_run):
            sys.exit(1)
        if not slim_tasks(dry_run):
            sys.exit(1)
        if not slim_reports(dry_run):
            sys.exit(1)
        if not slim_all_inboxes(dry_run):
            sys.exit(1)

    # Process inbox for all agents
    if not slim_inbox(agent_id, dry_run):
        sys.exit(1)

    sys.exit(0)


if __name__ == '__main__':
    main()
