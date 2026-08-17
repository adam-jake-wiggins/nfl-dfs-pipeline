"""Salary-source adapters.

The unstable dependency is isolated here. Every adapter produces the same
normalized :class:`~dfs_pipeline.adapters.base.SlatePlayer` records, so
downstream code never learns which source a slate came from.
"""

from dfs_pipeline.adapters.base import (
    GameInfo,
    SalarySource,
    SlatePlayer,
    SlateSchemaError,
    parse_game_info,
)
from dfs_pipeline.adapters.dk_csv import (
    REQUIRED_COLUMNS,
    DraftKingsCsvAdapter,
)
from dfs_pipeline.adapters.dk_api import (
    ROSTER_SLOTS,
    DraftGroup,
    DraftKingsApiAdapter,
    DraftKingsApiError,
)
from dfs_pipeline.adapters.fantasypros_csv import (
    LAYOUTS,
    SEASON_AVERAGE_METRIC,
    FantasyProsCsvAdapter,
)
from dfs_pipeline.adapters.projections_csv import (
    ProjectionRow,
    ProjectionsCsvAdapter,
)
from dfs_pipeline.adapters.odds_api import (
    OddsApiAdapter,
    OddsApiError,
    QuotaExhausted,
    TeamOdds,
)

__all__ = [
    "SlatePlayer",
    "GameInfo",
    "SalarySource",
    "SlateSchemaError",
    "parse_game_info",
    "DraftKingsCsvAdapter",
    "REQUIRED_COLUMNS",
    "OddsApiAdapter",
    "OddsApiError",
    "QuotaExhausted",
    "TeamOdds",
    "ProjectionRow",
    "ProjectionsCsvAdapter",
    "FantasyProsCsvAdapter",
    "LAYOUTS",
    "SEASON_AVERAGE_METRIC",
    "DraftKingsApiAdapter",
    "DraftKingsApiError",
    "DraftGroup",
    "ROSTER_SLOTS",
]
