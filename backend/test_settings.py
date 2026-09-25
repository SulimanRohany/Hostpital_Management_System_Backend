from .settings import *  # noqa: F403


# Tests use an isolated in-memory database.
DATABASES = {  # noqa: F405
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': ':memory:',
    }
}
