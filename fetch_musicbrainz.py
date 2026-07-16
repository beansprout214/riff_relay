"""
Fetches artist and relationship data from MusicBrainz for a test set of
artists chosen to have both connections and non-connections, so we can
sanity-check our edge-building logic before scaling up.

Follows MusicBrainz API etiquette:
- musicbrainzngs sets a proper User-Agent for us (required by MB's rules)
- musicbrainzngs auto rate-limits to 1 request/second (MB's suggested limit)
- No scheduled/cron polling here -- this is a one-off manual run, not a
  background job waking up at a fixed time (which MB explicitly asks
  applications to avoid)

IMPORTANT: before running, replace the placeholder contact info below with
your real repo URL or email. MusicBrainz uses this to reach you if your
app is causing problems -- an honest, identifiable User-Agent is exactly
what keeps you from getting blocked.
"""

import os
import musicbrainzngs
import psycopg2

# --- MusicBrainz setup -------------------------------------------------

# REQUIRED by MusicBrainz's API rules: identify your app and give them a
# way to reach you. Swap in your actual GitHub repo URL or email.
musicbrainzngs.set_useragent(
    "RiffRelau",
    "0.1",
    "https://github.com/beansprout214/riff_relay",
)

# A small, deliberately mixed test set:
# - Frank Ocean / Tyler, The Creator: known direct feature (edge expected)
# - Alex Turner / Arctic Monkeys / The Last Shadow Puppets: band-member
#   edges expected, connecting two "different" acts through one person
# - Taylor Swift: included with no expected connection to the above, to
#   confirm our code correctly produces NO edge rather than a false one
TEST_ARTISTS = [
    "Frank Ocean",
    "Tyler, The Creator",
    "Alex Turner",
    "Arctic Monkeys",
    "The Last Shadow Puppets",
    "Taylor Swift",
    "Travis Scott"
]


def search_artist_mbid(name):
    """Look up an artist by name and return their MusicBrainz ID (mbid).

    MusicBrainz's default search is fuzzy/relevance-ranked, not exact --
    searching "Alex Turner" unquoted can return "Tina Turner" if she has
    a more heavily-linked entry, since the search is loosely matching on
    either term. strict=True forces exact phrase matching instead, which
    fixes this for well-known, unambiguous names. For a production
    version with less-famous artists you'd still want a disambiguation
    step, since even exact-name matches can collide (multiple artists
    can share an identical name).
    """
    result = musicbrainzngs.search_artists(artist=name, limit=5, strict=True)
    matches = result.get("artist-list", [])
    if not matches:
        print(f"  no match found for '{name}'")
        return None

    # Even with strict matching, prefer a case-insensitive exact match if
    # one exists among the results, rather than blindly trusting rank 0.
    for match in matches:
        if match["name"].lower() == name.lower():
            return match["id"], match["name"]

    return matches[0]["id"], matches[0]["name"]


def fetch_artist_relations(mbid):
    """Fetch an artist's relationships to other artists.

    includes=["artist-rels"] asks MusicBrainz to return relationship data
    (band membership, collaborations, etc.) alongside the base artist
    record, so we get everything we need in one call instead of a
    separate request per relationship.
    """
    result = musicbrainzngs.get_artist_by_id(mbid, includes=["artist-rels"])
    return result["artist"].get("artist-relation-list", [])


def fetch_all_test_data():
    """Fetch artist + relation data for the whole test set.

    musicbrainzngs handles the 1 req/sec throttling internally, so we
    don't need manual time.sleep() calls between requests -- that's
    exactly the kind of easy-to-get-wrong logic the library exists to
    handle correctly on our behalf.
    """
    artists = {}  # name -> (mbid, canonical_name)
    relations = []  # list of (from_name, to_name, relation_type)

    print("Fetching artists from MusicBrainz (rate-limited to 1 req/sec)...")
    for name in TEST_ARTISTS:
        print(f"Looking up: {name}")
        found = search_artist_mbid(name)
        if not found:
            continue
        mbid, canonical_name = found
        artists[canonical_name] = mbid

        rels = fetch_artist_relations(mbid)
        for rel in rels:
            target = rel.get("artist", {}).get("name")
            rel_type = rel.get("type")
            if target and rel_type:
                relations.append((canonical_name, target, rel_type))
                print(f"  found relation: {canonical_name} --[{rel_type}]--> {target}")

    return artists, relations


# --- Postgres insertion --------------------------------------------------

def get_connection():
    """Connect using Railway's injected DATABASE_URL.

    Set DATABASE_URL in your terminal before running this script, e.g.:
        export DATABASE_URL="<paste from Railway Postgres Variables tab>"
    Using the same env var name Railway already provides keeps this
    script portable between your laptop and the deployed app.
    """
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        raise RuntimeError(
            "DATABASE_URL not set. Copy it from Railway's Postgres "
            "service > Variables tab, then run:\n"
            "  export DATABASE_URL='postgresql://...'"
        )
    return psycopg2.connect(db_url)


def insert_data(artists, relations):
    conn = get_connection()
    cur = conn.cursor()

    name_to_id = {}
    for name in artists:
        cur.execute(
            """
            INSERT INTO artists (name)
            VALUES (%s)
            ON CONFLICT DO NOTHING
            RETURNING id
            """,
            (name,),
        )
        row = cur.fetchone()
        if row:
            name_to_id[name] = row[0]
        else:
            # already existed -- look up its id
            cur.execute("SELECT id FROM artists WHERE name = %s", (name,))
            name_to_id[name] = cur.fetchone()[0]

    inserted_edges = 0
    for from_name, to_name, rel_type in relations:
        # Only insert edges where both ends are artists we actually
        # looked up (the target of a relation might be someone outside
        # our small test set -- fine to skip those for this test run).
        if from_name in name_to_id and to_name in name_to_id:
            cur.execute(
                """
                INSERT INTO collaborations
                    (artist_a_id, artist_b_id, relationship_type, source)
                VALUES (%s, %s, %s, 'musicbrainz')
                ON CONFLICT (artist_a_id, artist_b_id, relationship_type)
                DO NOTHING
                """,
                (name_to_id[from_name], name_to_id[to_name], rel_type),
            )
            inserted_edges += 1

    conn.commit()
    cur.close()
    conn.close()
    print(f"\nInserted {len(name_to_id)} artists and {inserted_edges} edges.")


if __name__ == "__main__":
    artists, relations = fetch_all_test_data()
    print(f"\nTotal artists found: {len(artists)}")
    print(f"Total relations found: {len(relations)}")
    insert_data(artists, relations)