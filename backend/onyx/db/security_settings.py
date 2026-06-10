"""Persistence for the singleton ``security_settings`` row.

One row per tenant schema (boolean PK pinned to ``true``). Every column is
an *override*: ``NULL`` == "fall back to env default".
"""

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from onyx.db.models import SecuritySettings as SecuritySettingsRow
from onyx.server.security.models import SecuritySettingsOverrides
from onyx.server.security.models import SSRFProtectionLevel
from onyx.utils.logger import setup_logger

logger = setup_logger()


def _coerce_ssrf_level(value: str | None) -> str | None:
    """``ssrf_protection_level`` is a free-form string column. Coerce an
    unrecognized value (hand-edited / pre-enum data) to ``None`` so it falls
    back to the env default instead of failing validation and dropping *every*
    override on the row."""
    if value is None:
        return None
    try:
        return SSRFProtectionLevel(value).value
    except ValueError:
        logger.warning(
            "Ignoring invalid ssrf_protection_level %r in security_settings; "
            "falling back to env default.",
            value,
        )
        return None


def load_overrides(db_session: Session) -> SecuritySettingsOverrides:
    """Returns an empty overrides object (all-None) when no row exists."""
    row = db_session.execute(select(SecuritySettingsRow)).scalar_one_or_none()
    if row is None:
        return SecuritySettingsOverrides()
    data = {name: getattr(row, name) for name in SecuritySettingsOverrides.model_fields}
    data["ssrf_protection_level"] = _coerce_ssrf_level(data["ssrf_protection_level"])
    return SecuritySettingsOverrides.model_validate(data)


def upsert_overrides(db_session: Session, overrides: SecuritySettingsOverrides) -> None:
    """Upsert the singleton row.

    We pass every column explicitly (not ``exclude_none``) so DO UPDATE
    actually clears fields the admin removed — leaving them out would keep
    the previously-set value.
    """
    payload = {
        name: getattr(overrides, name)
        for name in SecuritySettingsOverrides.model_fields
    }
    stmt = insert(SecuritySettingsRow).values(id=True, **payload)
    stmt = stmt.on_conflict_do_update(index_elements=["id"], set_=payload)
    db_session.execute(stmt)
    db_session.commit()
