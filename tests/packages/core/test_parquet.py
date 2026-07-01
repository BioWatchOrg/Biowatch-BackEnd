import pandas as pd
import pyarrow.parquet as pq
import pytest
from core.parquet import write_parquet


@pytest.fixture
def sample_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "zone_id": ["8a1fb46622dffff", "8a1fb46622d7fff"],
            "ndvi": [0.42, 0.71],
            "bucket_id": [202601, 202601],
        }
    )


def test_write_parquet_creates_file(tmp_path, sample_df):
    path = tmp_path / "features.parquet"
    write_parquet(sample_df, str(path))
    assert path.exists()
    assert path.stat().st_size > 0


def test_write_parquet_roundtrip_preserves_data(tmp_path, sample_df):
    path = tmp_path / "features.parquet"
    write_parquet(sample_df, str(path))

    reloaded = pq.read_table(str(path)).to_pandas()
    pd.testing.assert_frame_equal(reloaded, sample_df)


def test_write_parquet_preserves_column_order(tmp_path, sample_df):
    path = tmp_path / "features.parquet"
    write_parquet(sample_df, str(path))

    schema = pq.read_schema(str(path))
    assert schema.names == list(sample_df.columns)


def test_write_parquet_preserves_dtypes(tmp_path, sample_df):
    path = tmp_path / "features.parquet"
    write_parquet(sample_df, str(path))

    reloaded = pq.read_table(str(path)).to_pandas()
    assert list(reloaded.dtypes) == list(sample_df.dtypes)


def test_write_parquet_empty_dataframe(tmp_path):
    df = pd.DataFrame(
        {"zone_id": pd.Series([], dtype="object"), "ndvi": pd.Series([], dtype="float64")}
    )
    path = tmp_path / "empty.parquet"
    write_parquet(df, str(path))

    reloaded = pq.read_table(str(path)).to_pandas()
    assert reloaded.empty
    assert list(reloaded.columns) == ["zone_id", "ndvi"]


def test_write_parquet_overwrites_existing_file(tmp_path, sample_df):
    path = tmp_path / "features.parquet"
    write_parquet(sample_df, str(path))

    smaller = sample_df.iloc[:1]
    write_parquet(smaller, str(path))

    reloaded = pq.read_table(str(path)).to_pandas()
    assert len(reloaded) == 1


def test_write_parquet_is_deterministic(tmp_path, sample_df):
    a = tmp_path / "a.parquet"
    b = tmp_path / "b.parquet"
    write_parquet(sample_df, str(a))
    write_parquet(sample_df, str(b))

    df_a = pq.read_table(str(a)).to_pandas()
    df_b = pq.read_table(str(b)).to_pandas()
    pd.testing.assert_frame_equal(df_a, df_b)


def test_write_parquet_invalid_directory_raises(tmp_path, sample_df):
    path = tmp_path / "missing_dir" / "features.parquet"
    with pytest.raises(Exception):
        write_parquet(sample_df, str(path))
