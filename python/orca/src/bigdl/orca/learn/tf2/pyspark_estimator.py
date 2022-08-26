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

import logging
import os

from pyspark.sql.dataframe import DataFrame
import tempfile
import shutil
import glob

import pickle

import tensorflow as tf

from bigdl.dllib.utils.common import get_node_and_core_number
from bigdl.dllib.utils.file_utils import enable_multi_fs_load, enable_multi_fs_save, \
    is_local_path, append_suffix
from bigdl.dllib.utils.utils import get_node_ip

from bigdl.orca.data.file import is_file, exists, get_remote_file_to_local, \
    get_remote_files_with_prefix_to_local, put_local_file_to_remote, \
    put_local_files_with_prefix_to_remote
from bigdl.orca.learn.tf2.spark_runner import SparkRunner
from bigdl.orca.learn.utils import find_free_port, find_ip_and_free_port
from bigdl.orca.learn.utils import maybe_dataframe_to_xshards, dataframe_to_xshards, \
    convert_predict_xshards_to_dataframe, make_data_creator, load_model, \
    save_model, process_xshards_of_pandas_dataframe
from bigdl.orca.learn.log_monitor import start_log_server, stop_log_server
from bigdl.orca.data.shard import SparkXShards
from bigdl.orca import OrcaContext
from bigdl.dllib.utils.log4Error import invalidInputError

logger = logging.getLogger(__name__)


