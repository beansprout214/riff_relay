"""
Same BFS crawler as crawl_musicbrainz.py, refactored into an importable
function so it can be triggered from the API with artist names supplied
at request time, instead of editing a hardcoded SEED_ARTISTS list and
rerunning the script by hand.
"""

import musicbrainzngs

MAX_DEPTH_DEFAULT = 2
MAX_ARTISTS_DEFAULT = 50  # kept modest -- this runs synchronously inside
                          # a web request, at 1 request/second

musicbrainzngs.set_useragent(
    "RiffRelay",
    "0.1",
    "https://github.com/beansprout214/riff_relay",
)

IDENTITY_RELATION_TYPES = {"is person", "legal name"}
EXCLUDED_RELATION_TYPES = {
    "tribute", "named after artist", "involved with",
    "married", "parent", "sibling", "teacher", "founder",
}


def search_seed_mbid(name):
    result = musicbrainzngs.search_artists(artist=name, limit=5, strict=True)
    matches = result.get("artist-list", [])
    if not matches:
        return None
    for match in matches:
        if match["name"].lower() == name.lower():
            return match["id"], match["name"]
    return matches[0]["id"], matches[0]["name"]


def fetch_relations(mbid):
    result = musicbrainzngs.get_artist_by_id(mbid, includes=["artist-rels"])
    return result["artist"].get("artist-relation-list", [])


def get_or_create_artist(cur, mbid, name):
    cur.execute("SELECT id FROM artists WHERE mbid = %s", (mbid,))
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute(
        """
        INSERT INTO artists (mbid, name)
        VALUES (%s, %s)
        ON CONFLICT (mbid) DO NOTHING
        RETURNING id
        """,
        (mbid, name),
    )
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute("SELECT id FROM artists WHERE mbid = %s", (mbid,))
    return cur.fetchone()[0]


def insert_edge(cur, from_id, to_id, rel_type):
    cur.execute(
        """
        INSERT INTO collaborations (artist_a_id, artist_b_id, relationship_type, source)
        VALUES (%s, %s, %s, 'musicbrainz')
        ON CONFLICT (artist_a_id, artist_b_id, relationship_type) DO NOTHING
        """,
        (from_id, to_id, rel_type),
    )


def insert_alias(cur, alias_name, canonical_id):
    cur.execute(
        """
        INSERT INTO artist_aliases (alias_name, canonical_artist_id)
        VALUES (%s, %s)
        ON CONFLICT (alias_name) DO NOTHING
        """,
        (alias_name, canonical_id),
    )


def crawl(conn, seed_names, max_depth=MAX_DEPTH_DEFAULT, max_artists=MAX_ARTISTS_DEFAULT):
    """Runs a BFS crawl from the given seed artist names, inserting
    everything it finds into the given Postgres connection. Returns a
    summary dict -- useful for an API response so the caller sees what
    actually happened.
    """
    cur = conn.cursor()

    queue = []
    visited_mbids = set()
    unresolved_seeds = []

    for name in seed_names:
        found = search_seed_mbid(name)
        if not found:
            unresolved_seeds.append(name)
            continue
        mbid, canonical_name = found
        get_or_create_artist(cur, mbid, canonical_name)
        conn.commit()
        queue.append((mbid, canonical_name, 0))

    artists_crawled = 0
    edges_found = 0

    while queue and artists_crawled < max_artists:
        mbid, name, depth = queue.pop(0)
        if mbid in visited_mbids:
            continue
        visited_mbids.add(mbid)

        from_id = get_or_create_artist(cur, mbid, name)
        artists_crawled += 1

        relations = fetch_relations(mbid)
        for rel in relations:
            target = rel.get("artist", {})
            target_mbid = target.get("id")
            target_name = target.get("name")
            rel_type = rel.get("type")
            if not target_mbid or not target_name or not rel_type:
                continue

            if rel_type in IDENTITY_RELATION_TYPES:
                insert_alias(cur, target_name, from_id)
                conn.commit()
                continue

            if rel_type in EXCLUDED_RELATION_TYPES:
                continue

            to_id = get_or_create_artist(cur, target_mbid, target_name)
            insert_edge(cur, from_id, to_id, rel_type)
            conn.commit()
            edges_found += 1

            if depth + 1 <= max_depth and target_mbid not in visited_mbids:
                queue.append((target_mbid, target_name, depth + 1))

    cur.close()
    return {
        "artists_crawled": artists_crawled,
        "edges_found": edges_found,
        "unresolved_seeds": unresolved_seeds,
        "max_depth": max_depth,
        "max_artists": max_artists,
    }