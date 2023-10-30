"""tipgstac dependencies."""

from typing import List, Optional

from aiocache import cached
from buildpg import render
from fastapi import HTTPException, Path, Query
from starlette.requests import Request
from typing_extensions import Annotated

from tipgstac.collections import CollectionList, PgSTACCollection
from tipgstac.settings import CacheSettings

cache_config = CacheSettings()


@cached(
    ttl=cache_config.ttl,
    key_builder=lambda _f, request, limit, offset: f"catalog-limit:{limit or 0}-offset:{offset or 0}",
)
async def CatalogParams(
    request: Request,
    # TODO
    # bbox_filter: Annotated[Optional[List[float]], Depends(bbox_query)],
    # datetime_filter: Annotated[Optional[List[str]], Depends(datetime_query)],
    limit: Annotated[
        Optional[int],
        Query(
            ge=0,
            le=1000,
            description="Limits the number of collection in the response.",
        ),
    ] = None,
    offset: Annotated[
        Optional[int],
        Query(
            ge=0,
            description="Starts the response at an offset.",
        ),
    ] = None,
) -> CollectionList:
    """Return Collections Catalog."""
    limit = limit or 10  # add collection limit settings
    offset = offset or 0

    collections: List[PgSTACCollection] = []

    async with request.app.state.pool.acquire() as conn:
        matched = await conn.fetchval("SELECT count(*) FROM pgstac.collections;")
        q, p = render(
            """SELECT content FROM pgstac.collections LIMIT :limit OFFSET :offset;""",
            limit=limit,
            offset=offset,
        )

        for row in await conn.fetch(q, *p):
            collection = row.get("content")
            if not collection:
                continue

            collections.append(
                PgSTACCollection(
                    type="Collection",
                    id=collection["id"],
                    table="collections",
                    schema="pgstac",
                    extent=collection.get("extent"),
                    description=collection.get("description", None),
                    id_column="id",
                    stac_version=collection.get("stac_version"),
                    stac_extensions=collection.get("stac_extensions", []),
                ),
            )

        returned = len(collections)

        return CollectionList(
            collections=collections,
            matched=matched,
            next=offset + returned if matched - returned > offset else None,
            prev=max(offset - returned, 0) if offset else None,
        )


@cached(
    ttl=cache_config.ttl,
    key_builder=lambda _f, request, collectionId: collectionId,
)
async def CollectionParams(
    request: Request,
    collectionId: Annotated[str, Path(description="Collection identifier")],
) -> PgSTACCollection:
    """Collection Dependency."""
    async with request.app.state.pool.acquire() as conn:
        q, p = render(
            """
            WITH t AS (
                SELECT
                    *
                FROM
                    pgstac.get_collection(:id::text) c,
                    pgstac.get_queryables(:id::text) q
            )
            SELECT
                COALESCE(c || jsonb_build_object('queryables', q))
            FROM t;
            """,
            id=collectionId,
        )
        try:
            collection = await conn.fetchval(q, *p)
        except Exception:  # TODO: better error handling
            collection = None
            pass

        if not collection:
            raise HTTPException(
                status_code=404, detail=f"Collection '{collectionId}' not found."
            )

        queryables = None
        if collection.get("queryables"):
            queryables = collection["queryables"].get("properties")

        return PgSTACCollection(
            type="Collection",
            id=collection["id"],
            table="collections",
            schema="pgstac",
            stac_extent=collection.get("extent"),
            description=collection.get("description", None),
            id_column="id",
            stac_queryables=queryables,
            stac_version=collection.get("stac_version"),
            stac_extensions=collection.get("stac_extensions", []),
        )
