# Daily database backups

The backup command creates consistent SQLite files named
`database-YYYY-MM-DD.sqlite3` in `backups/`.

Run a backup manually:

```powershell
.\venv\Scripts\python.exe manage.py backup_database
```

Install the Windows daily task (the default time is 2:00 AM):

```powershell
.\scripts\install_daily_backup_task.ps1
```

To choose another time, for example 11:30 PM:

```powershell
.\scripts\install_daily_backup_task.ps1 -At "23:30"
```

The task uses `StartWhenAvailable`, so Windows runs a missed backup after the
computer starts. The command is idempotent: a second run on the same date leaves
the existing backup untouched. Use `--force` to replace it.

Configuration is available in `.env`:

- `DATABASE_BACKUP_DIR`: where backups are written. For disaster recovery this
  should be a drive or synchronized folder separate from the database server.
- `DATABASE_BACKUP_RETENTION_DAYS`: deletes dated backups older than this many
  days after a successful backup. The default is 30; use `0` to keep all files.

Restore a backup by replacing the configured SQLite database file while the
application is stopped.
