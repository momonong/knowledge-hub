"""Knowledge Hub phase 1 local single-user reading application."""

__all__ = ["create_app"]


def create_app(db_path, *, fetch=None, answer_provider=None, connection_provider=None):
    from .web import create_app as _create_app

    return _create_app(db_path, fetch=fetch, answer_provider=answer_provider,
                       connection_provider=connection_provider)
