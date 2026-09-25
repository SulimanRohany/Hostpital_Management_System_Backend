import os
import shutil
import sqlite3
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connections
from django.utils import timezone


class Command(BaseCommand):
    help = 'Create a dated, atomic backup of the default database.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--force', action='store_true',
            help='Replace an existing backup for today.',
        )

    def handle(self, *args, **options):
        backup_dir = Path(settings.DATABASE_BACKUP_DIR)
        if not backup_dir.is_absolute():
            backup_dir = Path(settings.BASE_DIR) / backup_dir
        backup_dir.mkdir(parents=True, exist_ok=True)

        connection = connections['default']
        engine = connection.settings_dict['ENGINE']
        today = timezone.localdate().isoformat()

        if engine == 'django.db.backends.sqlite3':
            destination = backup_dir / f'database-{today}.sqlite3'
            backup = self._backup_sqlite
        elif engine == 'django.db.backends.postgresql':
            destination = backup_dir / f'database-{today}.dump'
            backup = self._backup_postgresql
        else:
            raise CommandError(f'Backups are not supported for database engine {engine!r}.')

        if destination.exists() and not options['force']:
            self.stdout.write(self.style.WARNING(f'Backup already exists: {destination}'))
            return

        temporary = destination.with_name(f'.{destination.name}.tmp')
        try:
            temporary.unlink(missing_ok=True)
            backup(connection, temporary)
            os.replace(temporary, destination)
        except CommandError:
            temporary.unlink(missing_ok=True)
            raise
        except (OSError, sqlite3.Error, subprocess.SubprocessError) as exc:
            temporary.unlink(missing_ok=True)
            raise CommandError(f'Database backup failed: {exc}') from exc

        self._remove_expired_backups(backup_dir)
        self.stdout.write(self.style.SUCCESS(f'Database backup created: {destination}'))

    @staticmethod
    def _backup_sqlite(connection, destination):
        connection.ensure_connection()
        source = connection.connection
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
        finally:
            target.close()

    @staticmethod
    def _backup_postgresql(connection, destination):
        config = connection.settings_dict
        configured_executable = settings.PG_DUMP_PATH
        executable = shutil.which(configured_executable)
        if not executable and Path(configured_executable).is_file():
            executable = configured_executable
        if not executable:
            raise CommandError('The configured PostgreSQL backup tool could not be found.')

        command = [executable, '--format=custom', '--file', str(destination)]
        if config.get('HOST'):
            command.extend(['--host', str(config['HOST'])])
        if config.get('PORT'):
            command.extend(['--port', str(config['PORT'])])
        if config.get('USER'):
            command.extend(['--username', str(config['USER'])])
        command.append(str(config['NAME']))

        environment = os.environ.copy()
        if config.get('PASSWORD'):
            environment['PGPASSWORD'] = str(config['PASSWORD'])
        subprocess.run(command, env=environment, check=True, capture_output=True, text=True)

    def _remove_expired_backups(self, backup_dir):
        retention_days = settings.DATABASE_BACKUP_RETENTION_DAYS
        if retention_days <= 0:
            return
        cutoff = timezone.localdate() - timedelta(days=retention_days)
        for path in backup_dir.glob('database-*'):
            try:
                backup_date = datetime.strptime(
                    path.name.removeprefix('database-')[:10], '%Y-%m-%d'
                ).date()
            except ValueError:
                continue
            if backup_date < cutoff:
                path.unlink()
