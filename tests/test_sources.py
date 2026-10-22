from conftest import BG_IN, BG_OUT, TRACT


def test_geography_is_clipped_to_the_study_area(sources):
    assert sources.load("place")["area_id"].tolist() == ["2399999"]
    assert sorted(sources.load("block_groups")["area_id"]) == BG_IN
    assert sources.load("tracts")["area_id"].tolist() == [TRACT]
    blocks = sources.load("blocks")
    assert set(blocks["block_group"]) == set(BG_IN)
    assert blocks["housing2020"].dtype.kind == "i"


def test_parcels_inside_only_and_split_lots_merged(sources):
    parcels = sources.load("parcels")
    assert parcels["parcel_id"].is_unique
    assert len(parcels) == 8                     # 4 blocks x 2 parcels; outside block group dropped
    assert parcels.crs.to_epsg() == 4326


def test_e911_points_keep_unit_and_parcel_key(sources):
    e911 = sources.load("e911")
    assert {"unit", "map_bk_lot", "place_type", "address"} <= set(e911.columns)


def test_pums_is_filtered_to_the_puma_with_replicate_weights(sources):
    hh = sources.load("pums_households")
    pp = sources.load("pums_persons")
    assert set(hh["PUMA"]) == {"01000"} and "H9" not in set(hh["SERIALNO"])
    assert {"WGTP1", "WGTP80"} <= set(hh.columns) and {"PWGTP1", "PWGTP80"} <= set(pp.columns)
    assert len(pp) == 7


def test_acs_keeps_study_levels_and_cleans_jam_values(sources):
    acs = sources.load("acs")
    by_level = acs.groupby("level")["area_id"].unique().to_dict()
    assert sorted(by_level["block_group"]) == BG_IN and BG_OUT not in acs["area_id"].values
    assert list(by_level["county"]) == ["23005"] and list(by_level["state"]) == ["23"]
    assert list(by_level["city"]) == ["2399999"]
    first = acs[acs["cell"] == "B01001_001"]
    assert (first["moe"] == 0).all()             # -555555555 means controlled, so zero error
    assert acs["estimate"].notna().all()


def test_second_load_reads_the_cache(sources):
    sources.load("parcels")
    calls = sources.fetcher.calls
    sources.load("parcels")
    assert sources.fetcher.calls == calls
