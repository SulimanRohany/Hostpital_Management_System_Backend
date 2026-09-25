from .settings import *  # noqa: F403


# Tests must not depend on the developer's or deployment's PostgreSQL service.
DATABASES = {  # noqa: F405
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': ':memory:',
    }
}
