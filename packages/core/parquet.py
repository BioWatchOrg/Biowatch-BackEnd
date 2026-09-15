import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq


def write_parquet(df: pd.DataFrame, file_path: str) -> None:
    """
    Write a pandas DataFrame to a Parquet file.

    Args:
        df (pd.DataFrame): The DataFrame to write.
        file_path (str): The path to the output Parquet file.
    """
    table = pa.Table.from_pandas(df)
    pq.write_table(table, file_path)
