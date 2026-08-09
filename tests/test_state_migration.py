"""Schema v2 migration.

The live deployment's persistent disk carries state written while the
chain-wide mint watch existed: tracked tokens with source "mint" and
candidate factories learned from them (which predate per-entry provenance,
so they have no "source" field). Both are poison — the factories are
generic memecoin deployers, not the Stonk Launcher — and must be purged
once, automatically, on the first boot after upgrade.
"""
import json

from stonk_watcher.state import State

MINT_TOKEN = "0xa00b6fd10300717864c2d009302bc096685b7777"   # VERTO-style leftover
API_TOKEN = "0xc10c1c10c1c10c1c10c1c10c1c10c1c10c1c10c1"
BOGUS_FACTORY = "0x0fbad98595b0186da120e41f77c102beb49f803c"  # learned from VERTO
GOOD_FACTORY = "0xfac70fac70fac70fac70fac70fac70fac70fac70"


def write_v1_state(path):
    """State as the pre-v2 code actually wrote it."""
    data = {
        "api_seen_tokens": [API_TOKEN],
        "tracked": {
            MINT_TOKEN: {"source": "mint", "name": "VERTO", "symbol": "VERTO",
                         "creator_resolved": True},
            API_TOKEN: {"source": "api", "name": "CLOCKIN", "symbol": "CLOCKIN"},
        },
        "candidate_factories": {
            BOGUS_FACTORY: {"deploy_block": 100, "last_scanned": 200,
                            "learned_topic0": None, "confirmed_tokens": [],
                            "seen_log_keys": []},  # no "source": pre-v2 entry
        },
        "mint_last_scanned": 12345,
        "mint_seen_tokens": [MINT_TOKEN],
        "meta": {"created_at": 1.0},  # no "schema": v1
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)


def test_migration_purges_mint_tokens_and_sourceless_factories(tmp_path):
    path = str(tmp_path / "state.json")
    write_v1_state(path)
    state = State(path=path)

    assert MINT_TOKEN not in state.data["tracked"], "mint leftovers purged"
    assert API_TOKEN in state.data["tracked"], "API-listed tokens kept"
    assert BOGUS_FACTORY not in state.candidate_factories(), (
        "factories armed without provenance are exactly the bogus ones")
    assert "mint_last_scanned" not in state.data
    assert "mint_seen_tokens" not in state.data
    assert state.data["meta"]["schema"] == State.SCHEMA_VERSION


def test_migration_keeps_provenanced_factories(tmp_path):
    path = str(tmp_path / "state.json")
    write_v1_state(path)
    # A factory with recorded provenance (post-v2 write) must survive even
    # if the file still says v1 overall.
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    data["candidate_factories"][GOOD_FACTORY] = {
        "deploy_block": 300, "last_scanned": 400, "learned_topic0": None,
        "confirmed_tokens": [], "seen_log_keys": [], "source": "team_wallet"}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)

    state = State(path=path)
    assert GOOD_FACTORY in state.candidate_factories()
    assert BOGUS_FACTORY not in state.candidate_factories()


def test_migration_is_idempotent(tmp_path):
    path = str(tmp_path / "state.json")
    write_v1_state(path)
    state1 = State(path=path)
    state1.save()

    state2 = State(path=path)  # second boot: already v2, must not re-run
    state2.data["tracked"]["0x" + "ab" * 20] = {"source": "mint"}  # hypothetical
    state2.save()
    state3 = State(path=path)
    assert "0x" + "ab" * 20 in state3.data["tracked"], (
        "migration must run once, not scrub on every load")


def test_fresh_state_is_already_current(tmp_path):
    state = State(path=str(tmp_path / "state.json"))
    state.save()
    reloaded = State(path=str(tmp_path / "state.json"))
    assert reloaded.data["meta"].get("schema", State.SCHEMA_VERSION) \
        == State.SCHEMA_VERSION


def test_new_factories_record_their_source(tmp_path):
    state = State(path=str(tmp_path / "state.json"))
    state.add_candidate_factory(GOOD_FACTORY, 100, source="team_wallet")
    assert state.candidate_factories()[GOOD_FACTORY]["source"] == "team_wallet"
