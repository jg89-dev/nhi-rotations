from os import listdir
from json import load, dump, loads, dumps
from keepercommander import api
from logging import disable, CRITICAL
import threading
from queue import Queue
from croniter import croniter
from datetime import datetime
from time import sleep

# ─────────────────────────────────────────────
#  SETUP
# ─────────────────────────────────────────────

CONFIG_FILE    = 'NHI_rotation.json'
COMMANDER_FILE = 'commander_config.json'
POLL_INTERVAL  = 60   # seconds between scheduler ticks
ROTATION_TIMEOUT_POLLS  = 20  # max polls before giving up on a rotation
ROTATION_POLL_INTERVAL  = 3   # seconds between rotation completion checks


def build_config_interactively():
    """
    Interactively builds NHI_rotation.json when it doesn't exist.
    Prompts the user for the NHI record UID and the rotation folder
    names/UIDs, then writes them to disk.
    """
    nhi_uid        = input('UID of NHI record: ')
    folders_input  = input('Comma-separated list of folder names or UIDs for rotation records: ')
    folder_list    = [f.strip() for f in folders_input.split(',') if f]

    with open(CONFIG_FILE, 'w') as f:
        dump({"nhi_record": nhi_uid, "rotation_folders": folder_list}, f, indent=2)

    print('  ✓ NHI_rotation.json created')


def login_to_commander():
    """
    Logs into Keeper Commander using the local config file.
    On first run, prompts for credentials and configures the device
    for persistent login with a 30-day timeout.
    Returns the authenticated params object.
    """
    print('\n┌─ Keeper Commander Login')
    from keepercommander.__main__ import get_params_from_config

    if COMMANDER_FILE not in listdir():
        # First-time setup: create config, prompt for login, register device
        print('│  No config found — starting first-time setup...')
        from keepercommander import cli

        with open(COMMANDER_FILE, 'w') as f:
            dump({}, f)

        session = get_params_from_config(COMMANDER_FILE)
        session.user = input('│  Commander email login: ')
        api.login(session)

        print('│  Configuring device...')
        for cmd in [
            'this-device register',
            'this-device persistent-login on',
            'this-device ip-auto-approve on',
            'this-device timeout 30d'
        ]:
            cli.do_command(session, cmd)

    else:
        # Subsequent runs: load existing config
        session = get_params_from_config(COMMANDER_FILE)
        api.login(session)

    api.sync_down(session)
    print('└─ Logged in ✓\n')
    return session


# ─────────────────────────────────────────────
#  RECORD PARSING
# ─────────────────────────────────────────────

def parse_config(session, config):
    """
    Reads NHI_rotation.json and resolves folder names/UIDs to a list
    of rotation record dicts (with schedule, profile, config, etc.).
    Returns (nhi_uid, rotation_records).
    """
    print('┌─ Parsing NHI_rotation.json')
    from keepercommander.subfolder import get_contained_record_uids

    # Validate required config fields
    nhi_uid = config.get('nhi_record')
    if not nhi_uid:
        print('│  ✗ NHI record UID missing — exiting.')
        return

    folder_names = config.get('rotation_folders')
    if not folder_names or not isinstance(folder_names, list):
        print('│  ✗ Rotation folders missing or invalid — exiting.')
        return

    # Resolve folder names → UIDs (supports both name and UID as input)
    name_to_uid  = {session.folder_cache[uid].name: uid for uid in session.folder_cache}
    resolved_uids = []

    for folder in folder_names:
        if folder in session.folder_cache:
            resolved_uids.append(folder)            # already a UID
        elif folder in name_to_uid:
            resolved_uids.append(name_to_uid[folder])  # resolved from name

    resolved_uids = list(set(resolved_uids))        # deduplicate

    # Collect all record UIDs within the resolved folders
    record_uids = []
    for folder_uid in resolved_uids:
        folder_tree = get_contained_record_uids(session, folder_uid, False)
        for folder in folder_tree:
            record_uids += list(folder_tree[folder])

    record_uids = list(set(record_uids))            # deduplicate

    # Enrich each record UID with its schedule and rotation metadata
    rotation_records = build_rotation_records(session, nhi_uid, record_uids)

    print(f'└─ {len(rotation_records)} record(s) found with a valid schedule\n')
    return nhi_uid, rotation_records


