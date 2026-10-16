"""Unit tests for deterministic team_k mapping and capacity limits."""

import pytest

from CTFd.models import db
from CTFd.plugins.blueagent.models import BlueAgentTeamMapping, CapacityError


def test_team_mapping_allocation(app):
    with app.app_context():
        # Clear existing mappings
        BlueAgentTeamMapping.query.delete()
        db.session.commit()

        # Allocate first team
        m1 = BlueAgentTeamMapping.get_or_create(account_id=101, account_type="team")
        assert m1.team_k == 1

        # Idempotency check: same account returns same mapping
        m1_again = BlueAgentTeamMapping.get_or_create(account_id=101, account_type="team")
        assert m1_again.id == m1.id
        assert m1_again.team_k == 1

        # Allocate second team -> should receive team_k 2
        m2 = BlueAgentTeamMapping.get_or_create(account_id=102, account_type="team")
        assert m2.team_k == 2


def test_team_mapping_capacity_exhaustion(app):
    with app.app_context():
        BlueAgentTeamMapping.query.delete()
        db.session.commit()

        # Fill all 254 slots
        for k in range(1, 255):
            m = BlueAgentTeamMapping(account_id=k, account_type="team", team_k=k)
            db.session.add(m)
        db.session.commit()

        # Attempting to allocate team 255 must raise CapacityError
        with pytest.raises(CapacityError, match="All 254 team subnet slots"):
            BlueAgentTeamMapping.get_or_create(account_id=9999, account_type="team")
