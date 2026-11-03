"""A `government:` config with every government source switched off, however many state sites exist, so a test never reaches the network.
`retail_off()` does the same for the `retailers:` section."""
import gov_sources
import retail_sources


def gov_off() -> dict:
    return {"usajobs": {"enabled": False}, "texas_hhs": {"enabled": False},
            **{key: {"enabled": False} for key, *_ in gov_sources.STATE_SOURCES}}


def retail_off() -> dict:
    return {key: {"enabled": False} for key, *_ in retail_sources.RETAIL_SOURCES}
