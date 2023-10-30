"""tipgstac collections.

PgSTACCollection and PgSTACCatalog are custom class extending tipg.Collection and tipg.Catalog classes.

"""
import datetime
import json
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote_plus

from buildpg import asyncpg, render
from ciso8601 import parse_rfc3339
from fastapi import HTTPException
from pydantic import Field
from pygeofilter.ast import AstType
from pygeofilter.backends.cql2_json import to_cql2

from tipg.collections import Catalog, Collection, Column, FeatureCollection, Parameter
from tipg.errors import InvalidDatetime, InvalidLimit
from tipg.model import Extent
from tipg.settings import FeaturesSettings
from tipgstac.model import PgSTACSearch

features_settings = FeaturesSettings()


class PgSTACCollection(Collection):
    """Model for DB Table and Function."""

    type: str
    id: str
    table: str
    dbschema: str = Field(alias="schema")
    title: Optional[str] = None
    description: Optional[str] = None
    properties: List[Column] = []
    id_column: Optional[str] = None
    geometry_column: Optional[Column] = None
    datetime_column: Optional[Column] = None
    parameters: List[Parameter] = []
    stac_extent: Optional[Extent] = None
    stac_queryables: Optional[Dict] = None

    model_config = {"extra": "allow"}

    @property
    def extent(self) -> Optional[Extent]:
        """Return extent."""
        return self.stac_extent

    @property
    def queryables(self) -> Dict:
        """Return the queryables."""
        return self.stac_queryables or {}

    @property
    def bounds(self) -> Optional[List[float]]:
        """Return spatial bounds from collection extent."""
        if self.extent and self.extent.spatial:
            return self.extent.spatial.bbox[0]

        return None

    @property
    def dt_bounds(self) -> Optional[List[str]]:
        """Return temporal bounds from collection extent."""
        if self.extent and self.extent.temporal:
            return self.extent.temporal.interval[0]

        return None

    @property
    def crs(self):
        """Return crs of set geometry column."""
        return "http://www.opengis.net/def/crs/EPSG/0/4326"

    async def features(  # noqa: C901
        self,
        pool: asyncpg.BuildPgPool,
        *,
        ids_filter: Optional[List[str]] = None,
        bbox_filter: Optional[List[float]] = None,
        datetime_filter: Optional[List[str]] = None,
        properties_filter: Optional[List[Tuple[str, str]]] = None,
        cql_filter: Optional[AstType] = None,
        query: Optional[str] = None,
        sortby: Optional[str] = None,
        properties: Optional[List[str]] = None,
        limit: Optional[int] = None,
        token: Optional[str] = None,
        bbox_only: Optional[bool] = None,  # Not Available
        simplify: Optional[float] = None,  # Not Available
        geom_as_wkt: bool = False,  # Not Available
    ) -> Tuple[FeatureCollection, Optional[int], Optional[str], Optional[str],]:
        """Build and run PgSTAC query."""
        if limit and limit > features_settings.max_features_per_query:
            raise InvalidLimit(
                f"Limit can not be set higher than the `tipg_max_features_per_query` setting of {features_settings.max_features_per_query}"
            )

        if datetime_filter:
            if len(datetime_filter) == 2:
                start = (
                    parse_rfc3339(datetime_filter[0])
                    if datetime_filter[0] not in ["..", ""]
                    else None
                )
                end = (
                    parse_rfc3339(datetime_filter[1])
                    if datetime_filter[1] not in ["..", ""]
                    else None
                )

                if start is None and end is None:
                    raise InvalidDatetime(
                        "Double open-ended datetime intervals are not allowed."
                    )

                if start is not None and end is not None and start > end:
                    raise InvalidDatetime(
                        "Start datetime cannot be before end datetime."
                    )

            datetime_filter = "/".join(datetime_filter)  # type: ignore

        base_args = {
            "collections": [self.id],
            "ids": ids_filter,
            "bbox": bbox_filter,
            "limit": limit or features_settings.default_features_limit,
            "token": token,
            "query": json.loads(unquote_plus(query)) if query else query,
        }

        if cql_filter:
            base_args["filter"] = json.loads(to_cql2(cql_filter))
            base_args["filter-lang"] = "cql2-json"

        if datetime_filter:
            base_args["datetime"] = datetime_filter

        # if sortby:
        #     # https://github.com/radiantearth/stac-spec/tree/master/api-spec/extensions/sort#http-get-or-post-form
        #     sort_param = []
        #     for sort in sortby:
        #         sortparts = re.match(r"^([+-]?)(.*)$", sort)
        #         if sortparts:
        #             sort_param.append(
        #                 {
        #                     "field": sortparts.group(2).strip(),
        #                     "direction": "desc" if sortparts.group(1) == "-" else "asc",
        #                 }
        #             )
        #     base_args["sortby"] = sort_param

        # TODO: properties_filter

        if properties:
            base_args["fields"] = {"include": set(properties), "exclude": set()}

        clean = {}
        for k, v in base_args.items():
            if v is not None and v != []:
                clean[k] = v

        search = PgSTACSearch.model_validate(clean)
        try:
            async with pool.acquire() as conn:
                q, p = render(
                    """
                    SELECT * FROM pgstac.search(:req::text::jsonb);
                    """,
                    req=search.model_dump_json(exclude_none=True, by_alias=True),
                )
                fc = await conn.fetchval(q, *p)
        except Exception as e:
            if "Could not find item using token:" in repr(e):
                raise HTTPException(
                    status_code=404, detail=f"Invalid toke: {token}."
                ) from e
            fc = {}

        count = None
        if context := fc.get("context"):
            count = context.get("matched")

        next_token = fc.get("next")
        prev_token = fc.get("prev")

        return (
            FeatureCollection(
                type="FeatureCollection", features=fc.get("features", [])
            ),
            count,
            next_token,
            prev_token,
        )

    async def get_tile(
        self,
        *,
        pool: asyncpg.BuildPgPool,
        **kwargs: Any,
    ):
        """Build query to get Vector Tile."""
        raise NotImplementedError


class PgSTACCatalog(Catalog):
    """Collection Catalog."""

    collections: Dict[str, PgSTACCollection]
    last_updated: datetime.datetime