def build_rotation_records(session, nhi_uid, record_uids):
    """
    Filters records to only pamUser type with NHI rotation fields,
    and returns them enriched with schedule and rotation profile info.
    """
    rotation_records = []

    for uid in record_uids:
        record = api.get_record(session, uid)

        # Skip the NHI record itself
        if record.record_uid == nhi_uid:
            print(f'│  ⊘ Skipping NHI record ({uid})')
            continue

        # Only process pamUser records
        if record.record_type != 'pamUser':
            print(f'│  ⊘ Skipping {uid} — not pamUser type')
            continue

        # Extract NHI rotation custom fields
        nhi_field_names = ['rotation_config', 'rotation_resource', 'rotation_cron']
        nhi_fields = {
            x['name'].split(':')[-1]: x['value']
            for x in record.custom_fields
            if x['name'].split(':')[-1] in nhi_field_names
        }

        if not nhi_fields:
            print(f'│  ⊘ Skipping {uid} — no NHI rotation fields')
            continue

        schedule = parse_quartz_cron(nhi_fields.get('rotation_cron'))
        rotation_records.append({
            "uid":      uid,
            "profile":  'iam_user' if not nhi_fields.get('rotation_resource') else 'general',
            "config":   nhi_fields.get('rotation_config'),
            "link":     nhi_fields.get('rotation_resource') or nhi_fields.get('rotation_config'),
            "schedule": schedule,
        })
        print(f'│  ✓ {uid}  [{schedule}]')

    return rotation_records


# ─────────────────────────────────────────────
#  CRON UTILITIES
# ─────────────────────────────────────────────

def parse_quartz_cron(quartz_spec):
    """
    Converts a 6-field Quartz cron spec to a standard 5-field cron string
    compatible with croniter. Drops the leading seconds field and replaces
    Quartz-specific '?' wildcards with '*'.
    Returns the converted string, or None if conversion fails.
    """
    try:
        fields   = quartz_spec.split()
        standard = " ".join(fields[1:6])    # drop seconds (field 0)
        standard = standard.replace("?", "*")
        return standard
    except Exception:
        return None


def is_due(record):
    """
    Returns True if this record's cron schedule fired within the last tick window.
    Uses get_prev() to find the most recent scheduled fire time and compares
    it against now — the correct approach for a polling-based scheduler.
    """
    cron = croniter(record["schedule"], datetime.now())
    last_fire = cron.get_prev(datetime)
    return (datetime.now() - last_fire).total_seconds() < POLL_INTERVAL


# ─────────────────────────────────────────────
#  ROTATION
# ─────────────────────────────────────────────