class SparkTFEstimator():
    def __init__(self,
                 model_creator,
                 config=None,
                 compile_args_creator=None,
                 verbose=False,
                 workers_per_node=1,
                 model_dir=None,
                 log_to_driver=True,
                 **kwargs):
        self.model_creator = model_creator
        self.compile_args_creator = compile_args_creator
        self.config = {} if config is None else config
        self.verbose = verbose

        sc = OrcaContext.get_spark_context()

        num_node, num_core = get_node_and_core_number()
        self.num_workers = num_node * workers_per_node
        self.total_cores = num_node * num_core

        # over partition to cover tasks all over the cluster
        self.workerRDD = sc.parallelize(list(range(self.total_cores * 4)),
                                        self.total_cores * 4).repartition(self.num_workers)

        if "inter_op_parallelism" not in self.config:
            self.config["inter_op_parallelism"] = 1
        if "intra_op_parallelism" not in self.config:
            self.config["intra_op_parallelism"] = num_core // workers_per_node

        self.model_weights = None

        if "batch_size" in self.config:
            invalidInputError(False,
                              "Please do not specify batch_size in config. Input batch_size in the"
                              " fit/evaluate function of the estimator instead.")
        self.model_dir = model_dir
        master = sc.getConf().get("spark.master")
        if not master.startswith("local"):
            logger.info("For cluster mode, make sure to use shared filesystem path "
                        "as model directory.")

        self.application_id = sc.applicationId
        self.ip = get_node_ip()
        self.port = find_free_port()
        is_local = sc.master.startswith("local")
        self.need_to_log_to_driver = (not is_local) and log_to_driver
        if self.need_to_log_to_driver:
            self.log_server_thread = start_log_server(self.ip, self.port)

    def _get_cluster_info(self, sc):
        cluster_info = self.workerRDD.barrier().mapPartitions(find_ip_and_free_port).collect()
        return cluster_info

    def fit(self, data, epochs=1, batch_size=32, verbose=1,
            callbacks=None, validation_data=None, class_weight=None, initial_epoch=0,
            steps_per_epoch=None, validation_steps=None, validation_freq=1,
            data_config=None, feature_cols=None,
            label_cols=None):
        """
        Train this tensorflow model with train data.
        :param data: train data. It can be XShards, Spark DataFrame or creator function which
               returns Iter or DataLoader.
               If data is XShards, each partition can be a Pandas DataFrame or a dictionary of
               {'x': feature, 'y': label}, where feature(label) is a numpy array or a tuple of
               numpy arrays.
        :param epochs: Number of epochs to train the model. Default: 1.
        :param batch_size: Batch size used for training. Default: 32.
        :param verbose: Prints output of one model if true.
        :param callbacks: List of Keras compatible callbacks to apply during training.
        :param validation_data: validation data. Validation data type should be the same
               as train data.
        :param class_weight: Optional dictionary mapping class indices (integers) to a weight
               (float) value, used for weighting the loss function. This can be useful to tell
               the model to "pay more attention" to samples from an under-represented class.
        :return:
        """
        sc = OrcaContext.get_spark_context()

        # Data partition should be equal to num workers.
        # Repartition Spark DataFrame before converting to SparkXShards.
        # Repartition on SparkXShards will result in empty partitions.
        if isinstance(data, DataFrame) or isinstance(data, SparkXShards):
            if data.rdd.getNumPartitions() != self.num_workers:
                data = data.repartition(self.num_workers)
            if validation_data and validation_data.rdd.getNumPartitions() != self.num_workers:
                validation_data = validation_data.repartition(self.num_workers)
        data, validation_data = maybe_dataframe_to_xshards(data, validation_data,
                                                           feature_cols, label_cols,
                                                           mode="fit",
                                                           num_workers=self.num_workers,
                                                           accept_str_col=True)

        # for continuous training
        if self.model_weights:
            weights = sc.broadcast(self.model_weights)
        else:
            weights = None

        init_params = dict(
            model_creator=self.model_creator,
            compile_args_creator=self.compile_args_creator,
            config=self.config,
            verbose=self.verbose,
            size=self.num_workers,
            model_weights=weights,
            mode="fit",
            cluster_info=self._get_cluster_info(sc),
            model_dir=self.model_dir,
            application_id=self.application_id,
            need_to_log_to_driver=self.need_to_log_to_driver,
            driver_ip=self.ip,
            driver_port=self.port
        )

        params = dict(
            epochs=epochs,
            batch_size=batch_size,
            verbose=verbose,
            callbacks=callbacks,
            class_weight=class_weight,
            initial_epoch=initial_epoch,
            steps_per_epoch=steps_per_epoch,
            validation_steps=validation_steps,
            validation_freq=validation_freq,
            data_config=data_config
        )

        if isinstance(data, SparkXShards):
            # set train/validation data
            if data._get_class_name() == 'pandas.core.frame.DataFrame':
                data, validation_data = process_xshards_of_pandas_dataframe(data,
                                                                            feature_cols,
                                                                            label_cols,
                                                                            validation_data,
                                                                            "fit")
            if validation_data is None:
                def transform_func(iter, init_param, param):
                    partition_data = list(iter)
                    param["data_creator"] = make_data_creator(partition_data)
                    return SparkRunner(**init_param).step(**param)

                res = data.rdd.barrier().mapPartitions(
                    lambda iter: transform_func(iter, init_params, params)).collect()
            else:
                def transform_func(iter, init_param, param):
                    data_tuple_list = list(iter)
                    data_list = [x for data_tuple in data_tuple_list for x in data_tuple[0]]
                    valid_list = [x for data_tuple in data_tuple_list for x in data_tuple[1]]
                    param["data_creator"] = make_data_creator(data_list)
                    param["validation_data_creator"] = make_data_creator(valid_list)
                    return SparkRunner(**init_param).step(**param)

                train_rdd = data.rdd.mapPartitions(lambda iter: [list(iter)])
                val_rdd = validation_data.rdd.mapPartitions(lambda iter: [list(iter)])
                res = train_rdd.zip(val_rdd).barrier().mapPartitions(
                    lambda iter: transform_func(iter, init_params, params)).collect()
        else:
            params["data_creator"] = data
            params["validation_data_creator"] = validation_data

            def transform_func(iter, init_param, param):
                return SparkRunner(**init_param).step(**param)

            res = self.workerRDD.barrier().mapPartitions(
                lambda iter: transform_func(iter, init_params, params)).collect()

        if self.model_dir:
            try:
                temp_dir = tempfile.mkdtemp()
                get_remote_file_to_local(os.path.join(self.model_dir, "state.pkl"),
                                         os.path.join(temp_dir, "state.pkl"))
                import pickle
                with open(os.path.join(temp_dir, "state.pkl"), 'rb') as f:
                    state = pickle.load(f)
                    self.model_weights = state['weights']
            finally:
                shutil.rmtree(temp_dir)

        return res[0]

    def evaluate(self, data, batch_size=32, num_steps=None, verbose=1,
                 sample_weight=None, callbacks=None, data_config=None,
                 feature_cols=None, label_cols=None):
        """
        Evaluates the model on the validation data set.
        :param data: evaluate data. It can be XShards, Spark DataFrame or creator function which
               returns Iter or DataLoader.
               If data is XShards, each partition can be a Pandas DataFrame or a dictionary of
               {'x': feature, 'y': label}, where feature(label) is a numpy array or a tuple of
               numpy arrays.
        :param validation_data: validation data. Validation data type should be the same
               as train data.
        :param batch_size: Batch size used for evaluation. Default: 32.
        :param verbose: Prints output of one model if true.
        :param callbacks: List of Keras compatible callbacks to apply during evaluation.
        :param class_weight: Optional dictionary mapping class indices (integers) to a weight
               (float) value, used for weighting the loss function. This can be useful to tell
               the model to "pay more attention" to samples from an under-represented class.
        :return: validation result
        """
        sc = OrcaContext.get_spark_context()
        logger.info("Starting validation step.")

        if isinstance(data, DataFrame) or isinstance(data, SparkXShards):
            if data.rdd.getNumPartitions() != self.num_workers:
                data = data.repartition(self.num_workers)
        data, _ = maybe_dataframe_to_xshards(data, validation_data=None,
                                             feature_cols=feature_cols,
                                             label_cols=label_cols,
                                             mode="evaluate",
                                             num_workers=self.num_workers,
                                             accept_str_col=True)

        if self.model_weights:
            weights = sc.broadcast(self.model_weights)
        else:
            weights = None

        init_params = dict(
            model_creator=self.model_creator,
            compile_args_creator=self.compile_args_creator,
            config=self.config,
            verbose=self.verbose,
            size=self.num_workers,
            model_weights=weights,
            mode="evaluate",
            cluster_info=self._get_cluster_info(sc),
            model_dir=self.model_dir,
            application_id=self.application_id,
            need_to_log_to_driver=self.need_to_log_to_driver,
            driver_ip=self.ip,
            driver_port=self.port
        )

        params = dict(
            batch_size=batch_size,
            verbose=verbose,
            sample_weight=sample_weight,
            steps=num_steps,
            callbacks=callbacks,
            data_config=data_config,
        )

        if isinstance(data, SparkXShards):
            # set train/validation data
            if data._get_class_name() == 'pandas.core.frame.DataFrame':
                data = process_xshards_of_pandas_dataframe(data, feature_cols, label_cols)

            def transform_func(iter, init_param, param):
                partition_data = list(iter)
                param["data_creator"] = make_data_creator(partition_data)
                return SparkRunner(**init_param).validate(**param)

            res = data.rdd.barrier().mapPartitions(
                lambda iter: transform_func(iter, init_params, params)).collect()
        else:
            params["data_creator"] = data

            def transform_func(iter, init_param, param):
                return SparkRunner(**init_param).validate(**param)

            res = self.workerRDD.barrier().mapPartitions(
                lambda iter: transform_func(iter, init_params, params)).collect()
        return res[0]

    def predict(self, data, batch_size=None, verbose=1,
                steps=None, callbacks=None, data_config=None,
                feature_cols=None):
        """
        Predict the input data
        :param data: predict input data.  It can be XShards or Spark DataFrame.
               If data is XShards, each partition can be a Pandas DataFrame or a dictionary of
               {'x': feature}, where feature is a numpy array or a tuple of numpy arrays.
        :param batch_size: Batch size used for inference. Default: None.
        :param verbose: Prints output of one model if true.
        :param steps: Total number of steps (batches of samples) before declaring the prediction
               round finished. Ignored with the default value of None.
        :param callbacks: List of Keras compatible callbacks to apply during prediction.
        :param data_config: An optional dictionary that can be passed to data creator function.
        :param feature_cols: Feature column name(s) of data. Only used when data is a Spark
               DataFrame or an XShards of Pandas DataFrame. Default: None.
        :return:
        """
        logger.info("Starting predict step.")
        sc = OrcaContext.get_spark_context()
        if self.model_weights:
            weights = sc.broadcast(self.model_weights)
        else:
            weights = None

        init_params = dict(
            model_creator=self.model_creator,
            compile_args_creator=self.compile_args_creator,
            config=self.config,
            verbose=self.verbose,
            size=self.num_workers,
            model_weights=weights,
            mode="predict",
            cluster_info=self._get_cluster_info(sc),
            model_dir=self.model_dir,
            application_id=self.application_id,
            need_to_log_to_driver=self.need_to_log_to_driver,
            driver_ip=self.ip,
            driver_port=self.port
        )

        params = dict(
            verbose=verbose,
            batch_size=batch_size,
            steps=steps,
            callbacks=callbacks,
            data_config=data_config
        )

        if isinstance(data, DataFrame):
            pre_predict_data = data.repartition(self.num_workers)
            xshards, _ = dataframe_to_xshards(pre_predict_data,
                                              validation_data=None,
                                              feature_cols=feature_cols,
                                              label_cols=None,
                                              mode="predict",
                                              accept_str_col=True)

            def transform_func(iter, init_param, param):
                partition_data = list(iter)
                # res = combine_in_partition(partition_data)
                param["data_creator"] = make_data_creator(partition_data)
                return SparkRunner(**init_param).predict(**param)

            pred_shards = SparkXShards(xshards.rdd.mapPartitions(
                lambda iter: transform_func(iter, init_params, params)))
            result = convert_predict_xshards_to_dataframe(pre_predict_data, pred_shards)
        else:
            invalidInputError(False,
                              "Only xshards or Spark DataFrame is supported for predict")

        return result

    def save_weights(self, filepath, overwrite=True, save_format=None):
        """
        Save model weights at the provided path.
        :param filepath: String or PathLike, path to the file to save the weights to.
        When saving in TensorFlow format, this is the prefix used for checkpoint files
        (multiple files are generated). Note that the '.h5' suffix causes weights to be
        saved in HDF5 format. It can be local, hdfs, or s3 filepath.
        :param overwrite: Whether to silently overwrite any existing file at the target location,
        or provide the user with a manual prompt.
        :param save_format: Either 'tf' or 'h5'.
        A filepath ending in '.h5' or '.keras' will default to HDF5 if save_format is None.
        Otherwise None defaults to 'tf'.
        """

        # Some model might need to aggregate variables during checkpointing
        # which requires both the chief and workers to participate in the
        # allreduce communication protocol.
        # So we need to call get_state on every remote workers, otherwise
        # it might get stuck
        model = self.model_creator(self.config)
        model.set_weights(self.model_weights)
        if is_local_path(filepath):
            model.save_weights(filepath, overwrite, save_format)
        else:
            file_name = os.path.basename(filepath)
            temp_dir = tempfile.mkdtemp()
            temp_path = os.path.join(temp_dir, file_name)
            try:
                model.save_weights(temp_path, overwrite, save_format)
                if save_format == 'h5' or filepath.endswith('.h5') or filepath.endswith('.keras'):
                    # hdf5 format
                    put_local_file_to_remote(temp_path, filepath, over_write=overwrite)
                else:
                    # tf format
                    remote_dir = os.path.dirname(filepath)
                    put_local_files_with_prefix_to_remote(temp_path, remote_dir,
                                                          over_write=overwrite)
            finally:
                shutil.rmtree(temp_dir)

    def load_weights(self, filepath, by_name=False):
        """
        Load tensorflow keras model weights in this estimator.

        :param filepath: keras model weights save path.
        :param by_name: Boolean, whether to load weights by name or by topological
               order. Only topological loading is supported for weight files in
               TensorFlow format.
        """
        model = self.model_creator(self.config)
        if is_file(filepath):
            # h5 format
            if is_local_path(filepath):
                model.load_weights(filepath, by_name)
            else:
                file_name = os.path.basename(filepath)
                temp_dir = tempfile.mkdtemp()
                temp_path = os.path.join(temp_dir, file_name)
                try:
                    get_remote_file_to_local(filepath, temp_path)
                    model.load_weights(temp_path, by_name)
                finally:
                    shutil.rmtree(temp_dir)
        else:
            # tensorflow format
            if is_local_path(filepath):
                model.load_weights(filepath, by_name)
            else:
                temp_dir = tempfile.mkdtemp()
                try:
                    prefix = os.path.basename(filepath)
                    get_remote_files_with_prefix_to_local(filepath, temp_dir)
                    model.load_weights(os.path.join(temp_dir, prefix), by_name)
                finally:
                    shutil.rmtree(temp_dir)
        self.model_weights = model.get_weights()

    def save(self,
             filepath,
             overwrite=True,
             include_optimizer=True,
             save_format=None,
             signatures=None,
             options=None):
        """
        Saves the model to Tensorflow SavedModel or a single HDF5 file.

        :param filepath: String, PathLike, path to SavedModel or H5 file to save the
            model. It can be local/hdfs/s3 filepath
        :param overwrite: Whether to silently overwrite any existing file at the
            target location, or provide the user with a manual prompt.
        :param include_optimizer: If True, save optimizer's state together.
        :param save_format: Either `'tf'` or `'h5'`, indicating whether to save the
            model to Tensorflow SavedModel or HDF5. Defaults to 'tf' in TF 2.X,
            and 'h5' in TF 1.X.
        :param signatures: Signatures to save with the SavedModel. Applicable to the
            'tf' format only. Please see the `signatures` argument in
            `tf.saved_model.save` for details.
        :param options: (only applies to SavedModel format)
            `tf.saved_model.SaveOptions` object that specifies options for
            saving to SavedModel.
        """
        # get current model
        if exists(self._model_saved_path):
            model = load_model(self._model_saved_path)
        else:
            model = self.get_model()
        # save model
        save_model(model, filepath, overwrite=overwrite, include_optimizer=include_optimizer,
                   save_format=save_format, signatures=signatures, options=options)

    def load(self, filepath, custom_objects=None, compile=True):
        """
        Loads a model saved via `estimator.save()

        :param filepath: (str) Path of saved model.
        :param custom_objects: Optional dictionary mapping names
          (strings) to custom classes or functions to be
          considered during deserialization.
        :param compile: Boolean, whether to compile the model after loading.
        :param options: Optional `tf.saved_model.LoadOptions` object that specifies
        options for loading from SavedModel.

        """
        model = load_model(filepath, custom_objects=custom_objects, compile=compile)
        self.model_weights = model.get_weights()
        # update remote model
        save_model(model, self._model_saved_path, save_format="h5", filemode=0o666)

    def get_model(self):
        """
        Returns the learned model.

        :return: the learned model.
        """
        model = self.model_creator(self.config)
        model.set_weights(self.model_weights)
        return model

    @property
    def _model_saved_path(self):
        return os.path.join(self.model_dir, "{}_model.h5".format(self.application_id))

    def shutdown(self):
        """
        Shutdown estimator and release resources.
        """
        stop_log_server(self.log_server_thread, self.ip, self.port)
