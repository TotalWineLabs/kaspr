import importlib

import pytest

from kaspr.types import settings

# setting name -> (value to set in the environment, expected parsed value)
ROCKSDB_SETTINGS = {
    "STORE_ROCKSDB_WRITE_BUFFER_SIZE": ("33554432", 33554432),
    "STORE_ROCKSDB_MAX_WRITE_BUFFER_NUMBER": ("5", 5),
    "STORE_ROCKSDB_TARGET_FILE_SIZE_BASE": ("33554432", 33554432),
    "STORE_ROCKSDB_BLOCK_CACHE_SIZE": ("123456789", 123456789),
    "STORE_ROCKSDB_BLOCK_CACHE_COMPRESSED_SIZE": ("123456789", 123456789),
    "STORE_ROCKSDB_BLOOM_FILTER_SIZE": ("10", 10),
    "STORE_ROCKSDB_SET_CACHE_INDEX_AND_FILTER_BLOCKS": ("true", True),
}


@pytest.fixture
def reload_settings(monkeypatch):
    """Re-evaluate the module level settings with a given environment."""
    for name in ROCKSDB_SETTINGS:
        for prefix in settings.PREFICES:
            monkeypatch.delenv(prefix + name, raising=False)

    def reload(**env):
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        return importlib.reload(settings)

    yield reload
    monkeypatch.undo()
    importlib.reload(settings)


@pytest.mark.parametrize("name", ROCKSDB_SETTINGS)
@pytest.mark.parametrize("prefix", ["K_", "KASPR_"])
def test_rocksdb_setting_is_read_from_its_own_env_var(reload_settings, name, prefix):
    defaults = {n: getattr(reload_settings(), n) for n in ROCKSDB_SETTINGS}
    value, expected = ROCKSDB_SETTINGS[name]

    loaded = reload_settings(**{prefix + name: value})

    assert getattr(loaded, name) == expected
    # no other setting may pick up this env var
    for other in ROCKSDB_SETTINGS.keys() - {name}:
        assert getattr(loaded, other) == defaults[other], other


def test_bloom_filter_size_defaults_to_3(reload_settings):
    assert reload_settings().STORE_ROCKSDB_BLOOM_FILTER_SIZE == 3