def rotate(session, nhi_uid, record):
    """
    Performs a full rotation cycle for a single record:
      1. Syncs vault state
      2. Updates the NHI record's data to match the rotation record
      3. Reconfigures the rotation profile via CLI command
      4. Triggers the PAM rotation
      5. Polls until rotation completes (or times out)
      6. Syncs the rotated password back to the source record
    """
    uid = record['uid']
    print(f'\n┌─ Rotation triggered → {uid}')

    from keepercommander.commands.recordv3 import RecordEditCommand
    from keepercommander.cli import do_command
    from keepercommander.commands.discoveryrotation_v1 import PAMGatewayActionRotateCommand

    edit_cmd = RecordEditCommand()

    # ── Step 1: Sync vault and copy rotation record data onto the NHI record
    print('│  Syncing vault... ', end='', flush=True)
    api.sync_down(session)
    print('done')

    print('│  Updating NHI record... ', end='', flush=True)
    new_data          = loads(session.record_cache[uid]['data_unencrypted'].decode('utf-8'))
    new_data['title'] = api.get_record(session, nhi_uid).title
    edit_cmd.execute(session, record=nhi_uid, data=dumps(new_data))
    api.sync_down(session)
    print('done')

    # ── Step 2: Configure the rotation profile via CLI
    print('│  Configuring rotation profile... ', end='', flush=True)
    cli_cmd = f'pam rotation edit -r={nhi_uid} -f -c={record["config"]} -rp {record["profile"]} -od -e '
    if record['profile'] == 'general':
        cli_cmd += '-rs='
    elif record['profile'] == 'IAM':
        cli_cmd += '-iac='
    cli_cmd += record['link']
    do_command(session, cli_cmd)
    print('done')

    # ── Step 3: Trigger PAM rotation
    print('│  Triggering PAM rotation... ', end='', flush=True)
    PAMGatewayActionRotateCommand().execute(session, record_uid=nhi_uid)
    print('done')

    # ── Step 4: Poll until the NHI record data changes (rotation complete)
    print('│  Waiting for rotation to complete', end='', flush=True)
    rotation_done = False
    polls = 0
    while not rotation_done:
        sleep(ROTATION_POLL_INTERVAL)
        polls += 1
        api.sync_down(session)
        print('.', end='', flush=True)

        current_data = loads(session.record_cache[nhi_uid]['data_unencrypted'].decode('utf-8'))
        if current_data != new_data:
            rotation_done = True
            print(' done')

        if polls >= ROTATION_TIMEOUT_POLLS:
            print(' timed out ✗')
            break

    # ── Step 5: Sync rotated password back to the source record
    print(f'│  Syncing password back to {uid}... ', end='', flush=True)
    synced_data          = loads(session.record_cache[nhi_uid]['data_unencrypted'].decode('utf-8'))
    synced_data['title'] = api.get_record(session, uid).title
    edit_cmd.execute(session, record=uid, data=dumps(synced_data))
    api.sync_down(session)
    print('done')

    print(f'└─ Rotation complete ✓\n')


# ─────────────────────────────────────────────
#  THREADS
# ─────────────────────────────────────────────

def scheduler(job_queue, rotation_records):
    """
    Thread 1 — runs every POLL_INTERVAL seconds, checks each record's cron
    schedule, and enqueues any that are due. The worker thread handles the
    actual rotation so this loop is never blocked by long-running updates.
    """
    while True:
        due_records = [r for r in rotation_records if r.get('schedule') and is_due(r)]
        if due_records:
            print(f'[scheduler] {len(due_records)} record(s) due — adding to queue')
            for record in due_records:
                job_queue.put(record)
        sleep(POLL_INTERVAL)


def worker(session, job_queue, nhi_uid):
    """
    Thread 2 — blocks on the queue and processes rotation jobs one at a time.
    Exceptions are caught and logged so the worker loop keeps running.
    """
    while True:
        record = job_queue.get()
        try:
            rotate(session, nhi_uid, record)
        except Exception as e:
            print(f'[worker] ✗ Error rotating {record["uid"]}: {e}')
        finally:
            job_queue.task_done()


# ─────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────

def main():
    disable(CRITICAL)  # suppress verbose Keeper SDK logging

    # ── Bootstrap config if missing
    if CONFIG_FILE not in listdir():
        print('NHI_rotation.json not found — running setup:\n')
        build_config_interactively()

    # ── Load rotation config
    with open(CONFIG_FILE, 'r') as f:
        config = load(f)

    # ── Authenticate
    session = login_to_commander()

    # ── Resolve records and schedules
    nhi_uid, rotation_records = parse_config(session, config)

    # ── Start scheduler and worker threads
    print('┌─ Starting rotation scheduler')
    print(f'│  {len(rotation_records)} record(s) loaded')
    print('│  Press Ctrl+C to stop')
    print('└─────────────────────────────\n')

    job_queue = Queue()
    t_scheduler = threading.Thread(target=scheduler, args=(job_queue, rotation_records,), daemon=True)
    t_worker    = threading.Thread(target=worker,    args=(session, job_queue, nhi_uid,),  daemon=True)
    t_scheduler.start()
    t_worker.start()

    # ── Keep main thread alive (daemon threads die when main exits)
    try:
        while True:
            sleep(1)
    except KeyboardInterrupt:
        print('\n>> Shutting down gracefully')


main()
