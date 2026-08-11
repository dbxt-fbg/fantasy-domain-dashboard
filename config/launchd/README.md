# launchd jobs

Scheduled jobs that must survive the Mac being asleep. These live here for
version control; launchd only reads them from `~/Library/LaunchAgents/`, so the
copy in this directory is the source of truth and the installed copy is a
deployment of it.

## Why not cron

cron does not run jobs it missed while the machine was asleep or powered off.
The nightly backup was a cron job at 02:00 on a laptop that is routinely asleep
at 02:00, so it silently did not run on 2026-07-21 or 07-22. When a power loss
truncated `data/metrics.db` on 07-22, the newest clean backup was from 07-20 —
two days stale, and the corruption then went undetected for 7 days.

launchd runs a missed `StartCalendarInterval` job on the next wake or boot,
which is the behaviour an overnight backup needs. Anything else that must not
silently skip a day belongs here rather than in the crontab.

## Jobs

| Plist | Schedule | Runs |
| --- | --- | --- |
| `com.davidbaxter.fantasy-backup.plist` | daily 02:00 | `scripts/backup_db.py` — nightly SQLite backup, 30-day retention |

The corresponding crontab line is commented out (not deleted) with the reason
inline, so `crontab -l` still explains where the job went.

## Install / update

Paths inside the plists are absolute, including the home directory and the
pyenv interpreter — launchd does not expand `~` or read your shell profile, and
its environment is thinner than cron's. Edit the paths if the repo moves or the
pyenv version changes.

```bash
# install (or reinstall after editing)
cp config/launchd/com.davidbaxter.fantasy-backup.plist ~/Library/LaunchAgents/
launchctl unload ~/Library/LaunchAgents/com.davidbaxter.fantasy-backup.plist 2>/dev/null
launchctl load   ~/Library/LaunchAgents/com.davidbaxter.fantasy-backup.plist

# verify it is registered (second column is the last exit status)
launchctl list | grep fantasy-backup

# run it now, without waiting for 02:00
launchctl start com.davidbaxter.fantasy-backup
tail -5 logs/backup_db.log
```

A successful run appends `Backup OK (… KB, integrity_check passed)` to
`logs/backup_db.log`. `backup_db.py` verifies the copy with
`PRAGMA integrity_check` and deletes it rather than keeping a bad backup, so a
missing dated file in `data/backups/` means the source was unreadable — check
`data/metrics.db` with `PRAGMA integrity_check` before anything else.

After editing a plist, remember to reinstall it — editing the copy in this
directory alone changes nothing.
