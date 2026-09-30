"""Imports every feature's ORM models so ``Base.metadata`` knows all the tables.

SQLAlchemy only learns a table exists when its model module has been imported.
Anything that needs the full schema (``alembic``, ``create_all``, the migration
drift check) imports ``Base`` from here rather than from ``app.db.base``.

ADDING A FEATURE WITH TABLES: add one import line below. If you forget, the
tables silently vanish from ``create_all`` and from ``alembic`` autogenerate;
``tests/test_structure.py`` fails when a ``models.py`` is not listed.
"""

from __future__ import annotations

from app.db.base import Base
from app.features.audit import models as _audit  # noqa: F401
from app.features.charts import models as _charts  # noqa: F401
from app.features.connections import models as _connections  # noqa: F401
from app.features.dashboards import models as _dashboards  # noqa: F401
from app.features.flag_rules import models as _flag_rules  # noqa: F401
from app.features.lists import models as _lists  # noqa: F401
from app.features.queries import models as _queries  # noqa: F401
from app.features.users import models as _users  # noqa: F401

__all__ = ["Base"]
