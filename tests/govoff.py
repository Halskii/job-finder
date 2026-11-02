"""A `government:` config with every government source switched off, however many state sites exist, so a test never reaches the network."""
import gov_sources


def gov_off() -> dict:
    return {"usajobs": {"enabled": False}, "texas_hhs": {"enabled": False},
            **{key: {"enabled": False} for key, *_ in gov_sources.STATE_SOURCES}}
