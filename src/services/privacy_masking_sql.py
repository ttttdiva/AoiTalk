"""SQL counterpart of the server-issued masking source marker.

Only a JSON boolean true is authoritative; a string or a number never is.
Keep this expression portable across the application's PostgreSQL runtime
and isolated SQLite tests without coercing JSON scalars to SQL booleans.
"""
from sqlalchemy import Boolean
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.functions import FunctionElement


class privacy_masking_source_sql(FunctionElement):
    type = Boolean()
    inherit_cache = True


@compiles(privacy_masking_source_sql)
@compiles(privacy_masking_source_sql, "postgresql")
def _postgresql_marker(element, compiler, **kwargs):
    value = compiler.process(list(element.clauses)[0], **kwargs)
    return f"COALESCE(CAST(({value} -> 'privacy_masking' -> 'source') AS JSONB) = 'true'::JSONB, FALSE)"


@compiles(privacy_masking_source_sql, "sqlite")
def _sqlite_marker(element, compiler, **kwargs):
    value = compiler.process(list(element.clauses)[0], **kwargs)
    return f"COALESCE(json_type({value}, '$.privacy_masking.source') = 'true', 0)"
