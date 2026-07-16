import os
import psycopg2
from fastapi import FastAPI, HTTPException

import crawl

app = FastAPI()
DATABASE_URL = os.environ.get("DATABASE_URL", "not set")


def get_connection():
    if DATABASE_URL == "not set":
        raise RuntimeError("DATABASE_URL not set")
    return psycopg2.connect(DATABASE_URL)


@app.get("/")
def read_root():
    return {"status": "alive"}


@app.get("/db-check")
def db_check():
    return {"database_url_present": DATABASE_URL != "not set"}


@app.post("/crawl")
def crawl_artist(artist: str, depth: int = 2, max_artists: int = 50):
    """Crawl outward from a given artist name, on demand -- no more
    editing a hardcoded seed list. Runs synchronously (blocks until
    done), so keep depth/max_artists modest for a request that needs
    to return in a reasonable time -- MusicBrainz is rate-limited to
    1 request/second, so max_artists=50 takes roughly 50 seconds.
    """
    # Cap the inputs so a request can't accidentally trigger a
    # multi-hour crawl -- this is a safety rail, not a suggestion to
    # raise these numbers casually from the API.
    depth = min(depth, 3)
    max_artists = min(max_artists, 100)

    conn = get_connection()
    try:
        result = crawl.crawl(conn, [artist], max_depth=depth, max_artists=max_artists)
    finally:
        conn.close()

    if result["artists_crawled"] == 0 and artist in result["unresolved_seeds"]:
        raise HTTPException(status_code=404, detail=f"No MusicBrainz artist found matching '{artist}'")

    return result


def resolve_artist_id(cur, name):
    """Look up an artist's Postgres id by name, checking both the
    artists table directly and the artist_aliases table -- so a user
    typing either 'Frank Ocean' or 'Christopher Breaux' finds the same
    node.
    """
    cur.execute("SELECT id FROM artists WHERE name ILIKE %s", (name,))
    row = cur.fetchone()
    if row:
        return row[0]

    cur.execute(
        "SELECT canonical_artist_id FROM artist_aliases WHERE alias_name ILIKE %s",
        (name,),
    )
    row = cur.fetchone()
    if row:
        return row[0]

    return None


@app.get("/shortest-path")
def shortest_path(from_artist: str, to_artist: str, max_depth: int = 6):
    conn = get_connection()
    cur = conn.cursor()

    from_id = resolve_artist_id(cur, from_artist)
    to_id = resolve_artist_id(cur, to_artist)

    if from_id is None:
        cur.close()
        conn.close()
        raise HTTPException(status_code=404, detail=f"'{from_artist}' not found in the graph yet -- try crawling it first")
    if to_id is None:
        cur.close()
        conn.close()
        raise HTTPException(status_code=404, detail=f"'{to_artist}' not found in the graph yet -- try crawling it first")

    cur.execute(
        """
        WITH RECURSIVE path AS (
            SELECT artist_a_id AS current, ARRAY[artist_a_id] AS visited, 0 AS depth
            FROM collaborations
            WHERE artist_a_id = %s

            UNION ALL

            SELECT c.artist_b_id, path.visited || c.artist_b_id, path.depth + 1
            FROM collaborations c
            JOIN path ON c.artist_a_id = path.current
            WHERE NOT c.artist_b_id = ANY(path.visited)
            AND path.depth < %s
        )
        SELECT visited, depth FROM path
        WHERE current = %s
        ORDER BY depth ASC
        LIMIT 1
        """,
        (from_id, max_depth, to_id),
    )
    result = cur.fetchone()

    if result is None:
        cur.close()
        conn.close()
        return {
            "connected": False,
            "message": f"No path found within {max_depth} hops. The graph may need more crawling.",
        }

    visited_ids, depth = result

    # Map ids back to display names, preserving path order
    cur.execute("SELECT id, name FROM artists WHERE id = ANY(%s)", (visited_ids,))
    id_to_name = dict(cur.fetchall())
    path_names = [id_to_name[artist_id] for artist_id in visited_ids]

    cur.close()
    conn.close()

    return {
        "connected": True,
        "degrees_of_separation": depth,
        "path": path_names,
    }