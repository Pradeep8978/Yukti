"""
yukti/services/bootstrap_service.py
Handles application bootstrap: database, broker initialization, crash recovery.
"""
from __future__ import annotations

import logging

from yukti.data.database import create_all_tables
from yukti.data.state import set_halt
from yukti.execution.reconcile import reconcile_positions, recover_from_crash
from yukti.execution.broker_factory import get_broker
from yukti.scheduler.jobs import is_trading_day

log = logging.getLogger(__name__)


class BootstrapService:
    def __init__(self) -> None:
        self.broker = None

    async def bootstrap(self, mode: str) -> None:
        """Run all bootstrap steps."""
        log.info("BootstrapService: starting bootstrap for mode=%s", mode)

        # 1. Create tables
        await create_all_tables()

        # 2. Initialize broker
        self.broker = get_broker()

        # Wire broker into dhan_client module
        import yukti.execution.dhan_client as _dc
        _dc.dhan = self.broker

        # 3. Crash recovery
        log.info("BootstrapService: running crash recovery")
        recovery_stats = await recover_from_crash()
        if recovery_stats.get("emergency_exit", 0) > 0:
            log.critical("BootstrapService: emergency exits performed: %d", recovery_stats["emergency_exit"])

        # 4. Daily reconciliation if trading day
        if is_trading_day():
            ok = await reconcile_positions()
            if not ok:
                log.critical("BootstrapService: reconciliation failed — starting HALTED")
                await set_halt(True)

        log.info("BootstrapService: bootstrap complete")