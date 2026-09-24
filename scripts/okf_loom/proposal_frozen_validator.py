"""Production frozen-validator import surface."""
from .proposal_snapshot import (FrozenFileFacts, ProductionFrozenSnapshotValidator,
                                frozen_validation_policy)

FrozenSnapshotValidatorAdapter = ProductionFrozenSnapshotValidator

__all__ = ["FrozenFileFacts", "ProductionFrozenSnapshotValidator",
           "FrozenSnapshotValidatorAdapter", "frozen_validation_policy"]
