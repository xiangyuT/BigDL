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
import os
from unittest import TestCase

import numpy as np
import pytest

import torch
import torch.nn as nn

from pyspark.sql import SparkSession
from pyspark.sql.types import FloatType, ArrayType, StructType, StructField

from bigdl.orca import OrcaContext
from bigdl.orca.data.pandas import read_csv
from bigdl.orca.learn.metrics import Accuracy

from bigdl.dllib.nncontext import init_nncontext
from bigdl.orca.learn.pytorch import Estimator
from bigdl.orca.data import SparkXShards
from bigdl.orca.data.image.utils import chunks
from bigdl.orca.learn.pytorch.callbacks.base import Callback

import tempfile
import shutil
import logging

np.random.seed(1337)  # for reproducibility
resource_path = os.path.join(
    os.path.realpath(os.path.dirname(__file__)), "../../../resources")


class LinearDataset(torch.utils.data.Dataset):
    """y = a * x + b"""

    def __init__(self, size=1000):
        X1 = torch.randn(size // 2, 50)
        X2 = torch.randn(size // 2, 50) + 1.5
        self.x = torch.cat([X1, X2], dim=0)
        Y1 = torch.zeros(size // 2, 1)
        Y2 = torch.ones(size // 2, 1)
        self.y = torch.cat([Y1, Y2], dim=0)

    def __getitem__(self, index):
        return self.x[index, None], self.y[index, None]

    def __len__(self):
        return len(self.x)


class Net(nn.Module):
    def __init__(self):
        super().__init__()
        self.fc1 = nn.Linear(50, 50)
        self.relu1 = nn.ReLU()
        self.dout = nn.Dropout(0.2)
        self.fc2 = nn.Linear(50, 100)
        self.prelu = nn.PReLU(1)
        self.out = nn.Linear(100, 1)
        self.out_act = nn.Sigmoid()

    def forward(self, input_):
        a1 = self.fc1(input_)
        h1 = self.relu1(a1)
        dout = self.dout(h1)
        a2 = self.fc2(dout)
        h2 = self.prelu(a2)
        a3 = self.out(h2)
        y = self.out_act(a3)
        return y


class IdentityNet(nn.Module):
    def __init__(self):
        super().__init__()
        # need this line to avoid optimizer raise empty variable list
        self.fc1 = nn.Linear(50, 50)

    def forward(self, input_):
        return input_


class LinearModel(nn.Module):
    def __init__(self):
        super().__init__()
        # need this line to avoid optimizer raise empty variable list
        self.fc1 = nn.Linear(1, 1, bias=False)
        self.fc1.weight.data.fill_(1.0)

    def forward(self, input_):
        return self.fc1(input_)


class MultiInputNet(nn.Module):
    def __init__(self):
        super().__init__()
        self.fc1 = nn.Linear(50, 50)
        self.out = nn.Linear(50, 1)
        self.out_act = nn.Sigmoid()

    def forward(self, input1, input2):
        x = torch.cat((input1, input2), 1)
        x = self.fc1(x)
        x = self.out(x)
        x = self.out_act(x)
        return x


class SimpleModel(nn.Module):
    def __init__(self):
        super().__init__()
        self.fc = nn.Linear(2, 1)
        self.out_act = nn.Sigmoid()

    def forward(self, input1, input2):
        x = torch.stack((input1, input2), dim=1)
        x = self.fc(x)
        x = self.out_act(x).flatten()
        return x


class CustomCallback(Callback):

    def on_train_end(self, logs=None):
        assert "train_loss" in logs
        assert "val_loss" in logs
        assert self.model

    def on_epoch_end(self, epoch, logs=None):
        assert "train_loss" in logs
        assert "val_loss" in logs
        assert self.model


def train_data_loader(config, batch_size):
    train_dataset = LinearDataset(size=config.get("data_size", 1000))
    train_loader = torch.utils.data.DataLoader(
        train_dataset,
        batch_size=batch_size
    )
    return train_loader


def val_data_loader(config, batch_size):
    val_dataset = LinearDataset(size=config.get("val_size", 400))
    validation_loader = torch.utils.data.DataLoader(
        val_dataset,
        batch_size=batch_size
    )
    return validation_loader


def get_model(config):
    torch.manual_seed(0)
    return Net()


def get_optimizer(model, config):
    return torch.optim.SGD(model.parameters(), lr=config.get("lr", 1e-2))


def get_estimator(workers_per_node=1, model_fn=get_model, sync_stats=False,
                  log_level=logging.INFO):
    estimator = Estimator.from_torch(model=model_fn,
                                     optimizer=get_optimizer,
                                     loss=nn.BCELoss(),
                                     metrics=Accuracy(),
                                     config={"lr": 1e-2},
                                     workers_per_node=workers_per_node,
                                     backend="spark",
                                     sync_stats=sync_stats,
                                     log_level=log_level)
    return estimator


class TestPyTorchEstimator(TestCase):
    def setUp(self) -> None:
        self.model_dir = tempfile.mkdtemp()

    def tearDown(self) -> None:
        shutil.rmtree(self.model_dir)

    def test_data_creator_convergence(self):
        estimator = get_estimator(workers_per_node=2)
        start_val_stats = estimator.evaluate(val_data_loader, batch_size=64)
        print(start_val_stats)
        train_stats = estimator.fit(train_data_loader, epochs=4, batch_size=128,
                                    validation_data=val_data_loader)
        print(train_stats)
        end_val_stats = estimator.evaluate(val_data_loader, batch_size=64)
        print(end_val_stats)
        assert 0 < end_val_stats["Accuracy"] < 1
        assert estimator.get_model()

        # sanity check that training worked
        dloss = end_val_stats["val_loss"] - start_val_stats["val_loss"]
        dacc = (end_val_stats["Accuracy"] -
                start_val_stats["Accuracy"])
        print(f"dLoss: {dloss}, dAcc: {dacc}")

        assert dloss < 0 < dacc, "training sanity check failed. loss increased!"

    def test_spark_xshards(self):
        from bigdl.dllib.nncontext import init_nncontext
        from bigdl.orca.data import SparkXShards
        estimator = get_estimator(workers_per_node=1)
        sc = init_nncontext()
        x_rdd = sc.parallelize(np.random.rand(4000, 1, 50).astype(np.float32))
        # torch 1.7.1+ requires target size same as output size, which is (batch, 1)
        y_rdd = sc.parallelize(np.random.randint(0, 2, size=(4000, 1, 1)).astype(np.float32))
        rdd = x_rdd.zip(y_rdd).map(lambda x_y: {'x': x_y[0], 'y': x_y[1]})
        train_rdd, val_rdd = rdd.randomSplit([0.9, 0.1])
        train_xshards = SparkXShards(train_rdd)
        val_xshards = SparkXShards(val_rdd)
        train_stats = estimator.fit(train_xshards, validation_data=val_xshards,
                                    batch_size=256, epochs=2)
        print(train_stats)
        val_stats = estimator.evaluate(val_xshards, batch_size=128)
        print(val_stats)
    
    def test_spark_xshards_of_dict(self):
        from bigdl.dllib.nncontext import init_nncontext
        from bigdl.orca.data import SparkXShards
        estimator = get_estimator(workers_per_node=1,
                                  model_fn=lambda config: MultiInputNet())
        sc = init_nncontext()
        x1_rdd = sc.parallelize(np.random.rand(4000, 1, 25).astype(np.float32))
        x2_rdd = sc.parallelize(np.random.rand(4000, 1, 25).astype(np.float32))
        # torch 1.7.1+ requires target size same as output size, which is (batch, 1)
        y_rdd = sc.parallelize(np.random.randint(0, 2, size=(4000, 1, 1)).astype(np.float32))
        rdd = x1_rdd.zip(x2_rdd).zip(y_rdd).map(lambda x_y: {'x': [x_y[0][0], x_y[0][1]], 'y': x_y[1]})
        train_rdd, val_rdd = rdd.randomSplit([0.9, 0.1])
        train_xshards = SparkXShards(train_rdd)
        val_xshards = SparkXShards(val_rdd)
        train_stats = estimator.fit(train_xshards, validation_data=val_xshards,
                                    batch_size=256, epochs=2)
        print(train_stats)
        val_stats = estimator.evaluate(val_xshards, batch_size=128)
        print(val_stats)

    def test_spark_xshards_not_dict(self):
        OrcaContext.pandas_read_backend = "pandas"
        file_path = os.path.join(resource_path, "orca/learn/ncf.csv")
        data_shard = read_csv(file_path, usecols=[0, 1, 2], dtype={0: np.float32, 1: np.float32,
                                                                   2: np.float32})
        
        estimator = get_estimator(model_fn=lambda config: SimpleModel())
        estimator.fit(data_shard, batch_size=2, epochs=2,
                      validation_data=data_shard,
                      feature_cols=["user", "item"],
                      label_cols=["label"])

        estimator.evaluate(data_shard, batch_size=2, feature_cols=["user", "item"],
                           label_cols=["label"])
        result = estimator.predict(data_shard, batch_size=2, feature_cols=["user", "item"])
        result.collect()

    def test_dataframe_train_eval(self):

        sc = init_nncontext()
        spark = SparkSession.builder.getOrCreate()
        rdd = sc.range(0, 100)
        data = rdd.map(lambda x: (np.random.randn(50).astype(np.float).tolist(),
                                  [float(np.random.randint(0, 2, size=()))])
                       )
        schema = StructType([
            StructField("feature", ArrayType(FloatType()), True),
            StructField("label", ArrayType(FloatType()), True)
        ])

        df = spark.createDataFrame(data=data, schema=schema)

        estimator = get_estimator(workers_per_node=2)
        estimator.fit(df, batch_size=4, epochs=2,
                      validation_data=df,
                      feature_cols=["feature"],
                      label_cols=["label"])
        estimator.evaluate(df, batch_size=4,
                           feature_cols=["feature"],
                           label_cols=["label"])
        estimator.shutdown()

    def test_dataframe_shard_size_train_eval(self):
        from bigdl.orca import OrcaContext
        OrcaContext._shard_size = 30
        sc = init_nncontext()
        spark = SparkSession.builder.getOrCreate()
        rdd = sc.range(0, 100)
        data = rdd.map(lambda x: (np.random.randn(50).astype(np.float).tolist(),
                                  [float(np.random.randint(0, 2, size=()))])
                       )
        schema = StructType([
            StructField("feature", ArrayType(FloatType()), True),
            StructField("label", ArrayType(FloatType()), True)
        ])
        df = spark.createDataFrame(data=data, schema=schema)

        estimator = get_estimator(workers_per_node=2)
        estimator.fit(df, batch_size=4, epochs=2,
                      feature_cols=["feature"],
                      label_cols=["label"])
        estimator.evaluate(df, batch_size=4,
                           feature_cols=["feature"],
                           label_cols=["label"])

    def test_partition_num_less_than_workers(self):
        sc = init_nncontext()
        spark = SparkSession.builder.getOrCreate()
        rdd = sc.range(200, numSlices=1)
        data = rdd.map(lambda x: (np.random.randn(50).astype(np.float).tolist(),
                                  [float(np.random.randint(0, 2, size=()))])
                       )
        schema = StructType([
            StructField("feature", ArrayType(FloatType()), True),
            StructField("label", ArrayType(FloatType()), True)
        ])

        df = spark.createDataFrame(data=data, schema=schema)

        estimator = get_estimator(workers_per_node=2)
        assert df.rdd.getNumPartitions() < estimator.num_workers

        estimator.fit(df, batch_size=4, epochs=2,
                      validation_data=df,
                      feature_cols=["feature"],
                      label_cols=["label"])
        estimator.evaluate(df, batch_size=4,
                           feature_cols=["feature"],
                           label_cols=["label"])
        estimator.predict(df, feature_cols=["feature"]).collect()

    def test_dataframe_predict(self):

        sc = init_nncontext()
        rdd = sc.parallelize(range(20))
        df = rdd.map(lambda x: ([float(x)] * 5,
                                [int(np.random.randint(0, 2, size=()))])
                     ).toDF(["feature", "label"])

        estimator = get_estimator(workers_per_node=2,
                                  model_fn=lambda config: IdentityNet())
        result = estimator.predict(df, batch_size=4,
                                   feature_cols=["feature"])
        expr = "sum(cast(feature <> to_array(prediction) as int)) as error"
        assert result.selectExpr(expr).first()["error"] == 0

    def test_xshards_predict_save_load(self):

        sc = init_nncontext()
        rdd = sc.range(0, 110).map(lambda x: np.array([x] * 50))
        shards = rdd.mapPartitions(lambda iter: chunks(iter, 5)).map(lambda x: {"x": np.stack(x)})
        shards = SparkXShards(shards)

        estimator = get_estimator(workers_per_node=2,
                                  model_fn=lambda config: IdentityNet())
        result_shards = estimator.predict(shards, batch_size=4)
        result_before = np.concatenate([shard["prediction"] for shard in result_shards.collect()])
        expected_result = np.concatenate([shard["x"] for shard in result_shards.collect()])
        assert np.array_equal(result_before, expected_result)

        path = "/tmp/model.pth"
        try:
            estimator.save(path)
            estimator.load(path)
            result_shards = estimator.predict(shards, batch_size=4)
            result_after = np.concatenate([shard["prediction"]
                                           for shard in result_shards.collect()])

        finally:
            os.remove(path)

        assert np.array_equal(result_before, result_after)

    # currently do not support pandas dataframe
    # def test_pandas_dataframe(self):

    #     OrcaContext.pandas_read_backend = "pandas"
    #     file_path = os.path.join(resource_path, "orca/learn/ncf.csv")
    #     data_shard = read_csv(file_path, usecols=[0, 1, 2], dtype={0: np.float32, 1: np.float32,
    #                                                                2: np.float32})

    #     estimator = get_estimator(model_fn=lambda config: SimpleModel())
    #     estimator.fit(data_shard, batch_size=2, epochs=2,
    #                   feature_cols=["user", "item"],
    #                   label_cols=["label"])

    #     estimator.evaluate(data_shard, batch_size=2, feature_cols=["user", "item"],
    #                        label_cols=["label"])
    #     result = estimator.predict(data_shard, batch_size=2, feature_cols=["user", "item"])
    #     result.collect()

    def test_multiple_inputs_model(self):

        sc = init_nncontext()
        rdd = sc.parallelize(range(100))

        from pyspark.sql import SparkSession
        spark = SparkSession.builder.getOrCreate()
        data = rdd.map(lambda x: ([float(x)] * 25, [float(x)] * 25,
                                  [float(np.random.randint(0, 2, size=()))])
                       )
        schema = StructType([
            StructField("f1", ArrayType(FloatType()), True),
            StructField("f2", ArrayType(FloatType()), True),
            StructField("label", ArrayType(FloatType()), True)
        ])

        df = spark.createDataFrame(data=data, schema=schema)

        estimator = get_estimator(workers_per_node=2,
                                  model_fn=lambda config: MultiInputNet())
        estimator.fit(df, batch_size=4, epochs=2,
                      validation_data=df,
                      feature_cols=["f1", "f2"],
                      label_cols=["label"])
        estimator.evaluate(df, batch_size=4,
                           feature_cols=["f1", "f2"],
                           label_cols=["label"])
        result = estimator.predict(df, batch_size=4,
                                   feature_cols=["f1", "f2"])
        result.collect()

    def test_data_parallel_sgd_correctness(self):
        sc = init_nncontext()
        spark = SparkSession.builder.getOrCreate()
        rdd = sc.range(0, 100).repartition(2)

        # partition 0: [(0, 0), (0, 0)]
        # partition 1: [(1, 0), (1, 0)]
        # model: y = w * x
        # loss = (wx)^2
        # dloss/dw = 2x^2*w
        # end of first iteration:
        #    partition 0 loss: 0.0
        #    partition 1 loss: 1.0
        #    avg_grad = avg([0, 0, 2, 2]) = 1
        #    weight = 1.0 - 0.5 * avg_grad = 0.5
        # end of second iteration:
        #    partition 0 loss: 0.0
        #    partition 1 loss: 0.25
        #    avg_grad = avg([0, 0, 1, 1]) = 0.5
        #    weight = 0.5 - 0.5 * avg_grad = 0.25
        data = rdd.mapPartitionsWithIndex(lambda idx, iter: [([float(idx)], [0.0]) for _ in iter][:2])
        schema = StructType([
            StructField("feature", ArrayType(FloatType()), True),
            StructField("label", ArrayType(FloatType()), True)
        ])
        df = spark.createDataFrame(data=data, schema=schema)

        def get_optimizer(model, config):
            return torch.optim.SGD(model.parameters(), lr=0.5)

        estimator = Estimator.from_torch(model=lambda config: LinearModel(),
                                         optimizer=get_optimizer,
                                         loss=torch.nn.MSELoss(),
                                         metrics=Accuracy(),
                                         config={},
                                         workers_per_node=2,
                                         backend="spark",
                                         sync_stats=False)

        stats = estimator.fit(df, batch_size=4, epochs=2,
                              validation_data=df,
                              feature_cols=["feature"],
                              label_cols=["label"],
                              reduce_results=False)

        state = estimator.get_state_dict()
        assert state['models'][0]['fc1.weight'].item() == 0.25

    def test_checkpoint_callback(self):
        from bigdl.orca.learn.pytorch.callbacks.model_checkpoint import ModelCheckpoint
        sc = OrcaContext.get_spark_context()
        spark = SparkSession.builder.getOrCreate()
        rdd = sc.range(0, 100)
        epochs = 2
        data = rdd.map(lambda x: (np.random.randn(50).astype(np.float).tolist(),
                                  [float(np.random.randint(0, 2, size=()))])
                       )
        schema = StructType([
            StructField("feature", ArrayType(FloatType()), True),
            StructField("label", ArrayType(FloatType()), True)
        ])
        df = spark.createDataFrame(data=data, schema=schema)
        df = df.cache()

        estimator = get_estimator(workers_per_node=2, log_level=logging.DEBUG)

        callbacks = [
            ModelCheckpoint(filepath=os.path.join(self.model_dir, "test-{epoch}"),
                            save_weights_only=True)
        ]
        estimator.fit(df, batch_size=4, epochs=epochs,
                      callbacks=callbacks,
                      feature_cols=["feature"],
                      label_cols=["label"])
        eval_before = estimator.evaluate(df, batch_size=4,
                                         feature_cols=["feature"],
                                         label_cols=["label"])

        for i in range(epochs):
            assert os.path.isfile(os.path.join(self.model_dir, f"test-epoch={i + 1}.ckpt"))

        latest_checkpoint_path = Estimator.latest_checkpoint(self.model_dir)
        assert os.path.isfile(latest_checkpoint_path)
        estimator.shutdown()

        new_estimator = get_estimator(workers_per_node=2, log_level=logging.DEBUG)
        new_estimator.load_checkpoint(latest_checkpoint_path)
        eval_after = new_estimator.evaluate(df, batch_size=4,
                                            feature_cols=["feature"],
                                            label_cols=["label"])
        for name, value in eval_before.items():
            print(f"Comparing evaluate result of {name}")
            np.testing.assert_almost_equal(value, eval_after[name])
        res = new_estimator.predict(df, feature_cols=["feature"]).collect()

    def test_manual_ckpt(self):
        sc = OrcaContext.get_spark_context()
        spark = SparkSession.builder.getOrCreate()
        rdd = sc.range(0, 100)
        epochs = 2
        data = rdd.map(lambda x: (np.random.randn(50).astype(np.float).tolist(),
                                  [float(np.random.randint(0, 2, size=()))])
                       )
        schema = StructType([
            StructField("feature", ArrayType(FloatType()), True),
            StructField("label", ArrayType(FloatType()), True)
        ])

        df = spark.createDataFrame(data=data, schema=schema)
        df = df.cache()

        estimator = get_estimator(workers_per_node=2, log_level=logging.DEBUG)
        estimator.fit(df, batch_size=4, epochs=epochs,
                      feature_cols=["feature"],
                      label_cols=["label"])
        eval_before = estimator.evaluate(df, batch_size=4,
                                         feature_cols=["feature"],
                                         label_cols=["label"])

        try:
            temp_dir = tempfile.mkdtemp()
            ckpt_file = os.path.join(temp_dir, "manual.ckpt")
            estimator.save_checkpoint(ckpt_file)
            estimator.shutdown()
            new_estimator = get_estimator(workers_per_node=2, log_level=logging.DEBUG)
            new_estimator.load_checkpoint(ckpt_file)
            eval_after = new_estimator.evaluate(df, batch_size=4,
                                                feature_cols=["feature"],
                                                label_cols=["label"])
            for name, value in eval_before.items():
                np.testing.assert_almost_equal(value, eval_after[name])
        finally:
            shutil.rmtree(temp_dir)

    def test_custom_callback(self):
        estimator = get_estimator(workers_per_node=2)
        callbacks = [CustomCallback()]
        estimator.fit(train_data_loader, epochs=4, batch_size=128,
                      validation_data=val_data_loader, callbacks=callbacks)

    def test_tensorboard_callback(self):
        from bigdl.orca.learn.pytorch.callbacks.tensorboard import TensorBoardCallback
        sc = OrcaContext.get_spark_context()
        spark = SparkSession.builder.getOrCreate()
        rdd = sc.range(0, 100)
        epochs = 2
        data = rdd.map(lambda x: (np.random.randn(50).astype(np.float).tolist(),
                                  [float(np.random.randint(0, 2, size=()))])
                       )
        schema = StructType([
            StructField("feature", ArrayType(FloatType()), True),
            StructField("label", ArrayType(FloatType()), True)
        ])
        df = spark.createDataFrame(data=data, schema=schema)
        df = df.cache()

        estimator = get_estimator(workers_per_node=2, log_level=logging.DEBUG)

        try:
            temp_dir = tempfile.mkdtemp()
            log_dir = os.path.join(temp_dir, "runs_epoch")

            callbacks = [
                TensorBoardCallback(log_dir=log_dir, freq="epoch")
            ]
            estimator.fit(df, batch_size=4, epochs=epochs,
                          callbacks=callbacks,
                          feature_cols=["feature"],
                          label_cols=["label"])

            assert len(os.listdir(log_dir)) > 0

            log_dir = os.path.join(temp_dir, "runs_batch")

            callbacks = [
                TensorBoardCallback(log_dir=log_dir, freq="batch")
            ]
            estimator.fit(df, batch_size=4, epochs=epochs,
                          callbacks=callbacks,
                          feature_cols=["feature"],
                          label_cols=["label"])

            assert len(os.listdir(log_dir)) > 0
        finally:
            shutil.rmtree(temp_dir)

        estimator.shutdown()

if __name__ == "__main__":
    pytest.main([__file__])
