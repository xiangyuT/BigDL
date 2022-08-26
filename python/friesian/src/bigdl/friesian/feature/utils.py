#
# Copyright 2016 The BigDL Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

from bigdl.dllib.utils.file_utils import callZooFunc
from pyspark.sql.types import IntegerType, ShortType, LongType, FloatType, DecimalType, \
    DoubleType, BooleanType
from pyspark.sql.functions import broadcast, udf
from bigdl.dllib.utils.log4Error import *
import warnings
from bigdl.dllib.utils.log4Error import *
from typing import TYPE_CHECKING, Any, List, Optional, Tuple, Union

if TYPE_CHECKING:
    from bigdl.friesian.feature.table import FeatureTable, TargetCode
    from pyspark.sql.dataframe import DataFrame as SparkDataFrame


def compute(df: "SparkDataFrame"):
    return callZooFunc("float", "compute", df)


def log_with_clip(df: "SparkDataFrame", columns: List[str], clip: bool=True) -> "SparkDataFrame":
    return callZooFunc("float", "log", df, columns, clip)


def generate_string_idx(df: "SparkDataFrame",
                        columns: List[str],
                        freq_limit: Optional[str],
                        order_by_freq: bool) -> List["SparkDataFrame"]:
    return callZooFunc("float", "generateStringIdx", df, columns, freq_limit, order_by_freq)


def fill_na(df: "SparkDataFrame",
            fill_val: Union[int, str, float],
            columns: List[str]) -> "SparkDataFrame":
    return callZooFunc("float", "fillNa", df, fill_val, columns)


def fill_na_int(df: "SparkDataFrame",
                fill_val: int,
                columns: Optional[List[str]]) -> "SparkDataFrame":
    return callZooFunc("float", "fillNaInt", df, fill_val, columns)


def clip(df: "SparkDataFrame",
         columns: List[str],
         min: Optional[int],
         max: Optional[int]) -> "SparkDataFrame":
    return callZooFunc("float", "clip", df, columns, min, max)


def fill_median(df: "SparkDataFrame", columns: List[str]) -> "SparkDataFrame":
    return callZooFunc("float", "fillMedian", df, columns)


def median(df: "SparkDataFrame",
           columns: List[str],
           relative_error: float=0.001) -> "SparkDataFrame":
    return callZooFunc("float", "median", df, columns, relative_error)


# TODO: ADD UTS
def cross_columns(df,
                  cross_column_list: Union[List[str], List[List[str]], str],
                  bucket_sizes: int):
    return callZooFunc("float", "crossColumns", df, cross_column_list, bucket_sizes)


def check_col_exists(df: "SparkDataFrame", columns: List[str]) -> None:
    df_cols = df.columns
    col_not_exist = list(filter(lambda x: x not in df_cols, columns))
    if len(col_not_exist) > 0:
        invalidInputError(False,
                          str(col_not_exist) + " do not exist in this Table")


def add_negative_samples(df: "SparkDataFrame",
                         item_size: int,
                         item_col: str,
                         label_col: str,
                         neg_num: int) -> "SparkDataFrame":
    return callZooFunc("float", "addNegSamples", df, item_size, item_col, label_col, neg_num)


def add_hist_seq(df: "SparkDataFrame",
                 cols: List[str],
                 user_col: str,
                 sort_col: str,
                 min_len: int,
                 max_len: int,
                 num_seqs: int) -> "SparkDataFrame":
    return callZooFunc("float", "addHistSeq", df, cols, user_col, sort_col, min_len, max_len,
                       num_seqs)


def add_neg_hist_seq(df: "SparkDataFrame",
                     item_size: int,
                     item_history_col: str,
                     neg_num: int) -> "SparkDataFrame":
    return callZooFunc("float", "addNegHisSeq", df, item_size, item_history_col, neg_num)


def add_value_features(df: "SparkDataFrame",
                       cols: List[str],
                       map_df: "SparkDataFrame",
                       key: str,
                       value: str) -> "SparkDataFrame":
    return callZooFunc("float", "addValueFeatures", df, cols, map_df, key, value)


def mask(df: "SparkDataFrame", mask_cols: Optional[Union[str, List[str]]], seq_len: int):
    return callZooFunc("float", "mask", df, mask_cols, seq_len)


def pad(df: "SparkDataFrame",
        cols: List[str],
        seq_len: int,
        mask_cols: Optional[Union[str, List[str]]],
        mask_token: Union[int, str]) -> "SparkDataFrame":
    df = callZooFunc("float", "mask", df, mask_cols, seq_len) if mask_cols else df
    df = callZooFunc("float", "postPad", df, cols, seq_len, mask_token)
    return df


def check_column_numeric(df: "SparkDataFrame", column: str) -> bool:
    return df.schema[column].dataType in [IntegerType(), ShortType(),
                                          LongType(), FloatType(),
                                          DecimalType(), DoubleType()]


def ordinal_shuffle_partition(df: "SparkDataFrame") -> "SparkDataFrame":
    return callZooFunc("float", "ordinalShufflePartition", df)


def write_parquet(df: "SparkDataFrame", path: str, mode: str) -> None:
    callZooFunc("float", "dfWriteParquet", df, path, mode)


def check_col_str_list_exists(df: "SparkDataFrame",
                              column: Union[List[str], str],
                              arg_name: str) -> None:
    if isinstance(column, str):
        invalidInputError(column in df.columns,
                          column + " in " + arg_name + " does not exist in Table")
    elif isinstance(column, list):
        for single_column in column:
            invalidInputError(single_column in df.columns,
                              "{} in {} does not exist in Table".format(single_column, arg_name))
    else:
        invalidInputError(False,
                          "elements in cat_cols should be str or list of str but"
                          " get " + str(column))


def get_nonnumeric_col_type(df: "SparkDataFrame",
                            columns: Optional[Union[str, List[str]]]) \
        -> List[Union[Tuple[str, str], Any]]:
    return list(filter(
        lambda x: x[0] in columns and not (x[1] == "smallint" or x[1] == "int" or
                                           x[1] == "bigint" or x[1] == "float" or x[1] == "double"),
        df.dtypes))


def gen_cols_name(columns: Union[List[str], str], name_sep: str="_") -> str:
    if isinstance(columns, str):
        return columns
    elif isinstance(columns, list):
        return name_sep.join(columns)
    else:
        invalidInputError(False,
                          "item should be either str or list of str")


def encode_target_(tbl: "FeatureTable",
                   targets: List["TargetCode"],
                   target_cols: Optional[List[str]]=None,
                   drop_cat: bool=True,
                   drop_fold: bool=True,
                   fold_col: Optional[str]=None) -> "FeatureTable":
    for target_code in targets:
        cat_col = target_code.cat_col
        out_target_mean = target_code.out_target_mean
        join_tbl = tbl._clone(target_code.df)
        invalidInputError("target_encode_count" in join_tbl.df.columns,
                          "target_encode_count should be in target_code")
        # (keys of out_target_mean) should include (output columns)
        output_columns = list(filter(lambda x:
                                     ((isinstance(cat_col, str) and x != cat_col) or
                                      (isinstance(cat_col, list) and x not in cat_col)) and
                                     (fold_col is not None and x != fold_col) and
                                     (x != "target_encode_count"),
                                     join_tbl.df.columns))

        for column in output_columns:
            invalidInputError(column in out_target_mean, column + " should be in out_target_mean")
            column_mean = out_target_mean[column][1]
            invalidInputError(isinstance(column_mean, int) or isinstance(column_mean, float),
                              "mean in target_mean should be numeric but get {} of type"
                              " {} in {}".format(column_mean, type(column_mean), out_target_mean))

        # select target_cols to join
        if target_cols is not None:
            new_out_target_mean = {}
            for out_col, target_mean in out_target_mean.items():
                if target_mean[0] not in target_cols:
                    join_tbl = join_tbl.drop(out_col)
                else:
                    new_out_target_mean[out_col] = target_mean
            out_target_mean = new_out_target_mean

        all_size = join_tbl.size()
        limit_size = 100000000
        t_df = join_tbl.df
        top_df = t_df if all_size <= limit_size \
            else t_df.sort(t_df.target_encode_count.desc()).limit(limit_size)
        br_df = broadcast(top_df.drop("target_encode_count"))

        if fold_col is None:
            join_key = cat_col
        else:
            join_key = [cat_col, fold_col] if isinstance(cat_col, str) else cat_col + [fold_col]

        if all_size <= limit_size:
            joined = tbl.df.join(br_df, on=join_key, how="left")
        else:
            keyset = set(top_df.select(cat_col).rdd.map(lambda r: r[0]).collect())
            filter_udf = udf(lambda key: key in keyset, BooleanType())
            df1 = tbl.df.filter(filter_udf(cat_col))
            df2 = tbl.df.subtract(df1)
            joined1 = df1.join(br_df, on=join_key)
            joined2 = df2.join(t_df.drop("target_encode_count").subtract(br_df),
                               on=join_key, how="left")
            joined = joined1.union(joined2)

        tbl = tbl._clone(joined)
        # for new columns, fill na with mean
        for out_col, target_mean in out_target_mean.items():
            if out_col in tbl.df.columns:
                tbl = tbl.fillna(target_mean[1], out_col)

    if drop_cat:
        for target_code in targets:
            if isinstance(target_code.cat_col, str):
                tbl = tbl.drop(target_code.cat_col)
            else:
                tbl = tbl.drop(*target_code.cat_col)

    if drop_fold:
        if fold_col is not None:
            tbl = tbl.drop(fold_col)

    return tbl


def str_to_list(arg: Union[List[str], str], arg_name: str) -> List[str]:
    if isinstance(arg, str):
        return [arg]
    invalidInputError(isinstance(arg, list), arg_name + " should be str or a list of str")
    return arg


def featuretable_to_xshards(tbl: "SparkDataFrame",
                            convert_cols: Optional[Union[List[str], str]]=None):
    from bigdl.orca.learn.utils import dataframe_to_xshards_of_feature_dict
    # TODO: partition < node num
    if convert_cols is None:
        convert_cols = tbl.columns
    if convert_cols and not isinstance(convert_cols, list):
        convert_cols = [convert_cols]
    return dataframe_to_xshards_of_feature_dict(tbl.df, convert_cols, accept_str_col=True)
